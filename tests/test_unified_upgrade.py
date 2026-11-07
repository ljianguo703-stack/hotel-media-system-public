import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from management import Store
from software_packages import SoftwareError, inspect
from software_agent import Agent, RolledBack

spec = importlib.util.spec_from_file_location('unified_upgrade', ROOT / 'deploy/software/unified_upgrade.py')
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)
spec = importlib.util.spec_from_file_location('package_builder', ROOT / 'deploy/software/package.py')
packager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)


class UnifiedUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='unified-upgrade-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.payload = self.root / 'payload'
        self.payload.mkdir()
        archive = self.root / 'program.zip'
        packager.build(ROOT, archive, upgrade.TARGET, upgrade.BASES[0])
        with zipfile.ZipFile(archive) as z:
            files = json.loads(z.read('hotel-software.json'))['files']
            for name in files:
                p = self.payload / 'program' / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(z.read(name))
        self.manifest = {'format': 1, 'target': upgrade.TARGET, 'supported_bases': list(upgrade.BASES), 'files': files}
        (self.payload / 'manifest.json').write_text(json.dumps(self.manifest))
        self.releases = self.root / 'releases'
        self.releases.mkdir()
        self.previous = self.releases / 'v1-0-0-rc5-rq04'
        self.previous.mkdir()
        (self.previous / 'sentinel').write_text('old program preserved')
        self.current = self.root / 'current'
        self.current.symlink_to(self.previous)
        self.data, self.media, self.state = self.root / 'data', self.root / 'media', self.root / 'state'
        self.data.mkdir()
        self.media.mkdir()
        (self.media / 'film.mp4').write_bytes(b'keep the hotel media')
        self.config = self.root / 'devices.json'
        self.config.write_text('{"devices":{}}')
        self.store = Store(self.data, self.media, self.config, [self.media])
        self.calls = []

    def runner(self, command, **kwargs):
        self.calls.append(command)
        self.assertIn(command, [['systemctl', 'stop', 'hotel-tv'], ['systemctl', 'start', 'hotel-tv']])
        return ''

    def apply(self):
        return upgrade.apply(self.payload, self.data, self.state, self.releases, self.current,
                             runner=self.runner, deploy_lock=self.root / 'deploy.lock')

    def test_every_supported_base_is_detected_and_exact_manifest_is_generated(self):
        for index, base in enumerate(upgrade.BASES):
            with self.subTest(base=base):
                target = self.releases / base
                target.mkdir(exist_ok=True)
                self.current.unlink()
                self.current.symlink_to(target)
                self.assertEqual(upgrade.current_release(self.current, self.releases).name, base)
                artifact = self.root / ('generated-' + str(index) + '.zip')
                upgrade.write_bundle(self.payload, self.manifest, base, artifact)
                metadata = inspect(artifact, artifact.name)
                self.assertEqual(metadata['base_release'], base)
                self.assertEqual(metadata['version'], upgrade.TARGET)

    def test_archive_is_deterministic_for_failed_stage_retry(self):
        first, second = self.root / 'first.zip', self.root / 'second.zip'
        for p in [first, second]:
            upgrade.write_bundle(self.payload, self.manifest, self.previous.name, p)
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_rq04_upgrades_with_real_sqlite_snapshot_and_preserves_media_config(self):
        with patch.object(Agent, 'wait_health'):
            result = self.apply()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.current.resolve().name, upgrade.TARGET)
        self.assertTrue((self.previous / 'sentinel').exists())
        self.assertEqual((self.media / 'film.mp4').read_bytes(), b'keep the hotel media')
        self.assertEqual(self.config.read_text(), '{"devices":{}}')
        self.assertEqual(len(list(self.state.glob('*-before.sqlite3'))), 1)
        with self.store.db() as db:
            value = json.loads(db.execute("SELECT body FROM software_inventory WHERE software='hotel-tv-server'").fetchone()[0])
        self.assertEqual(value['version'], upgrade.TARGET)

    def test_start_failure_rolls_back_program_database_and_provides_retry_source(self):
        with self.store.db() as db:
            db.execute('INSERT INTO software_jobs VALUES(?,?,?)', ('sw-parent', 'running', json.dumps({'id': 'sw-parent', 'kind': 'deb', 'software_id': upgrade.PACKAGE_ID, 'package_id': 'pkg-parent', 'actor': 'admin'})))
        with patch.object(Agent, 'wait_health', side_effect=[SoftwareError('start failure'), None]):
            with self.assertRaises(RolledBack):
                self.apply()
        self.assertEqual(self.current.resolve(), self.previous)
        with self.store.db() as db:
            jobs = [json.loads(r[0]) for r in db.execute("SELECT body FROM software_jobs WHERE status='rolled_back'")]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['package_id'], 'pkg-parent')

    def test_unknown_base_fails_before_service_stop(self):
        unknown = self.releases / 'future-custom-version'
        unknown.mkdir()
        self.current.unlink()
        self.current.symlink_to(unknown)
        with self.assertRaisesRegex(upgrade.UpgradeError, 'future-custom-version'):
            self.apply()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.current.resolve(), unknown)

    def test_same_version_mismatch_refused_without_stop(self):
        target = self.releases / upgrade.TARGET
        shutil.copytree(self.payload / 'program', target)
        (target / 'admin/index.html').write_text('local program edit')
        self.current.unlink()
        self.current.symlink_to(target)
        with self.assertRaisesRegex(upgrade.UpgradeError, '程序字节不同'):
            self.apply()
        self.assertEqual(self.calls, [])

    def test_duplicate_latest_is_healthy_noop(self):
        with patch.object(Agent, 'wait_health'):
            self.apply()
            self.calls.clear()
            result = self.apply()
        self.assertEqual(result['status'], 'already_current')
        self.assertEqual(self.calls, [])

    def test_payload_tampering_or_extra_data_file_refused(self):
        p = self.payload / 'program/server/unexpected.sqlite3'
        p.write_bytes(b'not allowed')
        with self.assertRaisesRegex(upgrade.UpgradeError, '清单不完整'):
            self.apply()
        self.assertEqual(self.calls, [])

    def test_database_snapshot_failure_restores_old_service(self):
        with patch.object(Agent, 'backup_database', side_effect=OSError('snapshot failure')), patch.object(Agent, 'wait_health'):
            with self.assertRaises(RolledBack):
                self.apply()
        self.assertEqual(self.current.resolve(), self.previous)
        self.assertEqual(self.calls, [['systemctl', 'stop', 'hotel-tv'], ['systemctl', 'stop', 'hotel-tv'], ['systemctl', 'start', 'hotel-tv']])
