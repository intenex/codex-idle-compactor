import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch
import updater as u
import launcher

class Updates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys=tempfile.TemporaryDirectory();p=Path(cls.keys.name)
        cls.key=p/'private.pem';cls.pub=p/'public.pem'
        subprocess.run(['/usr/bin/openssl','genrsa','-out',str(cls.key),'2048'],check=True,capture_output=True)
        subprocess.run(['/usr/bin/openssl','rsa','-in',str(cls.key),'-pubout','-out',str(cls.pub)],check=True,capture_output=True)
        cls.public=cls.pub.read_text()
    @classmethod
    def tearDownClass(cls):cls.keys.cleanup()
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.data=self.package('0.2.0')
    def tearDown(self):self.tmp.cleanup()
    def package(self,version,extra=None,bad=False):
        files={name:'# fixture\n' for name in ['compatibility.py','launcher.py','updater.py','boot.py','install.py']}
        files['idle_compactor.py']='raise RuntimeError()' if bad else 'print('+repr(json.dumps({'version':version,'healthy':True}))+')'
        files['release.json']=json.dumps({'version':version})
        files.update(extra or {})
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w',zipfile.ZIP_DEFLATED) as z:
            for name,s in files.items():z.writestr('idle-compactor/'+name,s)
        return b.getvalue()
    def manifest(self,data=None,version='0.2.0',sequence=2,**changes):
        data=self.data if data is None else data
        p=dict(schema=1,version=version,sequence=sequence,bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),
               url='https://github.com/'+u.REPO+'/releases/download/v'+version+'/idle-compactor.zip')
        p.update(changes)
        r=subprocess.run(['/usr/bin/openssl','dgst','-sha256','-sign',str(self.key)],input=u.canonical(p),capture_output=True,check=True)
        return json.dumps({'payload':p,'signature':base64.b64encode(r.stdout).decode()}).encode()
    def test_signed_update_stages_then_activates_preserving_settings(self):
        u.save(self.root/'active.json',dict(current='0.1.0',last_good='0.1.0',highest_sequence=1))
        u.save(self.root/'config.json',dict(enabled=True,max_per_month=20));(self.root/'ledger.sqlite').write_bytes(b'ledger fixture')
        candidate=u.stage(self.root,self.manifest(),self.public,lambda *_:self.data)
        self.assertEqual(u.read_json(self.root/'active.json')['current'],'0.1.0')
        u.activate(self.root,candidate)
        self.assertEqual(u.read_json(self.root/'active.json')['current'],'0.2.0')
        self.assertEqual(u.read_json(self.root/'config.json'),dict(enabled=True,max_per_month=20))
        self.assertEqual((self.root/'ledger.sqlite').read_bytes(),b'ledger fixture')
    def test_modified_manifest_rejected(self):
        m=json.loads(self.manifest());m['payload']['version']='99.0.0'
        with self.assertRaises(u.UpdateError):u.verify_envelope(json.dumps(m).encode(),self.public)
    def test_unsigned_manifest_rejected(self):
        with self.assertRaises(u.UpdateError):u.verify_envelope(b'{}',self.public)
    def test_corrupt_archive_rejected(self):
        with self.assertRaises(u.UpdateError):u.stage(self.root,self.manifest(),self.public,lambda *_:b'evil')
    def test_size_cap_rejected(self):
        with self.assertRaises(u.UpdateError):u.verify_envelope(self.manifest(bytes=u.MAX_DOWNLOAD+1),self.public)
    def test_manifest_rollback_rejected(self):
        u.save(self.root/'active.json',dict(current='0.3.0',highest_sequence=3))
        with self.assertRaises(u.UpdateError):u.stage(self.root,self.manifest(),self.public,lambda *_:self.data)
    def test_reused_sequence_rejected(self):
        u.save(self.root/'active.json',dict(current='0.1.0',highest_sequence=2))
        with self.assertRaises(u.UpdateError):u.stage(self.root,self.manifest(),self.public,lambda *_:self.data)
    def test_failed_selfcheck_does_not_activate(self):
        data=self.package('0.2.0',bad=True)
        with self.assertRaises(u.UpdateError):u.stage(self.root,self.manifest(data),self.public,lambda *_:data)
        self.assertFalse((self.root/'active.json').exists())
    def test_zip_traversal_rejected(self):
        data=self.package('0.2.0',extra={'../../escaped.py':'bad'})
        with self.assertRaises(u.UpdateError):u.stage(self.root,self.manifest(data),self.public,lambda *_:data)
        self.assertFalse((self.root.parent/'escaped.py').exists())
    def test_symlink_rejected(self):
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w') as z:
            x=zipfile.ZipInfo('idle-compactor/evil.py');x.create_system=3;x.external_attr=0o120777<<16;z.writestr(x,'/tmp/evil')
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(u.UpdateError):u.unpack(b.getvalue(),Path(td))
    def test_duplicate_member_rejected(self):
        import warnings
        b=io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            with zipfile.ZipFile(b,'w') as z:
                z.writestr('idle-compactor/a.py','1');z.writestr('idle-compactor/a.py','2')
        with self.assertRaises(u.UpdateError):u.unpack(b.getvalue(),self.root)
    def test_offline_check_keeps_current_and_throttles(self):
        u.save(self.root/'active.json',dict(current='0.1.0',highest_sequence=1))
        with patch.object(u,'download',side_effect=OSError('offline')) as d:
            self.assertIsNone(launcher.check(self.root));self.assertIsNone(launcher.check(self.root));self.assertEqual(d.call_count,1)
        self.assertEqual(u.read_json(self.root/'active.json')['current'],'0.1.0')
        self.assertEqual(u.read_json(self.root/'update-status.json')['status'],'retry-later')
    def test_runtime_rollback_and_later_fix(self):
        u.save(self.root/'active.json',dict(current='0.2.0',previous='0.1.0',last_good='0.1.0',highest_sequence=2))
        self.assertTrue(u.rollback(self.root,'0.2.0'))
        self.assertEqual(u.read_json(self.root/'active.json')['current'],'0.1.0')
        with self.assertRaises(u.UpdateError):u.stage(self.root,self.manifest(),self.public,lambda *_:self.data)
        data=self.package('0.2.1');candidate=u.stage(self.root,self.manifest(data,'0.2.1',3),self.public,lambda *_:data)
        u.activate(self.root,candidate);self.assertEqual(u.read_json(self.root/'active.json')['current'],'0.2.1')
    def test_github_update_source_pinned(self):
        with self.assertRaises(u.UpdateError):u.verify_envelope(self.manifest(url='https://example.com/update.zip'),self.public)
        self.assertFalse(u.valid_url('http://github.com/file'))
        self.assertFalse(u.valid_url('https://github.com.evil.example/file'))
        self.assertFalse(u.valid_url('https://user:secret@github.com/file'))
    def test_signed_version_mismatch(self):
        data=self.package('9.0.0')
        with self.assertRaises(u.UpdateError):u.stage(self.root,self.manifest(data),self.public,lambda *_:data)
    def test_bootstrap_recovers_from_broken_manager_process(self):
        import shutil,time,sys
        releases=self.root/'releases';(releases/'0.1.0').mkdir(parents=True);(releases/'0.2.0').mkdir()
        (releases/'0.2.0/launcher.py').write_text('raise RuntimeError("fixture failure")')
        (releases/'0.1.0/launcher.py').write_text('pass')
        shutil.copy2(Path(__file__).parent/'boot.py',self.root/'boot.py')
        u.save(self.root/'active.json',dict(current='0.2.0',previous='0.1.0',last_good='0.1.0',highest_sequence=2))
        proc=subprocess.Popen([sys.executable,str(self.root/'boot.py')],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            deadline=time.monotonic()+5
            while time.monotonic()<deadline and u.read_json(self.root/'active.json')['current']=='0.2.0':time.sleep(.02)
            self.assertEqual(u.read_json(self.root/'active.json')['current'],'0.1.0')
        finally:
            proc.terminate();proc.wait(timeout=5)

    def test_same_version_no_download(self):
        u.save(self.root/'active.json',dict(current='0.2.0',highest_sequence=2))
        with patch.object(u,'download') as download:
            self.assertIsNone(u.stage(self.root,self.manifest(),self.public,download));download.assert_not_called()

if __name__=='__main__':unittest.main()
