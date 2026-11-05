"""Fresh-install acceptance: actual AC3 conversion, publish gate and old pipeline freeze."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
import unittest
from urllib.parse import unquote
from unittest.mock import patch
import test_management as fixture
from management import Problem


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires actual media tools')
class AutomationE2E(unittest.TestCase):
    setUp = fixture.ManagementE2E.setUp
    tearDown = fixture.ManagementE2E.tearDown
    request = fixture.ManagementE2E.request
    state = fixture.ManagementE2E.state
    mutate = fixture.ManagementE2E.mutate
    wait_scan = fixture.ManagementE2E.wait_scan

    def sample(self):
        path = self.media / '电影' / '自动兼容验证.mkv'; path.parent.mkdir(exist_ok=True)
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=320x240:rate=24','-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','1','-c:v','libx264','-c:a','ac3','-ac','6',str(path)],check=True)
        self.wait_scan('main')
        with self.store.db() as db:
            row = next(r for r in db.execute('SELECT * FROM media') if '自动兼容验证' in r['body'])
            item = json.loads(row['body']); item['jellyfin_id'] = 'a'*32
            db.execute('UPDATE media SET body=?,published=0 WHERE id=?',(json.dumps(item),row['id']))
            self.store.put_kv(db,'media_automation',{'enabled':True})
        return path,item

    def wait_ready(self, item):
        for _ in range(200):
            with self.store.db() as db:
                if self.store.automation_readiness(db,item)['ready']: return self.store.compat_record(db,self.store.compat_id(item['path']))
            time.sleep(.05)
        self.fail('automatic compatibility did not finish')

    def test_automated_ac3_six_channel_copy_blocks_publish_and_keeps_source(self):
        source,item=self.sample(); before=hashlib.sha256(source.read_bytes()).hexdigest()
        code,_,body=self.mutate('/media/'+item['id'],{'published':True})
        self.assertEqual(code,409);self.assertIn('兼容',body['error'])
        batch=self.mutate('/media/batch',{'ids':[item['id']],'action':'publish'})[2]
        self.assertEqual(batch['ok'],[]);self.assertEqual(len(batch['failed']),1)
        self.store.prepare_automated_media(); record=self.wait_ready(item)
        self.assertEqual(record['verdict'],'audio');self.assertEqual(record['copy_status'],'ready')
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),before)
        target=self.store.media_path(unquote(record['copy_route']))
        data=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(target)]))
        audio=next(s for s in data['streams'] if s['codec_type']=='audio')
        self.assertEqual((audio['codec_name'],audio['channels']),('aac',2))
        self.assertEqual(self.mutate('/media/'+item['id'],{'published':True})[0],200)
        visible=next(x for x in self.request('/api/v1/catalog',tv=True)[2]['items'] if x['id']==item['id'])
        self.assertTrue(visible['path'].startswith('/media/_compat/'))
        # Upgraded source cannot silently bypass the gate; one new copy restores it.
        with source.open('ab') as f:f.write(b'\0')
        self.assertFalse(any(x['id']==item['id'] for x in self.request('/api/v1/catalog',tv=True)[2]['items']))
        self.store.prepare_automated_media(); updated=self.wait_ready(item)
        self.assertNotEqual(record['fingerprint'],updated['fingerprint'])
        self.assertTrue(any(x['id']==item['id'] for x in self.request('/api/v1/catalog',tv=True)[2]['items']))

    def test_batch_still_rejects_revision_conflicts_and_old_download_mutations(self):
        _,item=self.sample();revision=self.state()['revision']
        with self.store.db() as db:self.store.put_kv(db,'revision',revision+1)
        code,_,_=self.request('/api/admin/media/batch',{'revision':revision,'ids':[item['id']],'action':'publish'},auth=True)
        self.assertEqual(code,409)
        with self.assertRaises(Problem):self.store.create_download({}, {'name':'admin','role':'admin'},revision+1)
        with patch.object(self.store,'_dl_loop') as worker:
            self.store.recover_download_tasks();self.store.start_download_workers();worker.assert_not_called()


    # ---------- 1.1.0 regressions ----------
    def _ac3(self, name):
        path = self.media / '电影' / name; path.parent.mkdir(exist_ok=True)
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=320x240:rate=24','-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','1','-c:v','libx264','-c:a','ac3','-ac','6',str(path)],check=True)
        return path

    def _mark_jellyfin(self):
        with self.store.db() as db:
            for n, row in enumerate(db.execute('SELECT * FROM media ORDER BY rowid').fetchall()):
                item = json.loads(row['body'])
                if not item.get('jellyfin_id'):
                    item['jellyfin_id'] = '%032x' % (n + 1000)
                    db.execute('UPDATE media SET body=?,published=0 WHERE id=?', (json.dumps(item), row['id']))
            self.store.put_kv(db, 'media_automation', {'enabled': True})

    def _item(self, title):
        with self.store.db() as db:
            return next(m for m in (json.loads(r['body']) for r in db.execute('SELECT body FROM media')) if m.get('title') == title)

    def test_permanently_failed_items_never_starve_a_new_film(self):
        folder = self.media / '电影'; folder.mkdir(exist_ok=True)
        for i in range(12): (folder / ('损坏样本%02d.mkv' % i)).write_bytes(b'not a video file ' * 100)
        self.wait_scan('main'); self._mark_jellyfin()
        for _ in range(3): self.store.prepare_automated_media()
        self._ac3('新到影片.mkv'); self.wait_scan('main'); self._mark_jellyfin()
        with self.store.db() as db:
            failed = sum(self.store.automation_readiness(db, json.loads(r['body']))['label'] == '文件预检失败' for r in db.execute('SELECT body FROM media'))
        self.assertGreaterEqual(failed, 10)
        self.assertEqual(self.store.automation_work(), [self._item('新到影片')['id']])
        self.store.prepare_automated_media(); self.wait_ready(self._item('新到影片'))

    def test_gate_also_covers_manually_scanned_files_when_automation_is_on(self):
        self._ac3('手动拷贝.mkv'); self.wait_scan('main')
        with self.store.db() as db: self.store.put_kv(db, 'media_automation', {'enabled': True})
        item = self._item('手动拷贝'); self.assertNotIn('jellyfin_id', item)
        code, _, body = self.mutate('/media/' + item['id'], {'published': True})
        self.assertEqual(code, 409); self.assertIn('兼容', body['error'])
        self.store.prepare_automated_media(); self.wait_ready(item)
        self.assertEqual(self.mutate('/media/' + item['id'], {'published': True})[0], 200)
        visible = next(x for x in self.request('/api/v1/catalog', tv=True)[2]['items'] if x['id'] == item['id'])
        self.assertTrue(visible['path'].startswith('/media/_compat/'))

    def test_copies_follow_the_installer_compat_root_and_stay_routable(self):
        import tempfile as tf
        data_disk = Path(tf.mkdtemp(prefix='hotel-data-disk-')); self.addCleanup(shutil.rmtree, data_disk, True)
        self.store._configured_roots.append(data_disk.resolve())   # stands in for --library-root /mnt
        source, item = self.sample()
        with self.store.db() as db: self.store.put_kv(db, 'compat_root', str(data_disk / '.hotel-compat'))
        self.store.__dict__.pop('_compat_root_cache', None)
        self.store.prepare_automated_media(); record = self.wait_ready(item)
        self.assertTrue(record['copy_route'].startswith('/media/_compat/'))
        stored = self.store.media_path(unquote(record['copy_route']))
        self.assertIn(data_disk.resolve(), stored.parents)
        self.assertFalse((self.media / '_compat').exists() and any((self.media / '_compat').rglob('*.mkv')))
        outside = data_disk / 'outside.mkv'; outside.write_bytes(b'not a copy')
        with self.assertRaises(Problem): self.store.media_path('/media/_compat/../outside.mkv')
        with self.store.db() as db: self.store.put_kv(db, 'compat_root', '/opt/not-an-allowed-root')
        self.store.__dict__.pop('_compat_root_cache', None)
        with self.assertRaises(Problem) as error: self.store.media_path(record['copy_route'])
        self.assertIn('不在允许范围', str(error.exception))

    def test_space_shortage_is_reported_and_retried_automatically_when_space_returns(self):
        source, item = self.sample()
        with patch.object(self.store, 'compat_space', return_value=(False, 1, 2)):
            self.store.prepare_automated_media()
            for _ in range(200):
                with self.store.db() as db: record = self.store.compat_record(db, self.store.compat_id(item['path']))
                if record and record.get('job_status') == 'failed': break
                time.sleep(.05)
            self.assertEqual(record['job_error_kind'], 'space')
            with self.store.db() as db: label = self.store.automation_readiness(db, item)['label']
            self.assertIn('磁盘空间不足', label)
            self.assertEqual(self.store.automation_work(), [])   # nothing to do while still full
        self.assertEqual(self.store.automation_work(), [])       # space is back, but retries wait 10 minutes
        with self.store.db() as db:
            record = self.store.compat_record(db, self.store.compat_id(item['path'])); record['job_failed_at'] = 0
            self.store.put_object(db, 'compat', record)
        self.assertEqual(self.store.automation_work(), [item['id']])
        self.store.prepare_automated_media(); self.wait_ready(item)


    # ---------- independent review follow-ups ----------
    def test_existing_servers_without_automation_keep_copying_without_the_space_guard(self):
        self._ac3('生产手动.mkv'); self.wait_scan('main')
        item = self._item('生产手动')
        with patch.object(self.store, 'compat_space', return_value=(False, 1, 2)) as guard:
            revision = self.state()['revision']; self.store.compat_probe([item['id']], 'admin', revision)
            with self.store.db() as db: ident = self.store.compat_id(item['path']); revision = self.store.kv(db, 'revision')
            self.store.compat_enqueue([ident], 'admin', revision)
            for _ in range(200):
                with self.store.db() as db: record = self.store.compat_record(db, ident)
                if record.get('job_status') in ('done', 'failed'): break
                time.sleep(.05)
            guard.assert_not_called()
        self.assertEqual(record['job_status'], 'done')

    def test_a_probe_that_throws_is_terminal_until_the_file_changes(self):
        source, item = self.sample()
        with patch.object(self.store, 'probe_file', side_effect=subprocess.TimeoutExpired('ffprobe', 120)):
            self.store.prepare_automated_media()
        with self.store.db() as db:
            record = self.store.compat_record(db, self.store.compat_id(item['path']))
            self.assertEqual(record['verdict'], 'error'); self.assertEqual(self.store.automation_readiness(db, item)['label'], '文件预检失败')
        self.assertEqual(self.store.automation_work(), [])
        with source.open('ab') as f: f.write(b'\0')
        self.assertEqual(self.store.automation_work(), [item['id']])
        self.store.prepare_automated_media(); self.wait_ready(item)

    def test_replaced_file_after_a_failed_copy_starts_over_automatically(self):
        source, item = self.sample()
        with patch.object(self.store, 'ffmpeg_args', return_value=['false']):
            self.store.prepare_automated_media()
            for _ in range(200):
                with self.store.db() as db: record = self.store.compat_record(db, self.store.compat_id(item['path']))
                if record and record.get('job_status') == 'failed': break
                time.sleep(.05)
        self.assertEqual(record['job_status'], 'failed'); self.assertEqual(self.store.automation_work(), [])
        with source.open('ab') as f: f.write(b'\0')   # Radarr re-download to the same file name
        self.store.prepare_automated_media(); self.wait_ready(item)

    def test_full_temp_or_system_disk_is_not_misreported_as_compat_space(self):
        source, item = self.sample()
        import operations_compat
        with patch.object(operations_compat.tempfile, 'TemporaryFile', side_effect=OSError(28, 'No space left on device', '/tmp/x')):
            self.store.prepare_automated_media()
            for _ in range(200):
                with self.store.db() as db: record = self.store.compat_record(db, self.store.compat_id(item['path']))
                if record and record.get('job_status') == 'failed': break
                time.sleep(.05)
        self.assertEqual(record['job_status'], 'failed'); self.assertEqual(record.get('job_error_kind'), '')
        self.assertEqual(self.store.automation_work(), [])   # no 10-second retry loop

    def test_orphaned_copies_are_removed_but_archived_items_keep_theirs(self):
        source, item = self.sample(); self.store.prepare_automated_media(); record = self.wait_ready(item)
        copy = self.store.media_path(unquote(record['copy_route']))
        self.assertEqual(self.store.compat_gc(min_age_ms=0), 0)
        with self.store.db() as db:
            body = json.loads(db.execute('SELECT body FROM media WHERE id=?', (item['id'],)).fetchone()[0]); body['_archived'] = True
            db.execute('UPDATE media SET body=? WHERE id=?', (json.dumps(body), item['id']))
        self.assertEqual(self.store.compat_gc(min_age_ms=0), 0); self.assertTrue(copy.exists())
        self.assertEqual(self.store.compat_gc(), 0)   # young records are never swept
        with self.store.db() as db: db.execute('DELETE FROM media WHERE id=?', (item['id'],))
        self.assertEqual(self.store.compat_gc(min_age_ms=0), 1); self.assertFalse(copy.parent.exists())
        self.assertTrue(source.exists())                 # the source file is never touched
        with self.store.db() as db: self.assertIsNone(self.store.compat_record(db, record['id']))


class ColdInventory(unittest.TestCase):
    def test_empty_initial_cache_does_not_hide_inventory_in_first_two_seconds(self):
        import library_scan as s
        with patch.object(s,'_cache',(0,{})),patch.object(s.time,'monotonic',return_value=.1),patch.object(s,'physical_snapshot',return_value={'status':'ok','devices':[]}):
            self.assertEqual(s.mounted_volumes()['volumes'],[])
