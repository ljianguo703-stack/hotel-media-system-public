"""First-install randomness is persisted; upgrades and administrator choices stay authoritative."""
from pathlib import Path
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch

import test_home
from management import Store
import home_backgrounds


class BundledBackgroundE2E(test_home.HomeE2E):
    def test_fresh_install_is_shared_persistent_and_actual_image_is_served(self):
        state = self.home_admin()
        images = state['default_backgrounds']
        self.assertEqual(len(images), 4)
        chosen = self.catalog()['home']['background']
        self.assertIn(chosen, [image['path'] for image in images])
        status, raw = self.tv_get(chosen)
        self.assertEqual(status, 200)
        self.assertTrue(raw.startswith(b'\x89PNG\r\n\x1a\n'))
        revision = self.state()['revision']
        with patch('operations_home.secrets.choice') as choose:
            again = Store(self.data, self.media, self.config, [self.media, self.external])
            choose.assert_not_called()
        with again.db() as db:
            self.assertEqual(again.home_settings(db)['background'], chosen)
        self.assertEqual(again.state()['revision'], revision)
        self.assertEqual(self.catalog()['home']['background'], chosen)

    def home_admin(self):
        return self.request('/api/admin/operations?section=home', auth=True)[2]

    def test_choose_bundled_or_upload_custom_and_clear_are_kept_after_restart(self):
        home = self.home_admin()
        menu = home['menu']
        original = dict(home['home'])
        for image in home['default_backgrounds']:
            body = {**original, 'background': image['path']}
            self.assertEqual(self.op('home', 'home_save', **body)[0], 200)
            self.assertEqual(self.catalog()['home']['background'], image['path'])
            self.assertEqual(self.home_admin()['menu'], menu)
        status, _, custom = self.upload('my-background.png', self.tv_get(home['default_backgrounds'][0]['path'])[1])
        self.assertEqual(status, 201)
        for selected in (custom['path'], ''):
            self.assertEqual(self.op('home', 'home_save', **{**original, 'background': selected})[0], 200)
            with patch('operations_home.secrets.choice') as choose:
                again = Store(self.data, self.media, self.config)
                choose.assert_not_called()
            with again.db() as db:
                self.assertEqual(again.home_settings(db)['background'], selected)

    def test_legacy_empty_settings_are_not_randomized_during_upgrade(self):
        with self.store.db() as db:
            db.execute("DELETE FROM kv WHERE key='home_screen'")
        with patch('operations_home.secrets.choice') as choose:
            again = Store(self.data, self.media, self.config)
            choose.assert_not_called()
        with again.db() as db:
            self.assertEqual(again.home_settings(db)['background'], '')
        self.assertEqual(len(again.home_admin_state()['default_backgrounds']), 4)

    def test_choices_keep_existing_authorization_and_stale_write_protection(self):
        image = self.home_admin()['default_backgrounds'][0]
        viewer = self.login_as('background-viewer', 'viewer')
        self.assertEqual(self.op('home', 'home_save', headers=viewer, background=image['path'])[0], 403)
        revision = self.state()['revision']
        self.assertEqual(self.op('home', 'home_save', background=image['path'])[0], 200)
        response = self.request('/api/admin/operations', {'kind': 'home', 'action': 'home_save', 'background': image['path'], 'revision': revision}, auth=True)
        self.assertEqual(response[0], 409)


class BackgroundFiles(unittest.TestCase):
    def test_read_only_media_does_not_make_the_backend_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('home_backgrounds.tempfile.mkstemp', side_effect=PermissionError('read-only media')):
                with self.assertLogs('home_backgrounds', level='WARNING'):
                    self.assertEqual(home_backgrounds.prepare_backgrounds(tmp), [])

    def test_missing_resource_does_not_break_startup_and_does_not_escape_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(home_backgrounds, 'SOURCE', root / 'missing'):
                self.assertEqual(home_backgrounds.prepare_backgrounds(root / 'media'), [])
            media, outside = root / 'media', root / 'outside'
            media.mkdir(exist_ok=True); outside.mkdir()
            (media / '_home').symlink_to(outside, target_is_directory=True)
            self.assertEqual(home_backgrounds.prepare_backgrounds(media), [])
            self.assertEqual(list(outside.iterdir()), [])

    def test_seed_copies_real_public_assets_and_never_overwrites_existing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp)
            images = home_backgrounds.prepare_backgrounds(media)
            self.assertEqual(len(images), 4)
            target = media / '_home/defaults/01-coast.png'
            self.assertEqual(hashlib.sha256(target.read_bytes()).digest(), hashlib.sha256((home_backgrounds.SOURCE / target.name).read_bytes()).digest())
            target.write_bytes(b'owner-managed-existing-file')
            home_backgrounds.prepare_backgrounds(media)
            self.assertEqual(target.read_bytes(), b'owner-managed-existing-file')


if __name__ == '__main__':
    unittest.main()
