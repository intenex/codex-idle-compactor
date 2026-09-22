#!/usr/bin/env python3
"""Always control the installed version, even from an old download folder."""
import json
import os
from pathlib import Path
import sys
root=Path.home()/'Library/Application Support/Codex Idle Compactor'
if __name__=='__main__':
    current=json.loads((root/'active.json').read_text())['current']
    command=sys.argv[1:]
    if command and command[0] in ('update','update-status'):
        script=root/'releases'/current/'launcher.py'
        args=['check' if command[0]=='update' else 'status']
    else:
        script=root/'releases'/current/'idle_compactor.py';args=command or ['status']
    os.execv(sys.executable,[sys.executable,str(script),'--state',str(root)]+args)
