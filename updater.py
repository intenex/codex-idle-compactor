"""Signed release verification and staging; independent of desktop compatibility."""
from __future__ import annotations
import base64
import contextlib
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

REPO = 'intenex/codex-idle-compactor'
FEED = 'https://github.com/'+REPO+'/releases/latest/download/update.json'
MAX_DOWNLOAD = 5*1024*1024
MAX_UNPACKED = 20*1024*1024
INTERVAL = 1800
VERSION_PATTERN = r'\d+\.\d+\.\d+'

class UpdateError(RuntimeError):pass

def canonical(obj):return json.dumps(obj,sort_keys=True,separators=(',',':')).encode()

def read_json(path, default=None):
    try:return json.loads(path.read_text())
    except FileNotFoundError:return {} if default is None else default

def save(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    tmp=path.with_name(path.name+'.tmp-'+str(os.getpid()))
    with tmp.open('w') as f:
        os.chmod(tmp,0o600);json.dump(obj,f,sort_keys=True);f.write('\n');f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)

def valid_url(url):
    p=urllib.parse.urlparse(url)
    if p.scheme!='https' or p.username or p.password or p.port not in (None,443):return False
    return p.hostname in ('github.com','release-assets.githubusercontent.com','objects.githubusercontent.com')

class Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if not valid_url(newurl):raise UpdateError('Untrusted download redirect')
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def download(url,limit):
    if not valid_url(url):raise UpdateError('Untrusted download URL')
    request=urllib.request.Request(url,headers={'User-Agent':'Codex-Idle-Compactor-Updater/1','Accept':'application/octet-stream'})
    deadline=time.monotonic()+60
    data=bytearray()
    with urllib.request.build_opener(Redirects()).open(request,timeout=15) as response:
        if not valid_url(response.geturl()):raise UpdateError('Untrusted final download URL')
        while True:
            if time.monotonic()>deadline:raise UpdateError('Download deadline exceeded')
            part=response.read(min(65536,limit+1-len(data)))
            if not part:break
            data.extend(part)
            if len(data)>limit:raise UpdateError('Download size limit exceeded')
    return bytes(data)

def verify_envelope(raw, public_key):
    if len(raw)>65536:raise UpdateError('Manifest too large')
    try:
        env=json.loads(raw);payload=env['payload'];signature=base64.b64decode(env['signature'],validate=True)
    except (ValueError,KeyError,TypeError):raise UpdateError('Malformed signed manifest')
    with tempfile.TemporaryDirectory() as td:
        p=Path(td);(p/'payload').write_bytes(canonical(payload));(p/'sig').write_bytes(signature)
        (p/'key.pem').write_text(public_key)
        cmd=['/usr/bin/openssl','dgst','-sha256','-verify',str(p/'key.pem'),'-signature',str(p/'sig'),str(p/'payload')]
        check=subprocess.run(cmd,capture_output=True,timeout=15)
        if check.returncode:raise UpdateError('Release signature verification failed')
    if payload.get('schema')!=1 or not re.fullmatch(VERSION_PATTERN,str(payload.get('version',''))):
        raise UpdateError('Unsupported release schema or version')
    if type(payload.get('sequence')) is not int or payload['sequence']<1:raise UpdateError('Invalid sequence')
    if type(payload.get('bytes')) is not int or not 0<payload['bytes']<=MAX_DOWNLOAD:raise UpdateError('Invalid package size')
    if not re.fullmatch(r'[a-f0-9]{64}',str(payload.get('sha256',''))):raise UpdateError('Invalid digest')
    expected='https://github.com/'+REPO+'/releases/download/v'+payload['version']+'/idle-compactor.zip'
    if payload.get('url')!=expected:raise UpdateError('Unexpected release URL')
    return payload

