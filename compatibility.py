"""Read installed app capabilities as data; never execute bundled JavaScript."""
from __future__ import annotations
import functools
import json
from pathlib import Path
import plistlib
import re
import struct

METHODS = ('thread-owner-discovery', 'thread-stream-following-changed',
           'thread-stream-state-changed', 'thread-follower-compact-thread')

class CompatibilityError(RuntimeError):
    pass

def locate_app(preferred=None):
    paths = [Path(preferred)] if preferred else []
    paths += [Path('/Applications/ChatGPT.app'), Path('/Applications/Codex.app'),
              Path.home()/'Applications/ChatGPT.app', Path.home()/'Applications/Codex.app']
    for p in paths:
        if (p/'Contents/Resources/app.asar').is_file():
            return p
    raise CompatibilityError('No supported Electron desktop app found; ordinary ChatGPT Classic is not supported')

def app_info(app):
    try:
        p = plistlib.loads((app/'Contents/Info.plist').read_bytes())
    except (OSError, ValueError):
        raise CompatibilityError('Desktop app metadata unavailable')
    if p.get('CFBundleIdentifier') not in ('com.openai.codex', 'com.openai.chat', 'com.openai.chatgpt'):
        raise CompatibilityError('Unrecognized app identity')
    return tuple(str(p.get(k,'')) for k in ('CFBundleIdentifier','CFBundleShortVersionString','CFBundleVersion'))

def read_protocol(app):
    app = locate_app(app)
    info = app_info(app)
    archive = app/'Contents/Resources/app.asar'
    st = archive.stat()
    versions = _archive_versions(str(archive), st.st_size, st.st_mtime_ns)
    return dict(app=app, info=info, versions=versions)

@functools.lru_cache(maxsize=4)
def _archive_versions(path, size, mtime):
    # Electron ASAR Pickle header: version-independent file table and byte offsets.
    with open(path,'rb') as f:
        raw=f.read(16)
        if len(raw)!=16:
            raise CompatibilityError('Invalid app archive')
        _, header_size, _, json_size=struct.unpack('<4I',raw)
        if not 0 < json_size <= 16*1024*1024 or header_size < json_size or 8+header_size > size:
            raise CompatibilityError('Invalid app archive header')
        tree=json.loads(f.read(json_size))
        files=[]
        def walk(node,prefix=''):
            for name,meta in node.get('files',{}).items():
                p=prefix+'/'+name
                if 'files' in meta:walk(meta,p)
                elif p.endswith('.js') and '/node_modules/' not in p and ('/.vite/' in p or '/dist/' in p):
                    files.append((p,meta))
        walk(tree)
        versions={}
        consumed=0
        for name,meta in sorted(files,key=lambda x: (0 if '/build/' in x[0] else 1,x[0])):
            n=meta.get('size',0)
            if meta.get('unpacked') or not isinstance(n,int) or n>12*1024*1024:continue
            consumed+=n
            if consumed>96*1024*1024:break
            offset=8+header_size+int(meta.get('offset',-1))
            if offset<8+header_size or offset+n>size:raise CompatibilityError('Invalid app archive offset')
            f.seek(offset);s=f.read(n).decode('utf-8',errors='replace')
            for method in METHODS:
                found={int(v) for v in re.findall(r'["\x27`]'+re.escape(method)+r'["\x27`]\s*:\s*(\d+)',s)}
                if len(found)>1:raise CompatibilityError('Ambiguous IPC capability declaration')
                if found:
                    value=found.pop()
                    if method in versions and versions[method]!=value:
                        raise CompatibilityError('Conflicting IPC capability declaration')
                    versions[method]=value
            if len(versions)==len(METHODS):break
    if set(versions)!=set(METHODS):
        raise CompatibilityError('Required compaction IPC capabilities unavailable; awaiting adapter update')
    # These mutation/coordination contracts are understood. A snapshot version can
    # evolve while retaining the validated fields. Unknown mutation contracts wait
    # for a signed adapter update rather than guessing an irreversible operation.
    for method in METHODS:
        if method!='thread-stream-state-changed' and versions[method]!=1:
            raise CompatibilityError('New IPC action contract requires an adapter update')
    return versions
