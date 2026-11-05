"""Smart download (DL01–DL04): parser, settings/secret boundaries, search & selection, engine control and the full
sentence -> engine -> verify/organise -> scan -> compat -> publish -> TV catalogue chain on real media files.
Engines here are HTTP mocks (downloads_mocks.py) that really write files; real engines are verified separately."""
import base64
import http.client
import json
import os
import shutil
import subprocess
import time
import unittest
from pathlib import Path

import test_management as fixture
import test_orders
from downloads_mocks import MockHub
from downloads_net import make_torrent, torrent_info
from downloads_parse import parse_request, needs_clarification
from management import Store

FFMPEG = shutil.which('ffmpeg') and shutil.which('ffprobe')
SECRET_VALUES = ('qb-test', 'aria-test', 'prowlarr-test', 'tz-test')


class ParserTest(unittest.TestCase):
    def test_sentences(self):
        p = parse_request('帮我下载电影《沙丘2》，要4K的')
        self.assertEqual((p['title'], p['kind'], p['resolution']), ('沙丘2', 'movie', '2160p')); self.assertFalse(needs_clarification(p))
        p = parse_request('下载电视剧《漫长的季节》第一季第3到5集 1080p 国语中字')
        self.assertEqual((p['kind'], p['season'], p['episodes'], p['audio'], p['subtitle']), ('series', 1, [3, 5], ['国语'], ['中文字幕']))
        p = parse_request('找一部2021年的电影 沙丘'); self.assertEqual((p['title'], p['year']), ('沙丘', 2021)); self.assertTrue(needs_clarification(p))
        p = parse_request('来点好看的'); self.assertTrue(needs_clarification(p))
        p = parse_request('下载《繁花》'); self.assertIn('请说明是电影还是电视剧（或其他类型）', p['questions'])
        p = parse_request('帮我下《流浪地球2》高清 不超过20G'); self.assertEqual((p['resolution'], p['max_size_gb']), ('1080p', 20.0)); self.assertTrue(p['warnings'])


