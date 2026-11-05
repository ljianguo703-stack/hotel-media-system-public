"""RC03: real HTTP from a device through the hotel adapter to a fake native Jellyfin."""
import http.client
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from management import Store, encoded
from server import make_server


class SubtitlesE2E(unittest.TestCase):
    def setUp(self):
        # The current deployment's optional mounted-volume adapter starts with an empty
        # two-second cache on macOS. Use its real refresh, as the browser fixture does.
        if importlib.util.find_spec('library_scan'):
            import library_scan
            library_scan.mounted_volumes(refresh=True)
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.media = self.root / 'media'; self.media.mkdir()
        for name in ('film.mp4', 'ep.mp4', 'other.mp4', 'compatible.mkv'): (self.media / name).write_bytes(b'media')
        self.data = self.root / 'data'; self.data.mkdir()
        self.config = self.root / 'devices.json'
        self.config.write_text(json.dumps({'devices': {'test': {'token': 'device-test-only'}}}))
        (self.data / 'catalog.json').write_text('{"items":[]}')
        self.store = Store(self.data, self.media, self.config)
        def item(ident, name, kind, path):
            return {'Id': ident * 32, 'Name': name, 'Type': kind, 'Path': str(self.media / path), 'ImageTags': {}}
        def source(path, language='chi'):
            return {'Id': 'source_1', 'Path': str(self.media / path), 'MediaStreams': [
                {'Type': 'Subtitle', 'Index': 2, 'Codec': 'srt', 'IsExternal': True, 'Language': language, 'DisplayTitle': '中文字幕', 'IsDefault': True,
                 'DeliveryUrl': 'https://untrusted.invalid/?api_key=must-never-be-used'},
                {'Type': 'Subtitle', 'Index': 3, 'Codec': 'ass', 'IsExternal': False},
                {'Type': 'Subtitle', 'Index': 4, 'Codec': 'hdmv_pgs_subtitle', 'IsExternal': True}]}
        self.movie = item('a', '电影', 'Movie', 'film.mp4'); self.movie['MediaSources'] = [source('other.mp4', 'eng'), source('film.mp4')]
        self.show = item('b', '剧集', 'Series', 'unused'); self.episode = item('c', '分集', 'Episode', 'ep.mp4')
        self.episode.update(SeriesId='b'*32, ParentIndexNumber=1, IndexNumber=1, MediaSources=[source('ep.mp4')])
        self.mode = 'ok'; self.upstream_calls = []; fixture = self
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                fixture.upstream_calls.append(self.path)
                if 'Token="upstream-private"' not in self.headers.get('Authorization', ''): self.send_error(401); return
                if self.path.startswith('/Videos/'):
                    if fixture.mode == 'failure': self.send_error(503); return
                    if fixture.mode == 'redirect':
                        self.send_response(302); self.send_header('Location', '/should-not-follow'); self.end_headers(); return
                    raw = 'WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n你好，世界\n'.encode()
                    mime = 'text/vtt' if fixture.mode == 'ok' else 'text/html'
                else:
                    kind = parse_qs(urlsplit(self.path).query).get('IncludeItemTypes', [''])[0]
                    rows = {'Movie': [fixture.movie], 'Series': [fixture.show], 'Episode': [fixture.episode]}.get(kind, [])
                    raw = json.dumps({'Items': rows, 'TotalRecordCount': len(rows)}).encode(); mime = 'application/json'
                self.send_response(200); self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        (self.data / 'nas-integration.json').write_text(json.dumps({'url': f'http://127.0.0.1:{self.upstream.server_port}', 'token': 'upstream-private'}))
        self.tv = make_server('127.0.0.1', 0, self.config, self.data, self.media, store=self.store)
        threading.Thread(target=self.tv.serve_forever, daemon=True).start()
        self.sync()
        with self.store.db() as db: db.execute('UPDATE media SET published=1')

    def tearDown(self):
        for server in (self.tv, self.upstream): server.shutdown(); server.server_close()
        self.tmp.cleanup()

    def sync(self):
        job = self.store.start_jellyfin_sync('test')
        for _ in range(200):
            with self.store.db() as db: current = json.loads(db.execute('SELECT body FROM jobs WHERE id=?', (job['id'],)).fetchone()[0])
            if current['status'] in ('completed', 'failed'):
                self.assertEqual(current['status'], 'completed', current); return
            time.sleep(.01)
        self.fail('sync timed out')

    def request(self, path, token='device-test-only', method='GET'):
        conn = http.client.HTTPConnection('127.0.0.1', self.tv.server_port, timeout=5)
        conn.request(method, path, headers={'X-TV-Token': token})
        response = conn.getresponse(); result = response.status, dict(response.getheaders()), response.read(); conn.close(); return result

    def movie_entry(self): return next(x for x in self.store.catalog({'id': 'test'})['items'] if x['kind'] == 'movie')

    def test_movie_episode_version_binding_and_authenticated_webvtt(self):
        movie = self.movie_entry(); self.assertEqual(len(movie['subtitles']), 1)
        sub = movie['subtitles'][0]; self.assertEqual(sub['language'], 'chi')
        show = next(x for x in self.store.catalog({'id': 'test'})['items'] if x['kind'] == 'series')
        episode = show['episodes'][0]['subtitles'][0]; self.assertNotEqual(sub['id'], episode['id'])
        status, headers, raw = self.request(sub['path']); self.assertEqual(status, 200)
        self.assertTrue(raw.startswith(b'WEBVTT')); self.assertIn('你好'.encode(), raw); self.assertIn('text/vtt', headers['Content-Type'])
        self.assertIn('/Videos/' + 'a'*32 + '/source_1/Subtitles/2/Stream.vtt', self.upstream_calls)
        self.assertEqual(self.request(episode['path'])[0], 200)
        self.assertEqual(self.request(sub['path'], method='HEAD')[2], b'')
        before = len(self.upstream_calls); self.assertEqual(self.request(sub['path'], token='wrong')[0], 401)
        self.assertEqual(len(self.upstream_calls), before)
        public = self.request('/api/v1/catalog')[2].decode()
        for secret in ('upstream-private', 'untrusted.invalid', 'DeliveryUrl', '_jellyfin', str(self.media), 'source_1'):
            self.assertNotIn(secret, public)

    def test_outage_invalid_output_and_redirect_never_leak_or_follow(self):
        path = self.movie_entry()['subtitles'][0]['path']
        for mode in ('failure', 'html', 'redirect'):
            self.mode = mode; status, _, raw = self.request(path)
            self.assertEqual(status, 503); self.assertNotIn(b'upstream-private', raw)
        self.assertNotIn('/should-not-follow', self.upstream_calls)
        self.assertEqual(self.request('/api/v1/subtitles/../nas-integration.json')[0], 404)

    def test_sync_removal_and_visibility_revoke_old_subtitle(self):
        movie = self.movie_entry(); path = movie['subtitles'][0]['path']
        with self.store.db() as db: db.execute('UPDATE media SET published=0 WHERE id=?', (movie['id'],))
        self.assertEqual(self.request(path)[0], 404)
        with self.store.db() as db: db.execute('UPDATE media SET published=1,available=0 WHERE id=?', (movie['id'],))
        self.assertEqual(self.request(path)[0], 404)
        with self.store.db() as db: db.execute('UPDATE media SET available=1 WHERE id=?', (movie['id'],))
        self.movie['MediaSources'] = []; self.sync()
        self.assertFalse(self.movie_entry()['subtitles']); self.assertEqual(self.request(path)[0], 404)

    def test_compatible_movie_and_channel_share_original_subtitles(self):
        movie = self.movie_entry(); sub = movie['subtitles'][0]
        with self.store.db() as db:
            # The mapping is independently covered by AV01. This test exercises its public consumers.
            self.store.put_object(db, 'channel', {'id': 'test-ch', 'number': 1, 'name': '电影台', 'mode': 'loop', 'enabled': True, 'items': [movie['id']], 'breaks': {'enabled': False}, 'inserts': []})
        from unittest.mock import patch
        with patch.object(self.store, 'compat_routes', return_value={'/media/film.mp4': '/media/compatible.mkv'}), \
             patch.object(self.store, 'channel_timeline', return_value=[{'kind': 'program', 'route': '/media/film.mp4', 'media_id': movie['id'], 'start_ms': 1, 'end_ms': 100}]):
            compat = self.movie_entry(); self.assertEqual(compat['path'], '/media/compatible.mkv'); self.assertEqual(compat['subtitles'], [sub])
            entry = self.store.tv_channels()['channels'][0]['entries'][0]
            self.assertEqual(entry['path'], '/media/compatible.mkv'); self.assertEqual(entry['subtitles'], [sub])
        with self.store.db() as db: db.execute('UPDATE media SET published=0 WHERE id=?', (movie['id'],))
        self.assertEqual(self.request(sub['path'])[0], 200, 'a scheduled programme still serves its subtitles')

    def test_archive_disabled_source_and_missing_media_block_subtitles(self):
        movie = self.movie_entry(); path = movie['subtitles'][0]['path']
        with self.store.db() as db:
            row = db.execute('SELECT body FROM media WHERE id=?', (movie['id'],)).fetchone()
            body = json.loads(row['body']); body['_archived'] = True
            db.execute('UPDATE media SET body=? WHERE id=?', (encoded(body), movie['id']))
        self.assertEqual(self.request(path)[0], 404)
        body['_archived'] = False
        with self.store.db() as db:
            db.execute('UPDATE media SET body=? WHERE id=?', (encoded(body), movie['id']))
            source = json.loads(db.execute("SELECT body FROM sources WHERE id='main'").fetchone()['body']); source['enabled'] = False
            db.execute("UPDATE sources SET body=? WHERE id='main'", (encoded(source),))
        self.assertEqual(self.request(path)[0], 404)
        source['enabled'] = True
        with self.store.db() as db: db.execute("UPDATE sources SET body=? WHERE id='main'", (encoded(source),))
        (self.media / 'film.mp4').unlink()
        self.assertEqual(self.request(path)[0], 404)

    def test_directory_rescan_preserves_jellyfin_subtitles_and_sync_is_stable(self):
        before = self.movie_entry()['subtitles']
        job = self.store.start_scan('main', 'test')
        for _ in range(200):
            with self.store.db() as db: current = json.loads(db.execute('SELECT body FROM jobs WHERE id=?', (job['id'],)).fetchone()[0])
            if current['status'] in ('completed', 'failed'): break
            time.sleep(.01)
        self.assertEqual(current['status'], 'completed', current)
        self.assertEqual(self.movie_entry()['subtitles'], before)
        self.sync()
        with self.store.db() as db: revision = self.store.kv(db, 'revision')
        self.sync()
        with self.store.db() as db: self.assertEqual(self.store.kv(db, 'revision'), revision)
        self.assertEqual(self.movie_entry()['subtitles'], before)


if __name__ == '__main__': unittest.main()
