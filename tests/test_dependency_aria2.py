"""A verified prebuilt aria2 must avoid the external APK build during cached installs."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dependency_smart_download', ROOT / 'deploy/nas/smart_download.py')
smart = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smart)


class PrebuiltAria2Tests(unittest.TestCase):
    def prepare(self, identity, info):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        args = argparse.Namespace(media=str(root / 'media'), root=str(root / 'nas'),
                                  stacks=str(root / 'stacks'), uid=os.getuid(), gid=os.getgid(),
                                  timezone='UTC', with_prowlarr=False)
        with patch.dict(os.environ, {'HOTEL_PREBUILT_ARIA2_ID': identity}), \
             patch.object(smart, 'pull_image'), patch.object(smart, 'run') as run, \
             patch.object(smart, 'native_platform', return_value='linux/amd64'), \
             patch.object(smart.subprocess, 'check_output', return_value=json.dumps([info])):
            smart.prepare(args)
            return run.call_args_list

    def test_verified_image_skips_the_network_dependent_build(self):
        image_id = 'sha256:' + 'a' * 64
        self.assertEqual(self.prepare(image_id, {'Id': image_id, 'Os': 'linux', 'Architecture': 'amd64'}), [])

    def test_wrong_identity_or_architecture_stops(self):
        image_id = 'sha256:' + 'a' * 64
        for info in [{'Id': 'sha256:' + 'b' * 64, 'Os': 'linux', 'Architecture': 'amd64'},
                     {'Id': image_id, 'Os': 'linux', 'Architecture': 'arm64'}]:
            with self.subTest(info=info), self.assertRaisesRegex(RuntimeError, '身份或架构不符'):
                self.prepare(image_id, info)

    def test_normal_install_still_builds_aria2(self):
        calls = self.prepare('', {})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[0][:2], ['docker', 'compose'])
        self.assertEqual(calls[0].args[0][-1], 'build')


if __name__ == '__main__':
    unittest.main()
