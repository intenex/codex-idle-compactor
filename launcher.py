#!/usr/bin/env python3
"""Update manager and compactor supervisor; stays alive when app integration fails."""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import updater as u

VERSION='0.2.5'

def check(root,force=False):
    status=u.read_json(root/'update-status.json')
    now=time.time()
    if not force and now<status.get('next_check',0):return None
    status.update(last_check=now,next_check=now+u.INTERVAL,status='checking')
    u.save(root/'update-status.json',status)  # durable request throttle, including failures
    try:
        raw=u.download(u.FEED,65536)
        candidate=u.stage(root,raw,(root/'release-public.pem').read_text())
        status.update(status='staged' if candidate else 'current',error=None)
        if candidate:status['available_version']=candidate['version']
        else:status.pop('available_version',None)
        u.save(root/'update-status.json',status)
        return candidate
    except Exception as e:
        status.update(status='retry-later',error=str(e) if isinstance(e,u.UpdateError) else type(e).__name__)
        u.save(root/'update-status.json',status)
        return None

def stop_worker(root,worker):
    (root/'restart-request').touch(mode=0o600)
    # Let an in-flight compaction finish or become unresolved in the durable ledger.
    # Unknown outcomes still block further dispatch after an upgrade.
    deadline=time.monotonic()+240
    while worker.poll() is None and time.monotonic()<deadline:time.sleep(1)
    if worker.poll() is None:
        # Do not kill an operation simply to install an update. Try again later.
        return False
    (root/'restart-request').unlink(missing_ok=True)
    return True

def serve(root):
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (root/'supervisor.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        settings=u.read_json(root/'installation.json')
        executable=Path(__file__).parent/'idle_compactor.py'
        cmd=[sys.executable,str(executable),'--state',str(root)]
        if settings.get('codex_home'):cmd+=['--codex-home',settings['codex_home']]
        if settings.get('app'):cmd+=['--app',settings['app']]
        cmd+=['watch']
        worker=None;started=0;failures=0;pending=None
        (root/'restart-request').unlink(missing_ok=True)
        try:
            while True:
                force=(root/'check-update-now').exists()
                if force:(root/'check-update-now').unlink(missing_ok=True)
                # Checks happen independently of whether the desktop app is installed,
                # open, or protocol-compatible, and even if the compactor crashed.
                pending=pending or check(root,force)
                if pending:
                    if worker is None or stop_worker(root,worker):
                        u.activate(root,pending)
                        return  # bootstrap loads updated manager as well as updated worker
                if worker is None or worker.poll() is not None:
                    if worker is not None:
                        failures+=1
                        if time.time()-started<60 and failures>=2 and u.rollback(root,VERSION):return
                    started=time.time()
                    worker=subprocess.Popen(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                health=u.read_json(root/'worker-health.json')
                if health.get('version')==VERSION and health.get('at',0)>=started and time.time()-started>=45 and health.get('status') in ('running','compatibility-blocked'):
                    active=u.read_json(root/'active.json')
                    if active.get('current')==VERSION and active.get('last_good')!=VERSION:
                        active['last_good']=VERSION;u.save(root/'active.json',active);u.cleanup(root)
                elif time.time()-started>120 and health.get('at',0)<started:
                    if stop_worker(root,worker) and u.rollback(root,VERSION):return
                u.save(root/'supervisor-health.json',dict(at=time.time(),version=VERSION,worker_pid=worker.pid,
                       worker_state=health.get('status','starting'),auto_updates=True))
                time.sleep(5)
        finally:
            if worker is not None and worker.poll() is None:
                # Supervisor termination (uninstall/restart): reap its child. An
                # interrupted request remains reserved, so it is never retried.
                worker.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):worker.wait(timeout=5)


def main():
    p=argparse.ArgumentParser();p.add_argument('--state',type=Path,default=Path.home()/'Library/Application Support/Codex Idle Compactor')
    p.add_argument('command',choices=['serve','check','status']);a=p.parse_args();root=a.state.resolve()
    if a.command=='serve':serve(root)
    elif a.command=='check':
        (root/'check-update-now').touch(mode=0o600)
        print('Update check requested; the running supervisor will perform it.')
    else:print(json.dumps(dict(release=u.read_json(root/'active.json'),updates=u.read_json(root/'update-status.json'),
                             supervisor=u.read_json(root/'supervisor-health.json'),
                             worker=u.read_json(root/'worker-health.json')),indent=2))

if __name__=='__main__':
    signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
    main()
