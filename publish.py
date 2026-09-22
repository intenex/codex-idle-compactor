#!/usr/bin/env python3
"""Maintainer-only, manually triggered signed release. No hosted CI or scheduler."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import zipfile
import updater as u

FILES=['idle_compactor.py','compatibility.py','launcher.py','updater.py','boot.py','install.py','control.py',
       'release.json','release-public.pem','README.md','VERIFICATION.md','MAINTAINER.md',
       'Install.command','Install Observation.command','Pause.command','Uninstall.command',
       'test_idle_compactor.py','test_updater.py','publish.py']

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--version',required=True)
    p.add_argument('--key',type=Path,default=Path.home()/'Library/Application Support/Codex Idle Compactor Publisher/release-signing.pem')
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--publish',action='store_true')
    a=p.parse_args();base=Path(__file__).resolve().parent
    if not re.fullmatch(u.VERSION_PATTERN,a.version):raise SystemExit('Use a numeric major.minor.patch version')
    for name in ('idle_compactor.py','launcher.py'):
        if not re.search(r"VERSION\s*=\s*['\"]"+re.escape(a.version)+r"['\"]",(base/name).read_text()):
            raise SystemExit('Update VERSION in '+name+' before releasing')
    if u.read_json(base/'release.json').get('version')!=a.version:raise SystemExit('Update release.json first')
    subprocess.run([sys.executable,'-m','unittest','discover','-s',str(base),'-q'],check=True)
    if not a.key.is_file():raise SystemExit('Release signing key unavailable; restore your private backup')
    a.out.mkdir(parents=True,exist_ok=True)
    archive=a.out/'idle-compactor.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for name in FILES:z.write(base/name,'idle-compactor/'+name)
    payload=dict(schema=1,version=a.version,sequence=time.time_ns(),bytes=archive.stat().st_size,
                 sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                 url='https://github.com/'+u.REPO+'/releases/download/v'+a.version+'/idle-compactor.zip')
    signed=subprocess.run(['/usr/bin/openssl','dgst','-sha256','-sign',str(a.key)],input=u.canonical(payload),capture_output=True,check=True)
    raw=json.dumps(dict(payload=payload,signature=base64.b64encode(signed.stdout).decode()),sort_keys=True).encode()
    # Validate using the distributed trust key before uploading anything.
    u.verify_envelope(raw,(base/'release-public.pem').read_text())
    (a.out/'update.json').write_bytes(raw)
    notes=a.out/'release-notes.md'
    notes.write_text('Signed automatic update for Codex Idle Compactor '+a.version+'.\n\n'
                     'Compatible local desktop tasks are detected by protocol capabilities. Ordinary ChatGPT web/Classic chats and remote/cloud tasks are not supported. Existing compaction settings and limits are preserved.\n')
    if a.publish:
        subprocess.run(['gh','release','create','v'+a.version,'--repo',u.REPO,'--draft',
                        '--title','Idle Compactor '+a.version,'--notes-file',str(notes),str(archive),str(a.out/'update.json')],check=True)
        subprocess.run(['gh','release','edit','v'+a.version,'--repo',u.REPO,'--draft=false','--latest'],check=True)
    print(json.dumps(payload,indent=2))

if __name__=='__main__':main()