def unpack(data, target):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        infos=z.infolist()
        if len(infos)>80 or sum(x.file_size for x in infos)>MAX_UNPACKED:raise UpdateError('Archive limits exceeded')
        seen=set()
        for x in infos:
            parts=x.filename.split('/')
            if len(parts)!=2 or parts[0]!='idle-compactor' or parts[1] in ('','.','..'):
                raise UpdateError('Invalid archive path')
            name=parts[1]
            if name in seen or '/' in name or '\\' in name or stat.S_ISLNK(x.external_attr>>16):
                raise UpdateError('Duplicate or unsafe archive member')
            if Path(name).suffix not in ('.py','.json','.md','.command','.pem'):
                raise UpdateError('Unsupported archive member')
            seen.add(name)
            if x.is_dir():raise UpdateError('Unexpected directory entry')
            (target/name).write_bytes(z.read(x))
            (target/name).chmod(0o700 if name.endswith('.command') else 0o600)
        required={'idle_compactor.py','compatibility.py','launcher.py','updater.py','boot.py','install.py','release.json'}
        if not required<=seen:raise UpdateError('Incomplete release package')

def stage(root,raw,public_key,fetch=download):
    payload=verify_envelope(raw,public_key)
    active=read_json(root/'active.json')
    sequence=payload['sequence'];version=payload['version']
    if sequence<active.get('highest_sequence',0):raise UpdateError('Release rollback rejected')
    if version in active.get('rejected_versions',[]):raise UpdateError('Previously unhealthy release rejected')
    if sequence==active.get('highest_sequence',0) and version!=active.get('current'):
        raise UpdateError('Release sequence reuse rejected')
    if version==active.get('current'):return None
    data=fetch(payload['url'],MAX_DOWNLOAD)
    if len(data)!=payload['bytes'] or not hmac.compare_digest(hashlib.sha256(data).hexdigest(),payload['sha256']):
        raise UpdateError('Package digest mismatch')
    releases=root/'releases';releases.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=releases/version
    with tempfile.TemporaryDirectory(prefix='.staging-',dir=releases) as td:
        staged=Path(td);unpack(data,staged)
        meta=read_json(staged/'release.json')
        if meta.get('version')!=version:raise UpdateError('Package version mismatch')
        test=subprocess.run([sys.executable,str(staged/'idle_compactor.py'),'self-check'],
                            capture_output=True,text=True,timeout=30)
        try:health=json.loads(test.stdout)
        except ValueError:raise UpdateError('Release self-check failed')
        if test.returncode or health!={'version':version,'healthy':True}:raise UpdateError('Release self-check failed')
        for file in ('launcher.py','updater.py','boot.py'):
            compile((staged/file).read_text(),file,'exec')
        (staged/'verified-manifest.json').write_bytes(raw)
        if target.exists():
            # Only a previously staged, signed identical package may be reused.
            if (target/'verified-manifest.json').read_bytes()!=raw:raise UpdateError('Conflicting staged release')
        else:
            os.rename(staged,target)
    return dict(version=version,sequence=sequence)

def activate(root,candidate):
    active=read_json(root/'active.json')
    old=active.get('current')
    active.update(current=candidate['version'],previous=old,last_good=active.get('last_good',old),
                  highest_sequence=candidate['sequence'],activated_at=time.time())
    save(root/'active.json',active)

def rollback(root,failed):
    active=read_json(root/'active.json')
    if active.get('current')!=failed:return False
    previous=active.get('last_good')
    if not previous or previous==failed:previous=active.get('previous')
    if not previous or previous==failed:return False
    active['rejected_versions']=list(set(active.get('rejected_versions',[])+[failed]))
    active.update(current=previous,previous=None,last_error='Rolled back unhealthy release '+failed)
    save(root/'active.json',active)
    return True

def cleanup(root):
    active=read_json(root/'active.json')
    keep={active.get(k) for k in ('current','previous','last_good')}
    # Preserve only known signed releases. Never remove unrelated local files.
    for p in (root/'releases').iterdir():
        if p.is_dir() and re.fullmatch(VERSION_PATTERN,p.name) and p.name not in keep and (p/'verified-manifest.json').exists():
            shutil.rmtree(p)