@unittest.skipUnless(FFMPEG, 'ffmpeg/ffprobe not installed')
class DownloadE2E(unittest.TestCase):
    request = fixture.ManagementE2E.request
    state = fixture.ManagementE2E.state
    op = test_orders.OrdersE2E.op
    login_as = test_orders.OrdersE2E.login_as

    def setUp(self):
        fixture.ManagementE2E.setUp(self)
        self.store._dl_interval = 0.2
        self.hub = MockHub(); self.hub.seconds = 1.0
        self.src = self.root / 'sources'; self.src.mkdir()

    def tearDown(self):
        for store in [self.store] + getattr(self, 'extra_stores', []): store.stop_download_workers()
        self.hub.close(); fixture.ManagementE2E.tearDown(self)

    def clip(self, name, seconds=3, size='320x240'):
        path = self.src / name
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc=size=%s:rate=10' % size, '-f', 'lavfi', '-i', 'sine=sample_rate=8000',
                        '-t', str(seconds), '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(path)], check=True)
        return path

    def offer(self, title, clip, seeders=5):
        data = make_torrent(clip, name=title + clip.suffix); info = torrent_info(data)
        self.hub.files[title + '.torrent'] = data; self.hub.payloads[info['infohash']] = str(clip)
        self.hub.torznab_items.append({'title': title, 'size': info['size'], 'file': title + '.torrent', 'seeders': seeders})
        return info['infohash']

    def configure(self, auto=True, extra=None, secrets=None):
        settings = {'auto_mode': auto, 'preferred_resolution': 'any', 'min_free_gb': 0, 'min_video_seconds': 1, 'compat_wait_minutes': 5, 'scrape_wait_minutes': 1,
                    'sources': [{'id': 'tz', 'type': 'torznab', 'name': '测试 Torznab', 'url': self.hub.base + '/torznab/api', 'enabled': True}],
                    'engines': {'qbittorrent': {'enabled': True, 'url': self.hub.base + '/qb', 'username': 'admin', 'directory': '', 'host_directory': ''},
                                'aria2': {'enabled': True, 'url': self.hub.base + '/aria2/jsonrpc', 'directory': '', 'host_directory': ''}}}
        settings.update(extra or {})
        sec = {'qbittorrent': {'password': self.hub.qb_password}, 'aria2': {'secret': self.hub.aria_secret}, 'sources': {'tz': self.hub.torznab_key}}
        sec.update(secrets or {})
        status, _, body = self.op('download', 'download_settings', settings=settings, secrets=sec)
        self.assertEqual(status, 200, body)
        return body

    def create(self, text, **extra):
        extra.setdefault('flow_version', 1)  # existing full media-pipeline contract; DL10 tests cover the new default
        status, _, body = self.op('download', 'download_create', request=text, **extra)
        self.assertEqual(status, 200, body); return body['id']

    def task(self, ident):
        with self.store.db() as db: return self.store.dl_task(db, ident)

    def wait(self, ident, states, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            t = self.task(ident)
            if t['state'] in states: return t
            time.sleep(0.2)
        self.fail('task stuck in %s: %s' % (t['state'], t.get('note')))

    def act(self, ident, op, **extra):
        return self.op('download', 'download_action', id=ident, op=op, **extra)

    # ---------- D01: settings, permissions and secrets ----------
    def test_settings_secrets_permissions_and_connection_tests(self):
        view = self.configure()
        dumped = json.dumps(view, ensure_ascii=False)
        self.assertFalse(any(s in dumped for s in SECRET_VALUES)); self.assertTrue(view['secrets_present']['qbittorrent']['password'])
        self.assertEqual(oct((self.data / 'download-secrets.json').stat().st_mode & 0o777), '0o600')
        for target, expect in (('qbittorrent', 'ok'), ('aria2', 'ok'), ('source:tz', 'ok')):
            self.assertEqual(self.op('download', 'download_test', target=target)[2]['status'], expect, target)
        # Owner decision: open-source engines only. Xunlei is neither offered nor accepted.
        self.assertEqual(sorted(view['settings']['engines']), ['aria2', 'qbittorrent']); self.assertNotIn('http_engine', view['settings'])
        self.assertEqual(self.op('download', 'download_test', target='xunlei')[0], 400)
        self.assertEqual(self.op('download', 'download_settings', settings={'engines': {'xunlei': {'enabled': True}}})[0], 400)
        self.configure(secrets={'qbittorrent': {'password': 'wrong'}, 'aria2': {'secret': 'wrong'}, 'sources': {'tz': 'wrong'}})
        for target in ('qbittorrent', 'aria2', 'source:tz'): self.assertEqual(self.op('download', 'download_test', target=target)[2]['status'], 'auth_failed', target)
        self.configure(extra={'engines': {'qbittorrent': {'enabled': True, 'url': 'http://127.0.0.1:9', 'username': 'x'}}}, secrets={'qbittorrent': {'password': 'x'}})
        self.assertEqual(self.op('download', 'download_test', target='qbittorrent')[2]['status'], 'unreachable')
        operator, viewer = self.login_as('opdl', 'operator'), self.login_as('vwdl', 'viewer')
        self.assertEqual(self.op('download', 'download_settings', headers=operator, settings={'auto_mode': True})[0], 403)
        self.assertEqual(self.op('download', 'download_test', headers=operator, target='aria2')[0], 403)
        self.assertEqual(self.op('download', 'download_create', headers=viewer, request='下载电影《X》')[0], 403)
        no_csrf = {'Cookie': self.cookie}
        self.assertEqual(self.request('/api/admin/operations', {'kind': 'download', 'action': 'download_create', 'request': '下载电影《X》', 'revision': self.state()['revision']}, headers=no_csrf)[0], 403)
        stale = {'kind': 'download', 'action': 'download_create', 'request': '下载电影《X》', 'revision': self.state()['revision'] - 1}
        self.assertEqual(self.request('/api/admin/operations', stale, auth=True)[0], 409)
        audit = json.dumps(self.request('/api/admin/operations?section=audit', auth=True)[2], ensure_ascii=False)
        self.assertFalse(any(s in audit for s in SECRET_VALUES + ('wrong',)))
        self.assertIn(self.request('/api/v1/downloads', tv=True)[0], (401, 404))
        self.assertEqual(self.op('download', 'download_settings', settings={'staging_dir': str(self.media / 'visible')})[0], 400)

    # ---------- D02–D09: full chain ----------
    def test_sentence_to_tv_catalogue_on_real_media(self):
        clip = self.clip('a.mp4', 4, '1920x1080')
        infohash = self.offer('Test.Movie.2024.1080p.WEB-DL', clip)
        self.configure(); self.hub.seconds = 3.0
        ident = self.create('帮我下载电影《Test Movie》2024 1080p')
        t = self.wait(ident, ('downloading',))
        self.assertEqual((t['engine'], t['engine_ref']), ('qbittorrent', infohash))
        with self.store.db() as db: staging = Path(self.store.download_settings(db)['staging_dir'])
        time.sleep(0.6)
        job = self.store.start_scan('main', 'tester')
        for _ in range(100):
            with self.store.db() as db: row = json.loads(db.execute('SELECT body FROM jobs WHERE id=?', (job['id'],)).fetchone()['body'])
            if row['status'] not in ('queued', 'running'): break
            time.sleep(0.1)
        titles = [m['title'] for m in self.state()['media']]
        self.assertEqual(titles, ['现有影片'], 'partially downloaded files in staging must never be indexed')
        t = self.wait(ident, ('ready', 'failed'), 90)
        self.assertEqual(t['state'], 'ready', t.get('note')); self.assertTrue(t['published'])
        dest = Path(t['organized'][0]['dest']); src = Path(t['organized'][0]['src'])
        self.assertTrue(src.exists() and staging in src.parents, 'seeding file stays in staging')
        self.assertEqual(os.stat(dest).st_ino, os.stat(src).st_ino); self.assertEqual(t['organized'][0]['method'], 'hardlink')
        conn = http.client.HTTPConnection('127.0.0.1', self.tv.server_port, timeout=10)
        conn.request('GET', '/api/v1/catalog', headers={'X-TV-Token': 'tv-test-token'}); cat = json.loads(conn.getresponse().read())
        item = next(i for i in cat['items'] if i['id'] == t['media_id'])
        self.assertEqual(item['title'], 'Test Movie')
        conn.request('GET', item['path'], headers={'X-TV-Token': 'tv-test-token', 'Range': 'bytes=0-99'}); r = conn.getresponse(); body = r.read(); conn.close()
        self.assertEqual((r.status, len(body)), (206, 100))
        rows = self.request('/api/admin/operations?section=downloads', auth=True)[2]
        self.assertTrue(any(n['level'] == 'success' and n['task_id'] == ident for n in rows['notices']))
        self.assertFalse(any(s in json.dumps(rows) for s in SECRET_VALUES))
        self.assertEqual(self.hub.calls['qb_add'], 1)

    # ---------- D03/D04: search, selection, duplicates ----------
    def test_selection_never_downgrades_quality_or_guesses_year(self):
        small = self.clip('s.mp4', 2)
        self.offer('Same.Name.2019.1080p.BluRay', small)
        self.offer('Same.Name.2021.720p.WEB-DL', small)
        self.offer('Same.Name.2021.1080p.WEB-DL', small, seeders=0)
        self.offer('Other.Film.2021.1080p.WEB-DL', small)
        self.configure()
        ident = self.create('下载电影《Same Name》2021年 1080p')
        t = self.wait(ident, ('awaiting_choice', 'queued', 'downloading'))
        self.assertEqual(t['state'], 'awaiting_choice', t['note'])
        self.assertIn('未找到 1080p 资源', t['note'])
        by = {c['title']: c for c in t['candidates']}
        self.assertIn('年份 2019 不符', by['Same.Name.2019.1080p.BluRay']['blockers'])
        self.assertIn('当前无人做种', by['Same.Name.2021.1080p.WEB-DL']['blockers'])
        self.assertFalse(by['Same.Name.2021.720p.WEB-DL']['quality_ok'])
        self.assertIn('片名不匹配', by['Other.Film.2021.1080p.WEB-DL']['blockers'])
        self.assertEqual(self.act(ident, 'choose', key=by['Same.Name.2021.720p.WEB-DL']['key'])[2]['state'], 'queued')
        self.assertEqual(self.wait(ident, ('downloading', 'verifying', 'ready', 'failed'))['chosen']['title'], 'Same.Name.2021.720p.WEB-DL')

    def test_no_results_partial_failure_offline_and_duplicates(self):
        self.configure(extra={'sources': [{'id': 'tz', 'type': 'torznab', 'name': '正常源', 'url': self.hub.base + '/torznab/api', 'enabled': True},
                                          {'id': 'pw', 'type': 'prowlarr', 'name': '故障源', 'url': self.hub.base + '/prowlarr', 'enabled': True}]},
                       secrets={'sources': {'tz': self.hub.torznab_key, 'pw': self.hub.prowlarr_key}})
        self.hub.broken.add('prowlarr')
        ident = self.create('下载电影《Nothing Here》')
        t = self.wait(ident, ('no_results',)); self.assertIn('部分搜索源失败：故障源', t['note'])
        self.assertEqual(self.act(ident, 'retry')[2]['state'], 'searching')
        self.hub.broken.add('torznab')
        t = self.wait(ident, ('searching',)); time.sleep(1.0); t = self.task(ident)
        self.assertEqual(t['state'], 'searching'); self.assertTrue(t.get('waiting_network')); self.assertIn('所有搜索源都不可用', t['note'])
        self.assertEqual(self.op('download', 'download_create', request='下载电影《Nothing Here》', flow_version=1)[0], 409)
        self.hub.broken.clear()
        clip = self.clip('d.mp4', 2); infohash = self.offer('Dup.Film.2020.1080p', clip)
        self.hub.seconds = 4.0
        first = self.create('下载电影《Dup Film》'); self.wait(first, ('downloading',))
        magnet = 'magnet:?xt=urn:btih:' + infohash
        second = self.create('', manual={'uri': magnet, 'title': '另一个名字', 'kind': 'movie'})
        t = self.wait(second, ('failed',)); self.assertIn('已在任务', t['note'])

    def test_clarification_and_existing_media_block_auto_download(self):
        self.configure()
        ident = self.create('来点好看的')
        t = self.task(ident); self.assertEqual(t['state'], 'needs_clarify')
        self.assertEqual(self.act(ident, 'clarify', overrides={'title': '现有影片', 'kind': 'movie'})[2]['state'], 'searching')
        t = self.task(ident); self.assertTrue(t['existing_media'])
        self.assertFalse(self.store._auto_allowed(t))

    # ---------- D05/D06/D11: engine control, generations, restart reconcile ----------
    def test_pause_resume_cancel_keep_files_and_stale_updates_lose(self):
        clip = self.clip('p.mp4', 3); infohash = self.offer('Pause.Film.2022.1080p', clip)
        self.configure(); self.hub.seconds = 6.0
        ident = self.create('下载电影《Pause Film》')
        t = self.wait(ident, ('downloading',)); time.sleep(0.8)
        stale = dict(self.task(ident))
        self.assertEqual(self.act(ident, 'pause')[2]['state'], 'paused'); self.assertTrue(self.hub.qb[infohash]['paused'])
        self.assertIsNone(self.store._dl_update(stale, lambda x: x.update(state='downloading')), 'an update based on an older generation must be discarded')
        self.assertEqual(self.task(ident)['state'], 'paused')
        self.assertEqual(self.act(ident, 'resume')[2]['state'], 'downloading'); self.assertFalse(self.hub.qb[infohash]['paused'])
        time.sleep(0.5)
        partial = Path(self.task(ident)['engine_dir']) / 'Pause.Film.2022.1080p.mp4'
        self.assertEqual(self.act(ident, 'cancel')[2]['state'], 'cancelled')
        self.assertNotIn(infohash, self.hub.qb); self.assertTrue(partial.exists(), 'cancel keeps already downloaded data')
        time.sleep(1.0); self.assertEqual(self.task(ident)['state'], 'cancelled')

    def test_aria2_direct_link_survives_restart_without_duplicate_add(self):
        clip = self.clip('h.mp4', 3, '1920x1080'); url = 'https://files.example.invalid/Direct.Film.mp4'
        self.hub.payloads[url] = str(clip); self.configure(); self.hub.seconds = 3.0
        ident = self.create('', manual={'uri': url, 'title': 'Direct Film', 'kind': 'movie'})
        t = self.wait(ident, ('downloading',)); self.assertEqual(t['engine'], 'aria2')
        self.store._dl_interval = 3600  # "stop" the old process's polling
        self.store.stop_download_workers()
        reopened = Store(self.data, self.media, self.config, [self.media, self.external]); reopened._dl_interval = 0.2; self.extra_stores = [reopened]
        reopened.recover_download_tasks()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and self.task(ident)['state'] not in ('ready', 'failed'): time.sleep(0.2)
        t = self.task(ident)
        self.assertEqual(t['state'], 'ready', t['note']); self.assertEqual(self.hub.calls['aria_add'], 1)

    def test_disk_guard_and_post_step_retry_without_redownload(self):
        clip = self.clip('r.mp4', 3, '1920x1080'); self.offer('Retry.Film.2023.1080p', clip)
        self.configure(extra={'min_free_gb': 100000})
        ident = self.create('下载电影《Retry Film》')
        t = self.wait(ident, ('queued',)); time.sleep(1.0); t = self.task(ident)
        self.assertEqual(t['state'], 'queued'); self.assertIn('磁盘余量', t['note'])
        blocker = self.media / '电影'; blocker.write_text('not a folder')
        self.configure(extra={'min_free_gb': 0})
        t = self.wait(ident, ('failed', 'ready'), 60); self.assertEqual(t['state'], 'failed'); self.assertEqual(t['failed_at'], 'organizing')
        blocker.unlink()
        self.assertEqual(self.act(ident, 'retry')[2]['state'], 'organizing')
        t = self.wait(ident, ('ready', 'failed'), 60)
        self.assertEqual(t['state'], 'ready', t['note']); self.assertEqual(self.hub.calls['qb_add'], 1)

    # ---------- D08: Jellyfin metadata after import (HTTP double of the Jellyfin API used by jellyfin_sync.py) ----------
    def jellyfin(self, healthy=True):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        from urllib.parse import parse_qs, urlsplit
        media = self.media; state = {'healthy': healthy}
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self): self.send_response(204); self.end_headers()
            def do_GET(self):
                if not state['healthy']: self.send_error(503); return
                kind = parse_qs(urlsplit(self.path).query).get('IncludeItemTypes', [''])[0]
                files = sorted(p for p in media.rglob('*.mp4') if '.downloads' not in p.parts and p.name != 'clip.mp4')
                rows = [{'Id': 'e' * 32, 'Name': 'Jellyfin 片名', 'Type': 'Movie', 'Path': str(files[0]), 'ProductionYear': 2024, 'Overview': '来自 Jellyfin 的简介', 'ImageTags': {}}] if kind == 'Movie' and files else []
                raw = json.dumps({'Items': rows, 'TotalRecordCount': len(rows)}).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), H); threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        (self.data / 'nas-integration.json').write_text(json.dumps({'url': 'http://127.0.0.1:%d' % server.server_port, 'token': 'jf-test'}))
        return state

    def test_jellyfin_metadata_is_waited_for_and_failure_only_retries_the_scrape(self):
        self.jellyfin(healthy=True)
        clip = self.clip('j.mp4', 3, '1920x1080'); self.offer('Meta.Film.2024.1080p', clip)
        self.configure()
        ident = self.create('下载电影《Meta Film》')
        t = self.wait(ident, ('ready', 'failed'), 90)
        self.assertEqual(t['state'], 'ready', t['note']); self.assertEqual(t['scrape']['status'], 'done')
        with self.store.db() as db: body = json.loads(db.execute('SELECT body FROM media WHERE id=?', (t['media_id'],)).fetchone()['body'])
        self.assertEqual(body['description'], '来自 Jellyfin 的简介'); self.assertTrue(t['published'])

    def test_unavailable_jellyfin_does_not_fake_success(self):
        state = self.jellyfin(healthy=False)
        clip = self.clip('k.mp4', 3, '1920x1080'); self.offer('Nometa.Film.2024.1080p', clip)
        self.configure()
        with self.store.db() as db:
            s = self.store.download_settings(db); s['scrape_wait_minutes'] = 0.05; self.store.put_kv(db, 'download_settings', s)
        ident = self.create('下载电影《Nometa Film》')
        t = self.wait(ident, ('ready', 'failed'), 90)
        self.assertEqual(t['scrape']['status'], 'failed'); self.assertIn('Jellyfin', ' '.join(h['note'] for h in t['history']))
        self.assertEqual(t['state'], 'ready', '可播放的文件已入库，只标记刮削未完成'); adds = self.hub.calls['qb_add']
        state['healthy'] = True
        self.assertEqual(self.act(ident, 'retry_scrape')[2]['state'], 'scraping')
        t = self.wait(ident, ('ready', 'failed'), 60)
        self.assertEqual(t['scrape']['status'], 'done'); self.assertEqual(self.hub.calls['qb_add'], adds, 'retrying the scrape never re-downloads')

    # ---------- D07: multi-file season pack, episode selection, samples, cross-disk copy ----------
    def test_season_pack_maps_selected_episodes_and_copy_fallback_keeps_seeding_files(self):
        pack = self.src / 'Show.S01.1080p.WEB-DL'; pack.mkdir()
        for n in (1, 2, 3): shutil.copy(self.clip('e%d.mp4' % n, 2, '1920x1080'), pack / ('Show.S01E%02d.1080p.mp4' % n))
        shutil.copy(self.clip('sample.mp4', 2), pack / 'sample.mp4'); (pack / 'info.nfo').write_text('nfo')
        data = make_torrent(pack); info = torrent_info(data)
        self.hub.files['pack.torrent'] = data; self.hub.payloads[info['infohash']] = str(pack)
        self.hub.torznab_items.append({'title': 'Show.S01.1080p.WEB-DL', 'size': info['size'], 'file': 'pack.torrent', 'seeders': 3})
        self.configure()
        import operations_downloads, errno
        real_link = os.link
        def no_link(*a): raise OSError(errno.EXDEV, 'cross-device link')
        operations_downloads.os.link = no_link
        self.addCleanup(lambda: setattr(operations_downloads.os, 'link', real_link))
        ident = self.create('下载电视剧《Show》第一季第1到2集 1080p')
        t = self.wait(ident, ('ready', 'failed'), 90)
        self.assertEqual(t['state'], 'ready', t['note'])
        self.assertEqual(sorted((o['season'], o['episode']) for o in t['organized']), [(1, 1), (1, 2)])
        self.assertEqual({o['method'] for o in t['organized']}, {'copy'})
        for o in t['organized']:
            self.assertTrue(Path(o['src']).exists(), 'seeding files are never moved')
            self.assertEqual(Path(o['src']).read_bytes(), Path(o['dest']).read_bytes())
            self.assertIn('Season 01', o['dest'])
        self.assertFalse(list(self.media.rglob('.dl-*.part')), 'no temp files left behind')
        with self.store.db() as db: body = json.loads(db.execute('SELECT body FROM media WHERE id=?', (t['media_id'],)).fetchone()['body'])
        self.assertEqual((body['kind'], len(body['episodes'])), ('series', 2))
