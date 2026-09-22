#!/usr/bin/env python3
"""Install the signed, automatically updated release into Application Support."""
from __future__ import annotations
import argparse
import contextlib
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import time
import updater as u

LABEL='local.codex-idle-compactor'

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state',type=Path,default=Path.home()/'Library/Application Support/Codex Idle Compactor')
    modes=p.add_mutually_exclusive_group()
    modes.add_argument('--enable',action='store_true');modes.add_argument('--observe',action='store_true')
    p.add_argument('--max-per-day',type=int,default=2);p.add_argument('--max-per-month',type=int,default=20)
    p.add_argument('--codex-home',type=Path,default=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex'))))
    a=p.parse_args();root=a.state.expanduser().resolve()
    if sys.platform!='darwin':raise SystemExit('This installer supports macOS only.')
    root.mkdir(parents=True,exist_ok=True,mode=0o700);root.chmod(0o700)
    key=(Path(__file__).parent/'release-public.pem').read_text()
    if (root/'release-public.pem').exists() and (root/'release-public.pem').read_text()!=key:
        raise u.UpdateError('Installed update trust key differs; refusing to replace it')
    candidate=u.stage(root,u.download(u.FEED,65536),key)
    if candidate:u.activate(root,candidate)
    active=u.read_json(root/'active.json');release=root/'releases'/active['current']
    # The bootstrap copied here was authenticated as part of the signed package.
    shutil.copy2(release/'boot.py',root/'boot.py');(root/'boot.py').chmod(0o700)
    shutil.copy2(release/'control.py',root/'control.py');(root/'control.py').chmod(0o700)
    (root/'release-public.pem').write_text(key);(root/'release-public.pem').chmod(0o600)
    settings=u.read_json(root/'installation.json');settings.setdefault('codex_home',str(a.codex_home.expanduser().resolve()))
    u.save(root/'installation.json',settings)
    control=[sys.executable,str(release/'idle_compactor.py'),'--state',str(root)]
    if a.enable:
        # Activation is explicit. Retention of an existing config never silently
        # enlarges an approved cap during an automatic update.
        subprocess.run(control+['enable','--accept-metered-compaction','--max-per-day',str(a.max_per_day),
                               '--max-per-month',str(a.max_per_month)],check=True)
    elif a.observe:
        subprocess.run(control+['pause'],check=True)
    else:
        subprocess.run(control+['status'],check=True,stdout=subprocess.DEVNULL)
    ledger=root/'ledger.sqlite';deadline=time.monotonic()+240
    while ledger.exists():
        with contextlib.closing(sqlite3.connect(ledger)) as db:
            pending=db.execute("SELECT count(*) FROM attempts WHERE status IN ('reserved','acknowledged')").fetchone()[0]
        if not pending:break
        if time.monotonic()>=deadline:raise u.UpdateError('An earlier compaction is unresolved; reconcile before installing')
        time.sleep(1)
    domain='gui/'+str(os.getuid())
    subprocess.run(['launchctl','bootout',domain+'/'+LABEL],capture_output=True)
    path=Path.home()/'Library/LaunchAgents'/(LABEL+'.plist');path.parent.mkdir(parents=True,exist_ok=True)
    spec=dict(Label=LABEL,ProgramArguments=[sys.executable,str(root/'boot.py')],RunAtLoad=True,
              KeepAlive=True,ThrottleInterval=30,ProcessType='Background',ExitTimeOut=10)
    tmp=path.with_suffix('.tmp');tmp.write_bytes(plistlib.dumps(spec));tmp.chmod(0o600);os.replace(tmp,path)
    subprocess.run(['launchctl','bootstrap',domain,str(path)],check=True,capture_output=True)
    print('Installed '+active['current']+'. Signed updates check every 30 minutes. Settings and attempt ledger preserved.')

if __name__=='__main__':main()
