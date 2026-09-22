#!/usr/bin/env python3
"""Small recovery bootstrap. App adapters and the update manager live in releases."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

root=Path(__file__).resolve().parent

def state():return json.loads((root/'active.json').read_text())
def save(s):
    p=root/'active.json.tmp';p.write_text(json.dumps(s));os.chmod(p,0o600);os.replace(p,root/'active.json')

if __name__=='__main__':
    while True:
        try:
            s=state();version=s['current']
            if not re.fullmatch(r'\d+\.\d+\.\d+',version):raise ValueError('Invalid release pointer')
            started=time.monotonic()
            p=subprocess.run([sys.executable,str(root/'releases'/version/'launcher.py'),'--state',str(root),'serve'])
            current=state()
            if current.get('current')!=version:continue
            if p.returncode!=0:
                fallback=current.get('last_good')
                if fallback==version:fallback=current.get('previous')
                if fallback and fallback!=version:
                    current['current']=fallback;current['rejected_versions']=list(set(current.get('rejected_versions',[])+[version]));save(current)
            time.sleep(30)
        except Exception:
            # launchd restarts this bootstrap. No model work originates here.
            time.sleep(30)
