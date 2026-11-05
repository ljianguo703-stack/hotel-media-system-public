import contextlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from management import Store, password_hash

spec = importlib.util.spec_from_file_location('password_manager', ROOT / 'deploy/passwords/manage.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


class PasswordManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.data = self.root / 'data'; self.media = self.root / 'media'; self.media.mkdir()
        self.config = self.root / 'devices.json'; self.config.write_text('{"devices":{}}')
        self.store = Store(self.data, self.media, self.config, [self.media])
        with self.store.db() as db: db.execute('UPDATE users SET password=?', (password_hash('old-local-password'),))
        self.patches = [patch.object(m, 'DATA', self.data), patch.object(m, 'hotel_identity', contextlib.nullcontext),
                        patch.object(m.Manager, 'status', return_value={'users': [{'name': 'admin', 'enabled': True}],
                            'services': [{'id': key, 'installed': True} for key in m.LABELS]})]
        for p in self.patches: p.start()
        self.manager = m.Manager()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def body(self, **values): return {'action': 'save', 'admin_username': 'admin', 'passwords': values}

    def test_full_form_validation_precedes_any_password_change(self):
        for values in [{'admin': 'new-valid-password', 'tv_pin': '123'}, {'admin': 'new-valid-password', 'unknown': 'secret'},
                       {'admin': 'too-short'}, {'admin': 'contains\nnewline'}, {'tv_pin': '１２３４５６'}, {}]:
            with self.subTest(values=list(values)), patch.object(self.manager, 'set_admin') as change:
                with self.assertRaises(m.Failure): self.manager.save(self.body(**values))
                change.assert_not_called()
        body = self.body(admin='new-valid-password'); body['ubuntu_password'] = 'must-not-be-accepted'
        with self.assertRaises(m.Failure): self.manager.save(body)

    def test_backend_recovery_revokes_sessions_and_preserves_other_users_and_data(self):
        with self.store.db() as db:
            db.execute('INSERT INTO users VALUES(?,?,?,1)', ('viewer', password_hash('viewer-test-password'), 'viewer'))
        old, _ = self.store.login('admin', 'old-local-password')
        viewer, _ = self.store.login('viewer', 'viewer-test-password')
        result = self.manager.save(self.body(admin='new-local-password'))
        self.assertTrue(result['ok']); self.assertIsNone(self.store.session(old))
        self.assertIsNotNone(self.store.session(viewer)); self.store.login('admin', 'new-local-password')
        with self.assertRaises(Exception): self.store.login('admin', 'old-local-password')
        with self.store.db() as db:
            audit = json.dumps([tuple(r) for r in db.execute('SELECT * FROM audit')])
        self.assertNotIn('new-local-password', audit); self.assertNotIn('old-local-password', audit)

    def test_partial_failure_is_reported_and_later_items_still_run_without_secret_leak(self):
        with patch.object(self.manager, 'set_smb', side_effect=RuntimeError('upstream-secret-should-not-leak')):
            result = self.manager.save(self.body(smb='new-smb-password', tv_pin='593284'))
        self.assertFalse(result['ok']); self.assertFalse(result['results'][0]['ok']); self.assertTrue(result['results'][1]['ok'])
        self.assertNotIn('upstream-secret', json.dumps(result)); self.assertNotIn('593284', json.dumps(result))

    def test_pin_is_atomic_private_and_never_stored_in_plaintext(self):
        self.manager.save(self.body(tv_pin='593284'))
        path = self.data / 'maintenance-pin.json'; first = json.loads(path.read_text())
        self.assertNotIn('593284', path.read_text()); self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.manager.save(self.body(tv_pin='834652')); second = json.loads(path.read_text())
        self.assertNotEqual(first['revision'], second['revision']); self.assertNotEqual(first['sha256'], second['sha256'])

    def test_download_secret_update_preserves_unrelated_keys(self):
        path = self.data / 'download-secrets.json'
        path.write_text(json.dumps({'sources': {'keep': {'api_key': 'existing-local-key'}}, 'aria2': {'secret': 'old'}}))
        revision = self.store.state()['revision']
        self.manager.sync_download_secret('aria2', 'secret', 'new-connection-secret')
        result = json.loads(path.read_text()); self.assertEqual(result['sources']['keep']['api_key'], 'existing-local-key')
        self.assertEqual(result['aria2']['secret'], 'new-connection-secret'); self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.state()['revision'], revision + 1)
        with self.assertRaises(Exception):
            self.store.save_download_settings({'secrets': {'aria2': {'secret': 'stale-ui-secret'}}}, {'name': 'admin', 'role': 'admin'}, revision)
        self.assertEqual(json.loads(path.read_text())['aria2']['secret'], 'new-connection-secret')

    def test_unprivileged_cli_does_not_read_or_change_anything(self):
        with patch.object(m.os, 'geteuid', return_value=501), patch.object(m, 'Manager') as manager:
            with self.assertRaises(m.Failure): m.main()
            manager.assert_not_called()

    def test_qbittorrent_old_and_port_namespaced_cookie_formats(self):
        for cookie, payload in [('SID=local-test-session; HttpOnly', b'Ok.'), ('QBT_SID_8098=local-test-session; HttpOnly', b'')]:
            with self.subTest(cookie=cookie.split('=')[0]), patch.object(m, 'http', return_value=(payload, {'set-cookie': cookie})):
                self.assertEqual(self.manager.qb_login('temporary-test-password'), cookie.split(';')[0])
        with patch.object(m, 'http', return_value=(b'Fails.', {})):
            self.assertIsNone(self.manager.qb_login('wrong-test-password'))


    def test_qbittorrent_rotation_also_updates_radarr_and_sonarr_clients(self):
        (self.data / 'media-automation.json').write_text(json.dumps({
            'radarr': {'url': 'http://127.0.0.1:7878', 'api_key': 'radarr-test-key'},
            'sonarr': {'url': 'http://127.0.0.1:8989', 'api_key': 'sonarr-test-key'}}))
        calls = []
        def fake(url, body=None, headers=None, form=False, method=None):
            calls.append((url, method, json.loads(json.dumps(body)) if body is not None else None, headers))
            if url.endswith('/downloadclient') and method is None:
                return [{'id': 3, 'name': 'Hotel qB', 'fields': [{'name': 'host', 'value': 'hotel-qbittorrent'}, {'name': 'password', 'value': '********'}]},
                        {'id': 4, 'name': 'Owner client', 'fields': [{'name': 'password', 'value': '********'}]}], {}
            return {}, {}
        with patch.object(m, 'http', side_effect=fake):
            self.assertEqual(self.manager.sync_arr_downloaders('rotated-test-password'), ['radarr', 'sonarr'])
        puts = [c for c in calls if c[1] == 'PUT']
        self.assertEqual([c[0] for c in puts], ['http://127.0.0.1:7878/api/v3/downloadclient/3', 'http://127.0.0.1:8989/api/v3/downloadclient/3'])
        for _, _, body, headers in puts:
            self.assertEqual(next(f['value'] for f in body['fields'] if f['name'] == 'password'), 'rotated-test-password')
            self.assertIn(headers['X-Api-Key'], ('radarr-test-key', 'sonarr-test-key'))

    def test_rotation_without_media_automation_is_unchanged_and_missing_client_is_reported(self):
        with patch.object(m, 'http') as call:
            self.assertEqual(self.manager.sync_arr_downloaders('any-test-password'), []); call.assert_not_called()
        (self.data / 'media-automation.json').write_text(json.dumps({'radarr': {'url': 'http://127.0.0.1:7878', 'api_key': 'k'}}))
        with patch.object(m, 'http', return_value=([], {})):
            with self.assertRaises(m.Failure) as error: self.manager.sync_arr_downloaders('any-test-password')
        self.assertIn('修复下载器连接', str(error.exception)); self.assertNotIn('any-test-password', str(error.exception))
        (self.data / 'media-automation.json').write_text(json.dumps({'radarr': {'url': 'http://192.0.2.9:7878', 'api_key': 'k'}}))
        with patch.object(m, 'http') as call:   # a tampered endpoint never receives the password
            with self.assertRaises(m.Failure): self.manager.sync_arr_downloaders('any-test-password')
            call.assert_not_called()


if __name__ == '__main__': unittest.main()
