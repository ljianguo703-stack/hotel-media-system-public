"""DL08: native public API contracts, malformed responses, cross-source failures and a real-media Mock e2e."""
import json
import unittest
from unittest.mock import patch

import downloads_sources as sources
import test_downloads as downloads
from downloads_mocks import MockHub
from downloads_net import NetError

HASH = '1234567890abcdef1234567890abcdef12345678'
RSS = '''<rss version="2.0" xmlns:nyaa="https://nyaa.si/xmlns/nyaa"><channel><item>
<title>中文试片.2024.1080p</title><link>https://nyaa.si/download/42.torrent</link><guid>https://nyaa.si/view/42</guid>
<nyaa:infoHash>1234567890abcdef1234567890abcdef12345678</nyaa:infoHash><nyaa:size>1.5 GiB</nyaa:size>
<nyaa:seeders>0</nyaa:seeders><nyaa:leechers>4</nyaa:leechers><pubDate>Wed, 28 Oct 2026 00:00:00 GMT</pubDate>
</item></channel></rss>'''.encode()


class PublicSources(unittest.TestCase):
    def test_public_candidate_key_does_not_expose_url_path_tokens(self):
        one=sources.candidate('source','Package',12,link='https://example.invalid/private/path-token/package.zip',protocol='http')
        two=sources.candidate('source','Package',12,link='https://example.invalid/private/path-token/package.zip',protocol='http')
        self.assertNotIn('path-token',one['key']);self.assertEqual(one['key'],two['key']);self.assertEqual(len(one['key']),68)

    def setUp(self):
        self.hub = MockHub(); self.addCleanup(self.hub.close)
        for adapter in (sources.PublicSource, sources.NyaaSource):
            p = patch.object(adapter, 'interval', 0); p.start(); self.addCleanup(p.stop)

    def config(self, kind):
        return {'id': kind, 'type': kind, 'url': self.hub.base + '/' + kind, 'enabled': True, 'name': kind}

    def test_knaben_chinese_post_and_zero_seeders(self):
        self.hub.knaben_hits = [{'title': '中文试片.2024.1080p', 'hash': HASH.upper(), 'bytes': '1234', 'seeders': 0,
                                 'peers': '3', 'cachedOrigin': 'Nyaa.si', 'link': 'https://example.com/view/42'}]
        rows = sources.build(self.config('knaben'), '').search('中文试片 2024', 'movie', 5)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['infohash'], rows[0]['size'], rows[0]['seeders'], rows[0]['peers']), (HASH, 1234, 0, 3))
        self.assertEqual(rows[0]['link'], ''); self.assertIn('btih%3A' + HASH, rows[0]['magnet'])
        kind, method, payload = self.hub.public_calls[0]
        self.assertEqual((kind, method, payload['query'], payload['size']), ('knaben', 'POST', '中文试片 2024', 5))
        self.assertNotIn('hide_unsafe', payload)

    def test_apibay_discards_sentinel_and_ignored_query_popular_list(self):
        self.hub.apibay_items = [{'id': '0', 'name': 'No results returned'}, {'id': '1', 'name': 'Unrelated Popular', 'info_hash': HASH},
                                {'id': '2', 'name': 'Big.Buck.Bunny.1080p', 'info_hash': HASH, 'size': '456', 'seeders': '0'}]
        src = sources.build(self.config('apibay'), '')
        self.assertEqual(src.search('变形金刚', 'movie'), [])
        row = src.search('Big Buck Bunny', 'movie')[0]
        self.assertEqual((row['size'], row['seeders'], row['peers']), (456, 0, None))
        self.assertEqual(self.hub.public_calls[-1][2], {'q': 'Big Buck Bunny', 'cat': '0'})

    def test_nyaa_namespace_iec_size_and_download_link(self):
        self.hub.nyaa_rss = RSS
        row = sources.build(self.config('nyaa'), '').search('中文试片', 'series')[0]
        self.assertEqual((row['size'], row['seeders'], row['infohash']), (1610612736, 0, HASH))
        self.assertEqual(row['link'], 'https://nyaa.si/download/42.torrent')
        self.assertEqual(self.hub.public_calls[-1][2], {'page': 'rss', 'q': '中文试片', 'c': '0_0'})
        self.hub.nyaa_rss = RSS.replace(b'https://nyaa.si/download/42.torrent', b'https://nyaa.si/view/42')
        self.assertEqual(sources.build(self.config('nyaa'), '').search('x', '')[0]['link'], '')

    def test_partial_failure_and_dedup_preserve_real_seeders(self):
        self.hub.knaben_hits = [{'title': '中文试片.2024.1080p', 'hash': HASH, 'seeders': 0}]
        self.hub.nyaa_rss = RSS.replace(b'<nyaa:seeders>0', b'<nyaa:seeders>7')
        self.hub.broken.add('apibay')
        rows, status = sources.search_all([(self.config(k), '') for k in ('knaben', 'apibay', 'nyaa')], '中文试片', 'movie')
        self.assertEqual(len(rows), 1); self.assertEqual(rows[0]['seeders'], 7)
        self.assertEqual(rows[0]['also_from'], ['nyaa']); self.assertEqual([s['status'] for s in status], ['ok', 'http', 'ok'])

    def test_bad_schema_and_unknown_source_are_failures_not_empty_success(self):
        for kind, body in [('knaben', b'[]'), ('apibay', b'{}'), ('nyaa', b'<html/>')]:
            with patch.object(sources.PublicSource, '_fetch', return_value=body):
                with self.assertRaises(NetError): sources.build(self.config(kind), '').search('x', '')
        with self.assertRaises(NetError): sources.build({'type': 'unknown'}, '')
        with patch.object(sources.PublicSource, '_fetch', return_value=b'<html>blocked</html>'):
            rows, status = sources.search_all([(self.config('knaben'), '')], 'x', '')
            self.assertEqual(rows, []); self.assertEqual(status[0]['status'], 'invalid')

    def test_malformed_rows_do_not_invent_hash_or_seed_count(self):
        self.hub.knaben_hits = [None, {'title': 'no hash'}, {'title': 'valid', 'hash': HASH, 'bytes': 'bad', 'seeders': 'hot', 'peers': -3}]
        rows = sources.build(self.config('knaben'), '').search('valid', '')
        self.assertEqual(len(rows), 1); self.assertEqual((rows[0]['size'], rows[0]['seeders'], rows[0]['peers']), (0, None, 0))


