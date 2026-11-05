"""Independent DL07 regressions for ownership, control races and failed compatibility jobs."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from downloads_engines import Aria2, QBittorrent
from downloads_net import NetError
from downloads_parse import parse_request
from management import Store
from operations_downloads import DEFAULTS
from downloads_sources import candidate


class DownloadReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        data, media = self.root / 'data', self.root / 'media'
        data.mkdir(); media.mkdir()
        (data / 'catalog.json').write_text('{"items":[]}')
        (data / 'ads.json').write_text('{"ads":[]}')
        config = self.root / 'devices.json'; config.write_text('{"devices":{}}')
        self.store = Store(data, media, config)
        self.store.start_download_workers = lambda: None
        self.settings = copy.deepcopy(DEFAULTS)
        self.settings['staging_dir'] = str(media / '.downloads')
        self.settings['engines']['aria2'].update(enabled=True, url='http://127.0.0.1:6800/jsonrpc')
        self.user = {'name': 'review', 'role': 'admin'}
        self.addCleanup(self.tmp.cleanup)

    def task(self, **changes):
        created = self.store.create_download({'flow_version': 1, 'manual': {'uri': 'https://example.com/movie.mp4', 'title': '测试影片', 'kind': 'movie'}}, self.user, self.store._revision())
        with self.store.db() as db: task = self.store.dl_task(db, created['id'])
        task.update(changes)
        with self.store.db() as db: self.store.dl_save(db, task)
        return task

    def test_bt_without_qbittorrent_waits_for_supported_engine(self):
        self.assertIsNone(self.store.route_engine(self.settings, {'kind': 'torrent'}))
        self.assertIsNone(self.store.route_engine(self.settings, {'kind': 'magnet'}))
        self.assertFalse(Aria2.capabilities['torrent'])

    def test_qbittorrent_never_takes_over_an_existing_foreign_torrent(self):
        qb = QBittorrent({'url': 'http://127.0.0.1:8098'}, '')
        qb._ensure = lambda: None
        qb._info = lambda ref: {'category': 'other', 'tags': 'not-hoteltv'}
        qb._call = Mock()
        with self.assertRaises(NetError): qb.add('job', {'kind': 'magnet', 'infohash': 'a' * 40}, '/downloads/job')
        for method, args in (('pause', ()), ('resume', ()), ('cancel', ()), ('set_limit', (20,))):
            with self.assertRaises(NetError): getattr(qb, method)('a' * 40, *args)
        qb._call.assert_not_called()

    def test_cancel_during_remote_add_leaves_no_active_download(self):
        task = self.task()
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        remote = {'active': False}
        class Engine:
            def add(inner, *args):
                entered.set(); release.wait(3); remote['active'] = True; return 'test-ref'
            def cancel(inner, ref): remote['active'] = False
        engine = Engine(); self.store.engine_for = lambda *args: engine
        self.store._resolve_source = lambda *args: {'kind': 'url', 'uri': 'https://example.com/movie.mp4'}
        with self.store.db() as db: self.store.put_kv(db, 'download_settings', self.settings)
        errors = []
        def start():
            try: self.store._dl_start(task, self.settings)
            except Exception as exc: errors.append(exc)
        def cancel():
            try: self.store.download_action({'id': task['id'], 'op': 'cancel'}, self.user, self.store._revision())
            except Exception as exc: errors.append(exc)
            finally: cancelled.set()
        worker = threading.Thread(target=start); worker.start()
        self.assertTrue(entered.wait(2))
        controller = threading.Thread(target=cancel); controller.start()
        cancelled.wait(.15); release.set()
        worker.join(4); controller.join(4)
        self.assertFalse(worker.is_alive() or controller.is_alive()); self.assertEqual(errors, [])
        with self.store.db() as db: current = self.store.dl_task(db, task['id'])
        self.assertEqual(current['state'], 'cancelled')
        self.assertFalse(remote['active'], 'cancel must control a download whose add was still in flight')

    def test_stale_post_publish_cannot_publish_a_cancelled_task(self):
        task = self.task(state='publishing', auto=True, media_id='owned', organized=[{'dest': '/tmp/movie.mp4'}])
        with self.store.db() as db:
            db.execute('INSERT INTO media VALUES(?,?,?,?,?)', ('owned', 'main', '{"id":"owned","title":"影片","kind":"movie"}', 0, 1))
        self.store.download_action({'id': task['id'], 'op': 'cancel'}, self.user, self.store._revision())
        self.store.batch_media = Mock(return_value={})
        self.store._dl_post_step(task)
        self.store.batch_media.assert_not_called()

    def test_failed_conversion_is_not_silently_reenqueued_forever(self):
        task = self.task(state='converting', media_id='movie', organized=[{'route': '/media/movie.mp4'}])
        self.store.compat_tools = lambda: {'ffprobe': 'ffprobe'}
        self.store.compat_record = lambda *args: {'id': 'c', 'verdict': 'video', 'job_status': 'failed', 'job_error': 'encoder failed'}
        self.store.compat_enqueue = Mock()
        with self.assertRaises(self.store.Stop): self.store._dl_convert(task, self.settings)
        self.store.compat_enqueue.assert_not_called()

    def test_unreadable_or_absent_compatibility_record_is_not_success(self):
        task = self.task(state='converting', media_id='movie', organized=[{'route': '/media/movie.mp4'}])
        self.store.compat_tools = lambda: {'ffprobe': 'ffprobe'}
        self.store.compat_record = lambda *args: {'id': 'c', 'verdict': 'error', 'reasons': ['cannot read']}
        with self.assertRaises(self.store.Stop): self.store._dl_convert(task, self.settings)

    def test_resource_selection_does_not_auto_choose_sequel_or_unverified_year_language(self):
        p = parse_request('下载电影《测试影片》2024 1080p 国语中字')
        cs = [candidate('src', '测试影片2.2024.1080p', 100, magnet='magnet:?xt=urn:btih:' + '1' * 40),
              candidate('src', '测试影片.1080p', 100, magnet='magnet:?xt=urn:btih:' + '2' * 40),
              candidate('src', '测试影片.2024.1080p.English', 100, magnet='magnet:?xt=urn:btih:' + '3' * 40)]
        with self.store.db() as db: ranked = self.store.rank_candidates(db, self.settings, p, cs)
        self.assertTrue(all(not c['eligible'] for c in ranked))

    def test_manual_private_http_source_is_rejected_before_engine_add(self):
        task = self.task(manual={'kind': 'url', 'uri': 'http://169.254.169.254/latest/meta-data/', 'label': 'metadata'})
        with self.assertRaises(NetError): self.store._resolve_source(self.settings, task)

    def test_task_feed_never_exposes_tracker_download_credentials(self):
        task = self.task(chosen={'link': 'https://tracker.example/download?apikey=private-tracker-key', 'key': 'candidate'},
                         candidates=[{'link': 'https://tracker.example/download?passkey=private-tracker-key', 'key': 'candidate'}])
        text = json.dumps(self.store.download_rows({}), ensure_ascii=False)
        self.assertNotIn('private-tracker-key', text)
        self.assertNotIn('https://example.com/movie.mp4', text)
        with self.store.db() as db: raw = self.store.dl_task(db, task['id'])
        self.assertIn('private-tracker-key', raw['chosen']['link'], 'internal source must remain usable')

    def test_aria2_failed_result_is_removed_before_actual_retry(self):
        aria = Aria2({'url': 'http://127.0.0.1:6800/jsonrpc'}, '')
        aria._rpc = Mock(side_effect=[{'status': 'error'}, 'OK', 'new-ref'])
        aria.add('retry', {'kind': 'url', 'uri': 'https://example.com/file.mp4'}, '/downloads/retry')
        self.assertEqual([c.args[0] for c in aria._rpc.call_args_list], ['aria2.tellStatus', 'aria2.removeDownloadResult', 'aria2.addUri'])

    def test_aria2_completed_job_can_remove_result_without_deleting_file(self):
        aria = Aria2({'url': 'http://127.0.0.1:6800/jsonrpc'}, '')
        raw = json.dumps({'error': {'code': 1, 'message': 'Active Download not found for GID#abc'}}).encode()
        with patch('downloads_engines.request', return_value=(200, {}, raw, '')):
            with self.assertRaises(NetError) as result: aria._rpc('aria2.forceRemove', 'abc')
        self.assertEqual(result.exception.kind, 'missing')

    def test_disabling_rate_limit_clears_the_running_engine_limit(self):
        self.task(state='downloading', engine='aria2', engine_ref='ref', limit_kbps=100)
        engine = Mock(); self.store.engine_for = lambda *args: engine
        self.settings['speed_limit_kbps'] = 0
        self.store._dl_share_bandwidth(self.settings)
        engine.set_limit.assert_called_once_with('ref', 0)

    def test_tiny_positive_limit_never_becomes_unlimited(self):
        self.task(state='downloading', engine='aria2', engine_ref='a')
        self.task(state='downloading', engine='aria2', engine_ref='b')
        engine = Mock(); self.store.engine_for = lambda *args: engine
        self.settings['speed_limit_kbps'] = 1
        self.store._dl_share_bandwidth(self.settings)
        self.assertEqual(engine.set_limit.call_count, 2)
        self.assertTrue(all(c.args[1] > 0 for c in engine.set_limit.call_args_list))


if __name__ == '__main__': unittest.main()
