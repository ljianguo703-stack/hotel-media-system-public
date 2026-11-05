import argparse
import importlib.util
import json
import io
from urllib.error import HTTPError
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
def module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value); return value
b = module('bootstrap_test', 'deploy/bootstrap.py')
p = module('package_test', 'tools/build_release.py')
tv = module('provision_test', 'deploy/android/provision.py')
nas = module('nas_initialize_test', 'deploy/nas/initialize.py')


class PortableInstallationTests(unittest.TestCase):
    def args(self, **extra):
        return argparse.Namespace(profile='full', resume=False, operator='hotel-tv', lan_ip='192.168.1.10', hotel='技术验证', timezone='UTC', **extra)

    def test_distinct_installs_generate_independent_credentials_and_resume_does_not_rotate(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.object(b, 'STATE', base / 'state.json'), patch.object(b, 'DATA', base / 'data'):
                first = b.setup_state(self.args())
                self.assertEqual((base / 'state.json').stat().st_mode & 0o777, 0o600)
                with self.assertRaises(RuntimeError): b.setup_state(self.args())
                args = self.args(); args.resume = True
                self.assertEqual(b.setup_state(args), first)
                args.profile = 'core'
                with self.assertRaises(RuntimeError): b.setup_state(args)
                (base / 'state.json').unlink()
                second = b.setup_state(self.args())
                self.assertNotEqual(first['tv_token'], second['tv_token'])
                self.assertNotEqual(first['nas_password'], second['nas_password'])
                self.assertRegex(first['staff_pin'], r'^\d{6}$')

    def test_existing_business_database_is_not_overwritten_by_first_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); data = base / 'data'; data.mkdir()
            database = data / 'events.sqlite3'; database.write_bytes(b'existing-business')
            with patch.object(b, 'STATE', base / 'state.json'), patch.object(b, 'DATA', data):
                with self.assertRaises(RuntimeError): b.setup_state(self.args())
            self.assertEqual(database.read_bytes(), b'existing-business')
            self.assertFalse((base / 'state.json').exists())

    def test_seed_has_no_workstation_data_or_test_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); source = base / 'source'; stage = base / 'stage'; stage.mkdir()
            for folder in ('server', 'admin', 'deploy/ubuntu'): (source / folder).mkdir(parents=True)
            for path in ('server/server.py', 'admin/index.html', 'deploy/ubuntu/hotel-tv.service', 'LICENSE', 'NOTICE'):
                (source / path).write_text('program')
            for folder in ('server/data', 'server/media', 'server/__pycache__', 'admin/tests'): (source / folder).mkdir()
            for path in ('server/data/events.sqlite3', 'server/media/private.mp4', 'server/test_private.py', 'admin/tests/site.json'):
                (source / path).write_text('private')
            with patch.object(b, 'SOURCE', source):
                b.prepare_seed(stage, {'hotel': '空白新店', 'tv_token': 'random-first-device'})
            self.assertFalse((stage / 'server/data').exists()); self.assertFalse((stage / 'server/media').exists())
            self.assertFalse((stage / 'server/test_private.py').exists()); self.assertFalse((stage / 'admin/tests').exists())
            self.assertEqual(json.loads((stage / 'seed/catalog.json').read_text())['items'], [])
            self.assertEqual(list((stage / 'seed/media').iterdir()), [])
            self.assertEqual(json.loads((stage / 'seed/devices.json').read_text())['devices']['tv-001']['token'], 'random-first-device')

    def test_package_refuses_private_material_and_path_escape(self):
        p.validate_names(['server/server.py', 'deploy/android/hotel-defaults.example.json', 'README.md'])
        for path in ['local/a', 'server/data/catalog.json', 'server/media/test.mp4', 'app/src/main/assets/hotel-defaults.json', '../outside', '/etc/passwd', 'key.jks', 'something.apk', '.env']:
            with self.subTest(path=path), self.assertRaises(ValueError): p.validate_names([path])

    def test_cold_jellyfin_connection_retry_is_bounded_and_auth_errors_stop(self):
        with patch.object(nas.opener, 'open', side_effect=[ConnectionResetError(), io.BytesIO(b'{"ready":true}')]) as call, patch.object(nas.time, 'sleep'):
            self.assertEqual(nas.api('/Startup/Configuration', {}), {'ready': True})
            self.assertEqual(call.call_count, 2)
        with patch.object(nas.opener, 'open', side_effect=HTTPError('http://127.0.0.1',401,'auth',{},None)) as call, patch.object(nas.time, 'sleep'):
            with self.assertRaises(HTTPError): nas.api('/Users/AuthenticateByName', {})
            self.assertEqual(call.call_count, 1)
        with patch.object(nas.opener, 'open', side_effect=ConnectionResetError()) as call, patch.object(nas.time, 'sleep'):
            with self.assertRaises(ConnectionResetError): nas.api('/Startup/Configuration', {})
            self.assertEqual(call.call_count, 12)

    def test_tv_tool_requires_explicit_lan_target_and_does_not_assemble_reset_commands(self):
        self.assertEqual(tv.target_address('192.168.1.50'), '192.168.1.50')
        for address in ('127.0.0.1', '0.0.0.0', '8.8.8.8', '192.168.1.0/24', '192.168.1.50;reboot'):
            with self.subTest(address=address), self.assertRaises(ValueError): tv.target_address(address)
        command = tv.broadcast({'provision_token': 'test-credential', 'provision_status': True})
        self.assertIn('com.xjg.hoteltv/.guard.ConfigReceiver', command)
        self.assertNotIn('reboot', command); self.assertNotIn('pm clear', command)


if __name__ == '__main__': unittest.main()
