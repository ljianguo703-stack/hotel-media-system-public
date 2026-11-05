"""DL12: real engine phases, payload counters, node discovery and unchanged task ownership."""
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from downloads_engines import QBittorrent
from downloads_net import NetError
import downloads_sources as sources
import test_downloads_simple as simple


class QBPhases(unittest.TestCase):
    def engine(self, row):
        q = QBittorrent({'url': 'http://127.0.0.1:8098'}, '')
        q.sid = 'SID=test'; q.api = (2, 11); q._info = Mock(return_value=row)
        q._call = Mock(return_value=b'[]')
        return q

    def test_metadata_stall_and_queue_are_distinct_and_payload_is_not_network_bytes(self):
        for raw, expected in [('metaDL', 'metadata'), ('forcedMetaDL', 'metadata'), ('queuedDL', 'queued'),
                              ('stalledDL', 'stalled'), ('checkingDL', 'checking'), ('allocating', 'allocating'),
                              ('downloading', 'downloading'), ('forcedDL', 'downloading'), ('newUnknownState', 'unknown')]:
            with self.subTest(raw=raw):
                q = self.engine({'tags': 'hoteltv', 'state': raw, 'completed': 0, 'downloaded': 8000, 'size': -1,
                                 'num_seeds': 0, 'num_leechs': 2})
                state = q.status('a' * 40)
                self.assertEqual(state['state'], expected); self.assertEqual(state['downloaded'], 0)
                self.assertIsNone(state['total']); self.assertEqual((state['seeds'], state['peers']), (0, 2))

    def test_reannounce_never_controls_foreign_or_missing_torrents(self):
        q = self.engine({'tags': 'foreign', 'state': 'metaDL'})
        with self.assertRaises(NetError): q.reannounce('a' * 40)
        q._call.assert_not_called(); q._info.return_value = None
        with self.assertRaises(NetError): q.reannounce('a' * 40)
        q._call.assert_not_called(); q._info.return_value = {'tags': 'hoteltv'}
        q.reannounce('a' * 40)
        q._call.assert_called_once_with('/api/v2/torrents/reannounce', {'hashes': 'a' * 40})

    def test_new_jobs_write_into_selected_volume_from_the_start_without_global_preference_changes(self):
        q = self.engine(None); q._has_category = lambda: True; q._call.return_value=b'Ok.'
        q.add('test', {'kind': 'magnet', 'infohash': 'a' * 40, 'uri': 'magnet:?xt=urn:btih:' + 'a' * 40}, '/hotel-storage/disk-e/task')
        call = q._call.call_args
        self.assertEqual(call.args[0], '/api/v2/torrents/add')
        self.assertEqual(call.args[1]['savepath'], '/hotel-storage/disk-e/task')
        self.assertEqual(call.args[1]['autoTMM'], 'false'); self.assertEqual(call.args[1]['useDownloadPath'], 'false')
        self.assertNotIn('setPreferences', repr(q._call.call_args_list))

    def test_only_public_btsearch_hashes_gain_bootstrap_trackers(self):
        adapter = sources.BTSearchSource({'id': 'public', 'url': 'https://www.btsearch.love'}, '')
        raw = json.dumps({'total': 1, 'data': [{'hash': 'a' * 40, 'name': 'Example', 'size': 1024}]}).encode()
        with patch.object(adapter, '_fetch', return_value=raw): row = adapter.search('Example', '')[0]
        self.assertEqual(parse_qs(urlsplit(row['magnet']).query)['tr'], list(adapter.trackers))
        self.assertIsNone(row['seeders'])
        manual = sources.candidate('private', 'Private', 1024, magnet='magnet:?xt=urn:btih:' + 'b' * 40)
        self.assertNotIn('tr', parse_qs(urlsplit(manual['magnet']).query))


class LivePhaseOperations(unittest.TestCase):
    for _name in ('request', 'state', 'op', 'login_as', 'setUp', 'tearDown', 'configure', 'task', 'act', 'create_new'):
        locals()[_name] = getattr(simple.SimpleDownloads, _name)

    def prepare(self):
        self.configure(); self.store.stop_download_workers(); self.store.start_download_workers = lambda: None
        ident = self.create_new(manual={'uri': 'magnet:?xt=urn:btih:' + 'a' * 40})
        with self.store.db() as db:
            task = self.store.dl_task(db, ident)
            task.update(state='downloading', engine='qbittorrent', engine_ref='a' * 40,
                        engine_dir='/selected/task', stall={'bytes': 0, 'since': 0})
            self.store.dl_save(db, task)
        return ident

    def test_phase_progress_updates_clear_stale_queue_note_and_keep_connected_counts(self):
        ident = self.prepare(); task = self.task(ident)
        engine = Mock(); engine.status.return_value = {'state': 'metadata', 'downloaded': 0, 'total': None, 'speed': 0,
                                                      'eta': None, 'seeds': 0, 'peers': 0}
        settings = self.store.download_settings_view()['settings']; settings['stall_minutes'] = 120
        with patch.object(self.store, 'engine_for', return_value=engine), patch('operations_downloads.core') as core:
            core.return_value.now_ms.return_value = 180000
            self.store._dl_poll(task, settings, {'free_gb': 100})
            fresh = self.task(ident)
            self.assertEqual(fresh['progress']['engine_state'], 'metadata'); self.assertEqual(fresh['progress']['peers'], 0)
            self.assertEqual(fresh['progress']['waiting_seconds'], 180); self.assertIn('等待磁力元数据', fresh['note'])
            engine.status.return_value.update(state='downloading', downloaded=1024, total=2048, speed=512, seeds=1, peers=2)
            self.store._dl_poll(fresh, settings, {'free_gb': 100})
        fresh = self.task(ident)
        self.assertEqual(fresh['note'], '正在接收文件'); self.assertEqual(fresh['progress']['waiting_seconds'], 0)
        self.assertEqual(fresh['progress']['seeds'], 1)

    def test_node_discovery_preserves_identity_files_and_stall_timer_and_has_cooldown(self):
        ident = self.prepare(); before = self.task(ident)
        with patch.object(self.store, '_engine_call') as call:
            self.assertEqual(self.act(ident, 'reannounce')[0], 200)
            call.assert_called_once(); self.assertEqual(call.call_args.args[1], 'reannounce')
            self.assertEqual(self.act(ident, 'reannounce')[0], 400)
        after = self.task(ident)
        for field in ('engine_ref', 'engine_dir', 'storage', 'stall', 'generation'):
            self.assertEqual(after.get(field), before.get(field))
        with self.store.db() as db:
            after['state'] = 'cancelled'; self.store.dl_save(db, after)
        self.assertEqual(self.act(ident, 'reannounce')[0], 400)


if __name__ == '__main__': unittest.main()
