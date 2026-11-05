import importlib.util
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('build_run_test',ROOT/'tools/build_run.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class BundleTests(unittest.TestCase):
    def archive(self,root,unsafe=None):
        path=root/'payload.tar.gz'
        with tarfile.open(path,'w:gz') as t:
            entries={'VERSION':b'test-version\n','install.sh':b'#!/bin/sh\n','deploy/bootstrap.py':b'print("standalone plan verified")\n'}
            entries['deploy/ensure-runtime.sh']=(ROOT/'deploy/ensure-runtime.sh').read_bytes()
            if unsafe:entries[unsafe]=b'private'
            for name,data in entries.items():
                info=tarfile.TarInfo(name);info.size=len(data);t.addfile(info,io.BytesIO(data))
        return path

    def test_real_wrapper_plan_and_checksum_tamper_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);out=root/'installer.run'
            m.build(self.archive(root),out,'test-version')
            result=subprocess.run(['bash',str(out),'--plan'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr);self.assertIn('standalone plan verified',result.stdout)
            out.write_bytes(out.read_bytes()[:-1]+b'x')
            result=subprocess.run(['bash',str(out),'--plan'],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0);self.assertIn('checksum mismatch',result.stderr)

    def test_private_files_and_version_conflicts_are_never_wrapped(self):
        for private in ['../escape','server/data/events.sqlite3','.env','local/credentials.json']:
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                with self.assertRaises(ValueError):m.build(self.archive(root,private),root/'bundle.run','test-version')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with self.assertRaises(ValueError):m.build(self.archive(root),root/'bundle.run','wrong-version')