@unittest.skipUnless(downloads.FFMPEG, 'ffmpeg/ffprobe not installed')
class PublicSourceE2E(unittest.TestCase):
    for _name in ('request', 'state', 'op', 'login_as', 'setUp', 'tearDown', 'clip', 'configure', 'create', 'task', 'wait', 'act'):
        locals()[_name] = getattr(downloads.DownloadE2E, _name)

    def test_native_source_to_qb_real_file_and_tv_then_stale_health_cleared(self):
        from downloads_net import make_torrent, torrent_info
        clip = self.clip('public.mp4', 2, '1920x1080')
        info = torrent_info(make_torrent(clip))
        self.hub.payloads[info['infohash']] = str(clip)
        self.hub.knaben_hits = [{'title': 'Public.Sample.2024.1080p.WEB-DL', 'hash': info['infohash'], 'bytes': info['size'], 'seeders': 3}]
        conf = {'id': 'native', 'type': 'knaben', 'name': '公开源', 'url': self.hub.base + '/knaben', 'enabled': True}
        self.configure(extra={'sources': [conf]})
        self.assertEqual(self.op('download', 'download_test', target='source:native')[2]['status'], 'ok')
        ident = self.create('下载电影《Public Sample》2024年 1080p', auto=False)
        task = self.wait(ident, ('awaiting_choice', 'failed'))
        self.assertEqual(task['state'], 'awaiting_choice', task['note'])
        self.assertEqual(task['sources_status'][0]['count'], 1)
        view = self.request('/api/admin/operations?section=downloads', auth=True)[2]
        self.assertNotIn('magnet:', json.dumps(view)); self.assertNotIn('link', view['rows'][0]['candidates'][0])
        self.assertEqual(self.act(ident, 'choose', key=task['candidates'][0]['key'])[0], 200)
        task = self.wait(ident, ('ready', 'failed'), 40)
        self.assertEqual(task['state'], 'ready', task['note']); self.assertEqual(task['engine'], 'qbittorrent')
        self.assertTrue(task['media_id']); self.assertEqual(self.hub.calls['qb_add'], 1)
        self.assertEqual(self.act(ident, 'publish')[0], 200)
        task = self.wait(ident, ('ready', 'failed'), 20)
        cat = self.request('/api/v1/catalog', tv=True, headers={'X-TV-Token': 'tv-test-token'})[2]
        self.assertIn(task['media_id'], [r['id'] for r in cat['items']])
        task['chosen'].update(link='https://nyaa.si/download/42.torrent', infohash='a' * 40)
        with patch('operations_downloads.request', return_value=(200, {}, make_torrent(clip), 'https://nyaa.si/download/42.torrent')):
            with self.assertRaisesRegex(NetError, 'info-hash'): self.store._resolve_source({'sources': []}, task)
        with patch('operations_downloads.request', return_value=('magnet', {}, ('magnet:?xt=urn:btih:' + HASH).encode(), task['chosen']['link'])):
            with self.assertRaisesRegex(NetError, 'info-hash'): self.store._resolve_source({'sources': []}, task)
        conf['type'] = 'apibay'; conf['url'] = self.hub.base + '/apibay'
        updated = self.op('download', 'download_settings', settings={'sources': [conf]})
        self.assertEqual(updated[0], 200); self.assertNotIn('source:native', updated[2]['health'])
        self.assertEqual(self.op('download', 'download_settings', settings={'sources': [dict(conf, type='arbitrary')]})[0], 400)


if __name__ == '__main__': unittest.main()
