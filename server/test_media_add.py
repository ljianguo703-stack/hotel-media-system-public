"""1.1.0 加片: owner-pasted magnet/torrent through the built-in qBittorrent, identification, Radarr/Sonarr import,
owner confirmation of unclear titles and 改片名 through Jellyfin."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import media_add
from downloads_net import NetError, make_torrent
from media_add import episode_of, guess
import test_management as fixture
from management import Problem

KEYS = {'radarr': 'radarr-secret-key', 'sonarr': 'sonarr-secret-key', 'seerr': 'seerr-secret-key',
        'prowlarr': 'prowlarr-secret-key', 'bazarr': 'bazarr-secret-key'}
GB = 1024 ** 3


class FakeQB:
    def __init__(self):
        self.added, self.cancelled, self.deleted, self.torrents, self.files = [], [], [], {}, {}
        self.password = 'qb-test-password'
    def add(self, task_key, source, directory, limit_kbps=0):
        self.added.append((task_key, source, directory))
        self.torrents[source['infohash']] = {'state': 'downloading', 'downloaded': 1, 'total': 10, 'name': source.get('uri', '') and parse_qs(urlsplit(source['uri']).query).get('dn', [source['infohash']])[0] or 'torrent-name', 'dir': directory,
                                             'tags': 'hoteltv,hoteltv-' + task_key}
        return source['infohash']
    def status(self, ref):
        t = self.torrents.get(ref)
        if not t: return {'state': 'missing', 'downloaded': 0, 'total': None, 'speed': 0, 'eta': None, 'files': [], 'directory': '', 'error': ''}
        return {'state': t['state'], 'downloaded': t['downloaded'], 'total': t['total'], 'speed': 5, 'eta': 60, 'files': [], 'directory': t['dir'], 'error': '', 'seeds': 3}
    def _info(self, ref):
        t = self.torrents.get(ref); return {'name': t['name'], 'tags': t['tags']} if t else None
    @staticmethod
    def _tags(info): return {x for x in info.get('tags', '').split(',') if x}
    def cancel(self, ref): self.cancelled.append(ref); self.torrents.pop(ref, None)
    def _ensure(self): pass
    def _require_owned(self, info): pass
    def _call(self, path, form=None, files=None, retry=True):
        if path.startswith('/api/v2/torrents/files'):
            h=parse_qs(urlsplit(path).query)['hash'][0]
            torrent=self.torrents.get(h,{})
            folder=Path(torrent.get('dir','/nonexistent-fixture'))
            actual=[{'name':str(p.relative_to(folder)),'size':p.stat().st_size,'progress':1 if torrent.get('state')=='complete' else 0} for p in folder.rglob('*') if p.is_file()] if folder.is_dir() else [{'name':'video.mkv','progress':0}]
            return json.dumps(self.files.get(h,actual)).encode()
        assert path == '/api/v2/torrents/delete' and form['deleteFiles'] == 'true'
        self.deleted.append(form['hashes']); self.torrents.pop(form['hashes'], None)
    def complete(self, ref): self.torrents[ref].update(state='complete', downloaded=10)


class FakeApps:
    """Seerr search, Radarr and Sonarr as far as 加片 uses them. manualimport lists the real task folder."""
    def __init__(self):
        self.calls = []
        self.search = {}                     # query -> results
        self.details = {}                    # (type, id) -> detail
        self.movies, self.series, self.commands, self.has = {}, {}, {}, {}
        self.command_status = 'completed'
        self.import_works = True
        self.episodes_after = 0              # Sonarr: episode list appears only after the refresh queued on add
        self.episode_gets = 0
        self.servers = {}
        for app in KEYS:
            server = ThreadingHTTPServer(('127.0.0.1', 0), self.handler(app))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.servers[app] = server

    def url(self, app): return 'http://127.0.0.1:%d' % self.servers[app].server_port
    def close(self):
        for s in self.servers.values(): s.shutdown(); s.server_close()
    def posted(self, app, path): return [c[3] for c in self.calls if c[0] == app and c[1] == 'POST' and urlsplit(c[2]).path == path]

    def handler(self, app):
        fake = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def reply(self, code, value):
                raw = json.dumps(value).encode(); self.send_response(code); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
            def handle_any(self, method):
                parts = urlsplit(self.path); path = parts.path; q = {k: v[0] for k, v in parse_qs(parts.query).items()}
                raw = self.rfile.read(int(self.headers.get('Content-Length') or 0)); body = json.loads(raw) if raw else None
                fake.calls.append((app, method, self.path, body))
                if self.headers.get('X-Api-Key') != KEYS[app]: return self.reply(401, {'message': 'bad key'})
                if app == 'seerr':
                    if path == '/api/v1/search':
                        if fake.search == 'down': return self.reply(500, {'message': 'TMDB unreachable'})
                        return self.reply(200, {'results': fake.search.get(q['query'], [])})
                    m = re.fullmatch(r'/api/v1/(movie|tv)/(\d+)', path)
                    if m: return self.reply(200, fake.details.get((m.group(1), int(m.group(2))), {'id': int(m.group(2))}))
                if path == '/api/v3/tag':
                    return self.reply(200, {'id': 7, 'label': body['label']}) if method == 'POST' else self.reply(200, [])
                if path == '/api/v3/rootfolder': return self.reply(200, [{'path': fake.root + ('/media/电影' if app == 'radarr' else '/media/电视剧')}])
                if app == 'radarr':
                    if path == '/api/v3/movie' and method == 'GET': return self.reply(200, [m for m in fake.movies.values() if m['tmdbId'] == int(q['tmdbId'])])
                    if path == '/api/v3/movie/lookup/tmdb': return self.reply(200, {'title': 'Lookup', 'tmdbId': int(q['tmdbId']), 'year': 2023, 'images': []})
                    if path == '/api/v3/movie' and method == 'POST':
                        body['id'] = 100 + len(fake.movies); body['hasFile'] = False; body['path'] = body['rootFolderPath'] + '/Movie %d' % body['tmdbId']
                        fake.movies[body['id']] = body; return self.reply(201, body)
                    m = re.fullmatch(r'/api/v3/movie/(\d+)', path)
                    if m: return self.reply(200, fake.movies[int(m.group(1))])
                if app == 'sonarr':
                    if path == '/api/v3/series' and method == 'GET': return self.reply(200, [s for s in fake.series.values() if s['tvdbId'] == int(q['tvdbId'])])
                    if path == '/api/v3/series/lookup': return self.reply(200, [{'title': 'Lookup', 'tvdbId': int(q['term'].split(':')[1]), 'seasons': [{'seasonNumber': 1, 'monitored': True}]}])
                    if path == '/api/v3/series' and method == 'POST':
                        body['id'] = 200 + len(fake.series); fake.has[body['id']] = set(); body['path'] = body['rootFolderPath'] + '/Series %d' % body['tvdbId']
                        fake.series[body['id']] = body; return self.reply(201, body)
                    if path == '/api/v3/episode':
                        fake.episode_gets += 1
                        if fake.episode_gets <= fake.episodes_after: return self.reply(200, [])
                        has = fake.has[int(q['seriesId'])]
                        return self.reply(200, [{'id': 1000 + n, 'seasonNumber': 1, 'episodeNumber': n, 'hasFile': 1000 + n in has} for n in range(1, 13)])
                if path == '/api/v3/manualimport':
                    if 'movieId' in q or 'seriesId' in q:   # upstream: the title's own library folder, not ?folder=
                        return self.reply(500, {'message': 'Could not find a part of the path'})
                    items = []
                    for f in sorted(Path(q['folder']).rglob('*')):
                        if not f.is_file(): continue
                        item = {'path': str(f), 'size': f.stat().st_size, 'quality': {'quality': {'id': 7}}, 'languages': [{'id': 1}], 'releaseGroup': 'GRP', 'rejections': []}
                        if app == 'sonarr':   # folder mode: Sonarr only reads SxxEyy, and may pick another series (ids 9xxx)
                            m = re.search(r'S(\d+)E(\d+)', f.name)
                            item['episodes'] = [{'id': 9000 + int(m.group(2)), 'seasonNumber': int(m.group(1)), 'episodeNumber': int(m.group(2))}] if m else []
                            item['seasonNumber'] = int(m.group(1)) if m else None
                        items.append(item)
                    return self.reply(200, items)
                if path == '/api/v3/command' and method == 'POST':
                    ident = len(fake.commands) + 1; fake.commands[ident] = (app, body); return self.reply(201, {'id': ident, 'status': 'queued'})
                m = re.fullmatch(r'/api/v3/command/(\d+)', path)
                if m:
                    owner, command = fake.commands[int(m.group(1))]
                    if fake.command_status == 'completed' and fake.import_works:
                        for f in command['files']:
                            if 'movieId' in f: fake.movies[f['movieId']]['hasFile'] = True
                            else: fake.has[f['seriesId']].update(f['episodeIds'])
                    return self.reply(200, {'id': int(m.group(1)), 'status': fake.command_status, 'message': 'Import failed: disk'})
                return self.reply(404, {'message': 'unknown ' + path})
            def do_GET(self): self.handle_any('GET')
            def do_POST(self): self.handle_any('POST')
            def do_PUT(self): self.handle_any('PUT')
        return Handler


def movie(ident, title, year, original=''):
    return {'id': ident, 'mediaType': 'movie', 'title': title, 'originalTitle': original or title, 'releaseDate': '%d-02-01' % year, 'posterPath': '/p%06d.jpg' % ident}


def tv(ident, title, year):
    return {'id': ident, 'mediaType': 'tv', 'name': title, 'originalName': title, 'firstAirDate': '%d-01-01' % year, 'posterPath': '/t%06d.jpg' % ident}


class NameGuessing(unittest.TestCase):
    def test_common_release_names(self):
        cases = {
            '[电影天堂www.dytt89.com]流浪地球2-2023_HD国语中字.mp4': (['流浪地球2'], 2023, 'auto'),
            '【高清影视之家发布 www.HDBTHD.com】流浪地球2[国语配音+中文字幕].The.Wandering.Earth.II.2023.2160p.WEB-DL.H265.DDP5.1-QHstudIo': (['流浪地球2', 'The Wandering Earth II'], 2023, 'auto'),
            '阳光电影www.ygdy8.com.霸王别姬.BD.1080p.国语中字.mkv': (['霸王别姬'], None, 'auto'),
            'Oppenheimer (2023) [1080p] [WEBRip] [5.1] [YTS.MX]': (['Oppenheimer'], 2023, 'auto'),
            '[BD影视分享bd2020.co]沙丘2.Dune.Part.Two.2024.HD1080P.中英双字.mp4': (['沙丘2', 'Dune Part Two'], 2024, 'auto'),
            '[电影天堂www.dytt89.com]X战警：天启.BD.1080p.中英双字.mkv': (['X战警：天启'], None, 'auto'),
            '繁花.Blossoms.Shanghai.S01E01.2023.2160p.WEB-DL': (['繁花', 'Blossoms Shanghai'], 2023, 'series'),
            '狂飙.全39集.2023.4K.国语中字': (['狂飙'], 2023, 'series'),
            'Breaking.Bad.S05.1080p.BluRay.x264': (['Breaking Bad'], None, 'series'),
            'A.Complete.Unknown.2024.1080p.WEB-DL': (['A Complete Unknown'], 2024, 'auto'),   # "Complete" is a title word, not a pack
        }
        for name, (queries, year, kind) in cases.items():
            g = guess(name)
            self.assertEqual((g['queries'], g['year'], g['kind']), (queries, year, kind), name)
        self.assertEqual(guess('Inception.2010.1080p', 'series')['kind'], 'series')   # owner's choice wins
        self.assertEqual(guess('X.2022.1080p')['queries'], [])                       # one letter is not a title to trust

    def test_episode_numbers_sonarr_may_not_parse_and_extras(self):
        self.assertEqual(episode_of('Breaking.Bad.S05E03.mkv'), (5, 3))
        self.assertEqual(episode_of('Show Season 2 Episode 3.mkv'), (2, 3))
        self.assertEqual(episode_of('繁花 第12集.mp4'), (1, 12))
        self.assertEqual(episode_of('狂飙 第三集.mp4', 2), (2, 3))
        self.assertEqual(episode_of('繁花.EP08.4K.mp4'), (1, 8))
        self.assertEqual(episode_of('[Group][Title][07][1080p].mkv'), (1, 7))
        self.assertIsNone(episode_of('Movie.2023.mkv'))
        for name in ('花絮 02.mp4', '繁花.Trailer.mkv', 'sample.mkv', 'Behind.The.Scenes.mkv'): self.assertTrue(media_add.is_extra(name), name)
        for name in ('繁花 第02集.mp4', 'Breaking.Bad.S05E03.mkv', 'The.Wandering.Earth.II.2023.2160p.WEB-DL.mkv', 'Kung.Fu.Panda.4.2024.mkv', 'Young.Sheldon.S01E01.mkv',
                     'The.Interview.2014.1080p.mkv', 'Trailer.Park.Boys.S01E01.mkv', 'Hong.Kong.Movie.mkv', '幕后玩家.2018.mkv', 'Shang-Chi.2021.mkv'):
            self.assertFalse(media_add.is_extra(name), name)
        self.assertTrue(media_add.is_extra('Extras/Interview with the cast.mkv')); self.assertTrue(media_add.is_extra('Movie-trailer.mkv'))


class MediaAddFixture:
    setUp_base = fixture.ManagementE2E.setUp
    tearDown_base = fixture.ManagementE2E.tearDown
    request = fixture.ManagementE2E.request
    state = fixture.ManagementE2E.state

    def setUp(self):
        self.setUp_base()
        self.apps = FakeApps(); self.apps.root = str(self.media / 'auto')
        config = {app: {'url': self.apps.url(app), 'api_key': key} for app, key in KEYS.items()}
        config['radarr']['profile'] = 4; config['sonarr']['profile'] = 6
        config['qbittorrent'] = {'url': 'http://127.0.0.1:8098'}
        config.update(storage_root=self.apps.root, compat_root=str(self.media / '_compat'))
        (self.data / 'media-automation.json').write_text(json.dumps(config))
        (self.data / 'download-secrets.json').write_text(json.dumps({'qbittorrent': {'password': 'qb-test-password'}}))
        with self.store.db() as db: self.store.put_kv(db, 'media_automation', {'enabled': True})
        self.qb = FakeQB()
        self.engine = patch.object(media_add, 'QBittorrent', lambda config, password: self.qb); self.engine.start()
        self.timer = patch.object(media_add.threading, 'Timer'); self.timer.start()

    def tearDown(self):
        self.timer.stop(); self.engine.stop(); self.apps.close(); self.tearDown_base()

    def add(self, **body): return self.store.media_add_action({'action': 'add', **body}, 'admin')
    def appear_in_catalog(self, task, title='影片'):
        """What Jellyfin + the hotel sync do after an import: an unpublished row whose file is under the archived folder."""
        prefix = self.store._media_route_prefix(self.store._media_add_get(task['id'])['arr']['path'])
        body = {'id': 'jf-' + task['id'], 'title': title, 'kind': 'movie', 'path': prefix + 'file.mkv', 'jellyfin_id': 'b' * 32}
        with self.store.db() as db: db.execute('INSERT INTO media VALUES(?,?,?,?,1)', (body['id'], 'main', json.dumps(body, ensure_ascii=False), 0))
    def task(self, name=None): return next(t for t in self.store.media_add_list()['tasks'] if name is None or t['name'] == name)
    def finish(self, task, files):
        folder = Path(self.qb.added[-1][2]); folder.mkdir(parents=True, exist_ok=True)
        for name, size in files.items():
            path = folder / name; path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('wb') as f: f.truncate(size)   # sparse: sizes without disk use
        self.qb.complete(self.qb.added[-1][1]['infohash'])


class MediaAddE2E(MediaAddFixture, unittest.TestCase):
    def test_movie_is_identified_downloaded_and_imported_without_any_search(self):
        self.apps.search['流浪地球2'] = [movie(842675, '流浪地球2', 2023), movie(535167, '流浪地球', 2019)]
        result = self.add(link='magnet:?xt=urn:btih:' + 'a' * 40 + '&dn=' + '流浪地球2.2023.1080p.WEB-DL.mkv')
        self.assertIn('已开始下载', result['message'])
        task, source, folder = self.qb.added[0]
        self.assertEqual(Path(folder), Path(self.apps.root) / 'downloads' / 'manual' / task)   # one folder per task, same disk as the library
        t = self.task(); self.assertEqual(t['stage'], 'matched'); self.assertEqual(t['match']['tmdb_id'], 842675); self.assertEqual(t['match']['how'], 'auto')
        self.store.media_add_step()
        self.assertEqual(self.task()['stage'], 'matched'); self.assertEqual(self.task()['download']['progress'], 0.1)
        self.assertFalse(self.apps.posted('radarr', '/api/v3/movie'))          # nothing is registered before the download completes
        self.finish(t, {'流浪地球2.mkv': 4 * GB, 'Sample/sample.mkv': 40 * 1024 ** 2, '花絮.mkv': GB, 'readme.txt': 10})
        self.store.media_add_step()
        added = self.apps.posted('radarr', '/api/v3/movie')[0]
        self.assertFalse(added['monitored']); self.assertEqual(added['addOptions'], {'searchForMovie': False})
        self.assertEqual(added['rootFolderPath'], self.apps.root + '/media/电影'); self.assertEqual(added['qualityProfileId'], 4); self.assertEqual(added['tags'], [7])
        listing = [c[2] for c in self.apps.calls if c[0] == 'radarr' and c[2].startswith('/api/v3/manualimport')]
        self.assertNotIn('movieId', listing[0])                                 # with an id Radarr would list the library folder instead
        command = self.apps.posted('radarr', '/api/v3/command')[0]
        self.assertEqual(command['importMode'], 'copy')                        # hardlink: the torrent keeps seeding
        self.assertEqual([(Path(f['path']).name, f['movieId']) for f in command['files']], [('流浪地球2.mkv', added['id'])])
        self.assertEqual(self.task()['stage'], 'importing')
        self.store.media_add_step()
        t = self.task(); self.assertEqual(t['stage'], 'imported'); self.assertIn('同步到“待上架”', t['notes'][0])   # 100% and archived is not yet "in 待上架"
        self.store.media_add_step(); self.assertEqual(self.task()['stage'], 'imported')
        self.appear_in_catalog(t, '流浪地球2'); self.store.media_add_step()
        t = self.task(); self.assertEqual(t['stage'], 'listed'); self.assertIn('已进入待上架（未上架）', t['notes'][0])
        self.assertFalse(self.qb.cancelled or self.qb.deleted); self.assertIn('a' * 40, self.qb.torrents)   # the original keeps seeding

    def test_existing_library_file_is_never_replaced(self):
        self.apps.search['Inception'] = [movie(27205, '盗梦空间', 2010, 'Inception')]
        self.apps.movies[50] = {'id': 50, 'tmdbId': 27205, 'hasFile': True, 'title': 'Inception'}
        self.add(link='magnet:?xt=urn:btih:' + '5' * 40 + '&dn=Inception.2010.2160p')
        self.finish(None, {'Inception.mkv': 8 * GB}); self.store.media_add_step()
        t = self.task(); self.assertEqual(t['stage'], 'failed'); self.assertIn('避免覆盖', t['error'])
        self.assertFalse(self.apps.posted('radarr', '/api/v3/command'))

    def test_a_season_pack_taken_for_a_movie_stops_and_can_be_matched_as_a_series(self):
        self.apps.search['漫长的季节'] = [movie(71, '漫长的季节', 2023)]
        self.apps.details[('tv', 209000)] = {'id': 209000, 'mediaType': 'tv', 'name': '漫长的季节', 'firstAirDate': '2023-04-22', 'externalIds': {'tvdbId': 430000}}
        self.add(link='magnet:?xt=urn:btih:' + '6' * 40 + '&dn=漫长的季节.2023.1080p.WEB-DL')
        self.assertEqual(self.task()['match']['media_type'], 'movie')       # TMDB only offered a same-named film
        self.finish(None, {'漫长的季节 第01集.mp4': GB, '漫长的季节 第02集.mp4': GB, '漫长的季节 第03集.mp4': GB}); self.store.media_add_step()
        t = self.task(); self.assertEqual(t['stage'], 'needs_file'); self.assertEqual(len(t['file_candidates']),3)
        self.assertFalse(self.apps.posted('radarr', '/api/v3/command'))
        self.store.media_add_action({'action': 'match', 'id': t['id'], 'media_type': 'tv', 'tmdb_id': 209000}, 'admin')
        for _ in range(3): self.store.media_add_step()
        files = sorted(f['episodeIds'][0] for f in self.apps.posted('sonarr', '/api/v3/command')[0]['files'])
        self.assertEqual(files, [1001, 1002, 1003]); self.assertEqual(self.task()['stage'], 'imported')

    def test_unclear_title_waits_for_the_owner_then_imports(self):
        self.apps.search['Home'] = [movie(1, 'Home', 2009), movie(2, 'Home', 2015)]
        self.apps.details[('movie', 2)] = movie(2, '疯狂外星人', 2015, 'Home')
        self.add(link='magnet:?xt=urn:btih:' + 'b' * 40 + '&dn=Home.mkv')
        t = self.task(); self.assertEqual(t['stage'], 'needs_match'); self.assertEqual([c['tmdb_id'] for c in t['candidates']], [1, 2])
        issues = self.store.media_add_issues(); self.assertEqual(issues[0]['title'], '1 个下载需要确认片名'); self.assertEqual(issues[0]['action'], 'view:add')
        self.finish(t, {'Home.mkv': GB}); self.store.media_add_step()
        self.assertEqual(self.task()['stage'], 'needs_match'); self.assertFalse(self.apps.posted('radarr', '/api/v3/command'))
        message = self.store.media_add_action({'action': 'match', 'id': t['id'], 'media_type': 'movie', 'tmdb_id': 2}, 'admin')['message']
        self.assertIn('疯狂外星人', message)
        self.assertEqual(self.task()['match']['how'], 'owner')
        self.store.media_add_step(); self.store.media_add_step()
        self.assertEqual(self.task()['stage'], 'imported')
        self.assertEqual(self.apps.posted('radarr', '/api/v3/movie')[0]['tmdbId'], 2)

    def test_same_title_as_film_and_series_is_asked(self):
        self.apps.search['三体'] = [movie(81, '三体', 2023), tv(108545, '三体', 2023)]
        self.add(link='magnet:?xt=urn:btih:' + '7' * 40 + '&dn=三体.2023.1080p')
        self.assertEqual([c['media_type'] for c in self.task()['candidates']], ['movie', 'tv'])
        self.assertEqual(self.task()['stage'], 'needs_match')

    def test_series_waits_for_episodes_maps_names_and_never_doubles_an_episode(self):
        self.apps.search['繁花'] = [tv(209867, '繁花', 2023)]
        self.apps.details[('tv', 209867)] = {'id': 209867, 'externalIds': {'tvdbId': 421994}}
        self.apps.episodes_after = 1   # the series is new: Sonarr has no episodes until its refresh ran
        self.add(link='magnet:?xt=urn:btih:' + 'c' * 40 + '&dn=繁花.S01.2023.2160p.WEB-DL')
        t = self.task(); self.assertEqual((t['stage'], t['match']['media_type']), ('matched', 'tv'))
        self.finish(t, {'繁花.S01E01.mkv': GB, '繁花 第02集.mp4': GB, '繁花 第03集.mp4': GB, '繁花 第03集 粤语版.mp4': GB,
                        'Season 1 Episode 4.mp4': GB, '花絮 05.mp4': GB, '繁花 第06集.mp4': GB})
        self.store.media_add_step()
        t = self.task(); self.assertEqual(t['stage'], 'matched'); self.assertIn('分集信息', t['error'])
        series = self.apps.posted('sonarr', '/api/v3/series')[0]
        self.assertEqual(series['tvdbId'], 421994); self.assertFalse(series['monitored']); self.assertFalse(series['seasons'][0]['monitored'])
        self.assertEqual(series['addOptions'], {'monitor': 'none', 'searchForMissingEpisodes': False, 'searchForCutoffUnmetEpisodes': False})
        self.apps.has[series['id']].add(1006)                                  # episode 6 is already in the library
        self.store.media_add_step()
        listing = [c[2] for c in self.apps.calls if c[0] == 'sonarr' and c[2].startswith('/api/v3/manualimport')]
        self.assertTrue(listing and all('seriesId' not in x for x in listing))
        files = {Path(f['path']).name: f['episodeIds'] for f in self.apps.posted('sonarr', '/api/v3/command')[0]['files']}
        self.assertEqual(files, {'繁花.S01E01.mkv': [1001], '繁花 第02集.mp4': [1002], 'Season 1 Episode 4.mp4': [1004]})   # ids of OUR series
        self.store.media_add_step()
        t = self.task(); self.assertEqual(t['stage'], 'imported')
        self.assertEqual(t['unmapped'], ['繁花 第03集 粤语版.mp4', '繁花 第03集.mp4'])
        self.assertTrue(any('有重复' in n for n in t['notes'])); self.assertTrue(any('已有' in n for n in t['notes']))

    def test_series_without_tvdb_is_hardlinked_into_the_tv_library(self):
        self.apps.search['小巷人家'] = [tv(250000, '小巷人家', 2024)]
        self.apps.details[('tv', 250000)] = {'id': 250000, 'externalIds': {}}
        self.add(link='magnet:?xt=urn:btih:' + '8' * 40 + '&dn=小巷人家.全40集.2024.1080p')
        self.finish(None, {'小巷人家 第01集.mp4': 1000, '小巷人家 第02集.mp4': 1000, '花絮.mp4': 10}); self.store.media_add_step()
        t = self.task(); self.assertEqual(t['stage'], 'imported'); self.assertTrue(any('TVDB' in n for n in t['notes']))
        show = Path(self.apps.root) / 'media' / '电视剧' / '小巷人家 (2024) [tmdbid-250000]' / 'Season 01'
        self.assertEqual(sorted(p.name for p in show.iterdir()), ['小巷人家 S01E01.mp4', '小巷人家 S01E02.mp4'])
        source = Path(self.qb.added[-1][2]) / '小巷人家 第01集.mp4'
        self.assertEqual(os.stat(show / '小巷人家 S01E01.mp4').st_ino, source.stat().st_ino)   # hardlink, seeding copy untouched
        self.assertFalse(self.apps.posted('sonarr', '/api/v3/series'))

    def test_torrent_upload_and_rejected_links(self):
        self.apps.search['Inception'] = [movie(27205, '盗梦空间', 2010, 'Inception')]
        source = Path(self.tmp.name) / 'Inception.2010.1080p.BluRay.x264'; source.mkdir(); (source / 'Inception.mkv').write_bytes(b'x' * 1000)
        result = self.add(torrent_b64=base64.b64encode(make_torrent(source)).decode(), kind='movie')
        self.assertEqual(self.qb.added[0][1]['kind'], 'torrent'); self.assertEqual(self.task()['match']['tmdb_id'], 27205)
        self.assertEqual(self.add(torrent_b64=base64.b64encode(make_torrent(source)).decode())['message'], '这个资源已在加片列表中')
        two = 'magnet:?xt=urn:btih:' + 'b' * 40 + '&dn=A\nmagnet:?xt=urn:btih:' + 'c' * 40
        for link, words in [('ed2k://|file|a.mkv|1|ABC|/', 'ed2k'), ('https://example.com/a.mkv', '只支持磁力'), ('magnet:?dn=x', '一次只能'), (two, '一次只能'),
                            ('magnet:?xt=urn:btih:' + 'd' * 40 + '&xt=urn:btih:' + 'e' * 40, '一次只能'), ('magnet:?xt=urn:btih:zz', 'btih')]:
            with self.assertRaises(Problem) as error: self.add(link=link)
            self.assertIn(words, str(error.exception), link)
        for raw in (b'd4:infoi1ee', b'd4:infod5:filesld4:pathli1eeeeee', b'not bencode'):
            with self.assertRaises(Problem) as error: self.add(torrent_b64=base64.b64encode(raw).decode())
            self.assertEqual(str(error.exception), '不是有效的种子文件')
        self.assertEqual(len(self.qb.added), 1)

    def test_magnet_without_name_waits_for_metadata(self):
        self.apps.search['Inception'] = [movie(27205, '盗梦空间', 2010, 'Inception')]
        h = 'd' * 40
        self.add(link='magnet:?xt=urn:btih:' + h)
        self.assertEqual(self.task()['stage'], 'waiting_name')
        self.qb.torrents[h].update(state='metadata', name=h); self.store.media_add_step()
        self.assertEqual(self.task()['stage'], 'waiting_name')
        self.qb.torrents[h].update(state='downloading', name='Inception.2010.1080p.BluRay'); self.store.media_add_step()
        t = self.task(); self.assertEqual((t['name'], t['stage'], t['match']['tmdb_id']), ('Inception.2010.1080p.BluRay', 'matched', 27205))

    def test_outages_failures_and_retry(self):
        self.apps.search = 'down'
        self.add(link='magnet:?xt=urn:btih:' + 'e' * 40 + '&dn=Inception.2010')
        t = self.task(); self.assertEqual(t['stage'], 'identifying'); self.assertIn('5 分钟后自动重试', t['error'])
        self.store.media_add_step(); self.assertEqual(sum(c[0] == 'seerr' for c in self.apps.calls), 1)   # no spinning on an outage
        self.apps.search = {'Inception': [movie(27205, '盗梦空间', 2010, 'Inception')]}
        with self.store.db() as db:
            row = json.loads(db.execute('SELECT body FROM media_add').fetchone()[0]); row['identify_after'] = 0
            self.store._media_add_save(db, row)
        self.store.media_add_step(); self.assertEqual(self.task()['stage'], 'matched')
        self.apps.command_status = 'failed'
        self.finish(t, {'Inception.mkv': GB}); self.store.media_add_step(); self.store.media_add_step()
        t = self.task(); self.assertEqual(t['stage'], 'failed'); self.assertIn('Import failed', t['error'])
        self.assertEqual(self.store.media_add_issues()[0]['title'], '1 个加片任务失败')
        self.apps.command_status = 'completed'
        self.store.media_add_action({'action': 'retry', 'id': t['id']}, 'admin'); self.store.media_add_step(); self.store.media_add_step()
        self.assertEqual(self.task()['stage'], 'imported')

    def test_deleted_in_qbittorrent_then_pasted_again(self):
        self.apps.search['Inception'] = [movie(27205, '盗梦空间', 2010, 'Inception')]
        link = 'magnet:?xt=urn:btih:' + 'e' * 40 + '&dn=Inception.2010'
        self.add(link=link); self.qb.torrents.clear(); self.store.media_add_step()
        t = self.task(); self.assertEqual(t['stage'], 'failed'); self.assertIn('请重新贴链接', t['error'])
        self.assertEqual(self.add(link=link)['message'], '已重新开始下载')
        t = self.task(); self.assertEqual((t['stage'], t['error']), ('matched', '')); self.assertEqual(len(self.store.media_add_list()['tasks']), 1)
        self.assertEqual(self.qb.added[-1][0], t['id'])   # same task, same folder

    def test_ordinary_files_are_only_downloaded(self):
        source = Path(self.tmp.name) / 'tools'; source.mkdir(); (source / 'setup.exe').write_bytes(b'x' * 100); (source / 'readme.txt').write_bytes(b'r')
        self.assertIn('只下载、不入库', self.add(torrent_b64=base64.b64encode(make_torrent(source)).decode())['message'])
        t = self.task(); self.assertEqual(t['stage'], 'file'); self.assertFalse(any(c[0] == 'seerr' for c in self.apps.calls))
        self.qb.complete(self.qb.added[-1][1]['infohash']); self.store.media_add_step()
        self.assertEqual(self.task()['stage'], 'downloaded'); self.assertFalse(self.apps.posted('radarr', '/api/v3/movie'))
        self.apps.search['Ubuntu'] = [movie(5, 'Ubuntu', 2024)]
        self.add(link='magnet:?xt=urn:btih:' + '0' * 40 + '&dn=Ubuntu.24.04.iso')   # a magnet: known only once qB has the file list
        self.qb.files['0' * 40] = [{'name': 'ubuntu-24.04.iso'}]; self.store.media_add_step()
        self.assertEqual(self.task('Ubuntu.24.04.iso')['stage'], 'file')
        self.apps.search['Some Show'] = [movie(6, 'Some Show', 2020)]
        self.add(link='magnet:?xt=urn:btih:' + 'a1' * 20 + '&dn=Some.Show.2020', kind='movie')   # matched, but the download holds no video
        self.assertEqual(self.task('Some.Show.2020')['stage'], 'matched')
        self.finish(None, {'readme.txt': 10}); self.store.media_add_step()
        t = self.task('Some.Show.2020'); self.assertEqual(t['stage'], 'downloaded'); self.assertIn('普通文件', t['notes'][0])
        self.assertFalse(self.apps.posted('radarr', '/api/v3/command'))

    def test_unreachable_radarr_is_retried_not_failed(self):
        self.apps.search['Inception'] = [movie(27205, '盗梦空间', 2010, 'Inception')]
        self.add(link='magnet:?xt=urn:btih:' + '9' * 40 + '&dn=Inception.2010')
        self.finish(None, {'Inception.mkv': GB})
        real = type(self.store)._native
        def down(store, config, app, *args, **kwargs):
            if app == 'radarr': raise media_add.NativeError('radarr 无法连接')
            return real(store, config, app, *args, **kwargs)
        with patch.object(type(self.store), '_native', down): self.store.media_add_step()
        t = self.task(); self.assertEqual(t['stage'], 'matched'); self.assertIn('稍后自动重试', t['error'])
        self.store.media_add_step(); self.store.media_add_step()
        self.assertEqual(self.task()['stage'], 'imported')

    def test_qbittorrent_unreachable_keeps_the_task_and_backs_off_on_a_wrong_password(self):
        self.apps.search['Inception'] = [movie(27205, '盗梦空间', 2010, 'Inception')]
        self.add(link='magnet:?xt=urn:btih:' + 'f' * 40 + '&dn=Inception.2010')
        with patch.object(FakeQB, 'status', side_effect=NetError('unreachable', '无法连接')): self.store.media_add_step()
        t = self.task(); self.assertEqual(t['stage'], 'matched'); self.assertIn('暂时连不上内置 qBittorrent', t['error'])
        self.store.media_add_step(); self.assertEqual(self.task()['error'], '')
        with patch.object(FakeQB, 'status', side_effect=NetError('auth', '密码错误')) as status:
            self.store.media_add_step(); self.store.media_add_step(); self.store.media_add_step()
            self.assertEqual(status.call_count, 1)   # qBittorrent bans after 5 refused logins: no retry with the same password
            self.assertIn('密码管理', self.task()['error'])
            with self.assertRaises(Problem): self.add(link='magnet:?xt=urn:btih:' + 'ab' * 20)
            (self.data / 'download-secrets.json').write_text(json.dumps({'qbittorrent': {'password': 'new-test-password'}}))
            self.store.media_add_step()
            self.assertEqual(status.call_count, 2)   # a new password is tried at once

    def test_owner_action_is_not_overwritten_and_no_import_runs_on_a_stale_task(self):
        self.apps.search['Home'] = [movie(1, 'Home', 2009), movie(2, 'Home', 2015)]
        self.add(link='magnet:?xt=urn:btih:' + '1' * 40 + '&dn=Home.mkv')
        with self.store.db() as db: stale = json.loads(db.execute('SELECT body FROM media_add').fetchone()[0])
        self.store.media_add_action({'action': 'match', 'id': stale['id'], 'media_type': 'movie', 'tmdb_id': 2}, 'admin')
        stale['stage'] = 'needs_match'
        with self.store.db() as db: self.assertFalse(self.store._media_add_save(db, stale, stale['rev']))
        self.assertEqual(self.task()['stage'], 'matched')
        old = self.store._media_add_get(stale['id']); old['match'] = {'media_type': 'movie', 'tmdb_id': 1}
        self.finish(None, {'Home.mkv': GB})
        with self.store.db() as db:   # the owner changes the task while an import step is under way
            current = json.loads(db.execute('SELECT body FROM media_add').fetchone()[0]); self.store._media_add_save(db, current)
        self.store._media_add_run(self.store._media_add_import, self.store.media_center_config(), old)
        self.assertFalse(self.apps.posted('radarr', '/api/v3/command'))

    def test_remove_deletes_only_unimported_downloads_and_is_admin_only_over_http(self):
        self.apps.search['Inception'] = [movie(27205, '盗梦空间', 2010, 'Inception')]
        status, _, body = self.request('/api/admin/media-add', {'action': 'add', 'link': 'magnet:?xt=urn:btih:' + '2' * 40 + '&dn=Inception.2010', 'revision': self.state()['revision']}, auth=True)
        self.assertEqual(status, 200); ident = body['tasks'][0]['id']
        raw = json.dumps(self.request('/api/admin/media-add', auth=True)[2])
        for secret in list(KEYS.values()) + ['qb-test-password', 'magnet:']: self.assertNotIn(secret, raw)
        self.assertNotIn('"dir"', raw)
        with self.store.db() as db:
            from management import password_hash
            db.execute("INSERT INTO users VALUES('op',?,'operator',1)", (password_hash('operator-password-123'),))
        status, headers, login = self.request('/api/admin/login', {'username': 'op', 'password': 'operator-password-123'})
        op = {'Cookie': headers['Set-Cookie'].split(';')[0], 'X-CSRF-Token': login['user']['csrf']}
        status, _, _ = self.request('/api/admin/media-add', {'action': 'remove', 'id': ident}, headers=op)
        self.assertEqual(status, 403)
        status, _, body = self.request('/api/admin/media-add', {'action': 'remove', 'id': ident}, auth=True)
        self.assertEqual(status, 200); self.assertIn('删除了还没入库的下载文件', body['message']); self.assertEqual(self.qb.deleted, ['2' * 40])
        self.assertEqual(body['tasks'], [])
        # An imported task only leaves the list: the library file stays and the torrent keeps seeding.
        self.add(link='magnet:?xt=urn:btih:' + '4' * 40 + '&dn=Inception.2010'); self.finish(None, {'Inception.mkv': GB})
        self.store.media_add_step(); self.store.media_add_step(); t = self.task(); self.assertEqual(t['stage'], 'imported')
        self.assertIn('仍在片库中', self.store.media_add_action({'action': 'remove', 'id': t['id']}, 'admin')['message'])
        self.assertEqual(self.qb.deleted, ['2' * 40]); self.assertIn('4' * 40, self.qb.torrents)
        with patch.object(FakeQB, '_info', side_effect=NetError('unreachable', '无法连接')):
            self.add(link='magnet:?xt=urn:btih:' + '3' * 40 + '&dn=Other.2019')
            with self.assertRaises(Problem) as error: self.store.media_add_action({'action': 'remove', 'id': self.task('Other.2019')['id']}, 'admin')
        self.assertIn('任务未移除', str(error.exception)); self.assertEqual(self.task('Other.2019')['stage'] != 'removed', True)
        self.qb.torrents['3' * 40]['tags'] = 'hoteltv,hoteltv-somebody-else'   # the hash now belongs to another download task
        self.assertIn('没有删除', self.store.media_add_action({'action': 'remove', 'id': self.task('Other.2019')['id']}, 'admin')['message'])
        self.assertNotIn('3' * 40, self.qb.deleted)

    def test_remove_cannot_delete_a_download_while_native_import_is_being_sent(self):
        self.apps.search['Inception'] = [movie(27205, '盗梦空间', 2010, 'Inception')]
        self.add(link='magnet:?xt=urn:btih:' + 'b1' * 20 + '&dn=Inception.2010')
        self.finish(None, {'Inception.mkv': GB})
        task = self.store._media_add_get(self.task()['id'])
        entered, release = threading.Event(), threading.Event()
        native = type(self.store)._native
        errors = []
        def blocked_import(store, config, app, path, *args, **kwargs):
            if app == 'radarr' and path == '/api/v3/command':
                entered.set()
                if not release.wait(10): raise RuntimeError('test import barrier timed out')
            return native(store, config, app, path, *args, **kwargs)
        def import_task():
            try: self.store._media_add_run(self.store._media_add_import, self.store.media_center_config(), task)
            except Exception as exc: errors.append(exc)
        with patch.object(type(self.store), '_native', blocked_import):
            worker = threading.Thread(target=import_task); worker.start()
            try:
                self.assertTrue(entered.wait(10))
                with self.assertRaises(Problem):
                    self.store.media_add_action({'action': 'remove', 'id': task['id']}, 'admin')
                self.assertEqual(self.qb.deleted, [])
            finally:
                release.set(); worker.join(10)
        self.assertFalse(worker.is_alive()); self.assertEqual(errors, [])
        self.assertEqual(self.task()['stage'], 'importing')
        self.assertIn('b1' * 20, self.qb.torrents)

    def test_listing_requires_an_available_unarchived_jellyfin_item(self):
        from management import now_ms
        self.apps.search['Inception'] = [movie(27205, '盗梦空间', 2010, 'Inception')]
        self.add(link='magnet:?xt=urn:btih:' + 'b2' * 20 + '&dn=Inception.2010')
        task = self.store._media_add_get(self.task()['id'])
        task.update(stage='imported', arr={'path': self.apps.root + '/media/电影/test'}, imported_at=now_ms())
        with self.store.db() as db: self.store._media_add_save(db, task)
        self.appear_in_catalog(task)
        media_id = 'jf-' + task['id']
        with self.store.db() as db:
            original = json.loads(db.execute('SELECT body FROM media WHERE id=?', (media_id,)).fetchone()[0])
        for available, archived, jellyfin_id in ((0, False, 'b' * 32), (1, True, 'b' * 32), (1, False, None)):
            item = {**original, '_archived': archived, 'jellyfin_id': jellyfin_id}
            with self.store.db() as db:
                db.execute('UPDATE media SET body=?,available=? WHERE id=?', (json.dumps(item), available, media_id))
            self.store._media_add_listing(self.store._media_add_get(task['id']))
            self.assertEqual(self.task()['stage'], 'imported', (available, archived, jellyfin_id))
        with self.store.db() as db:
            db.execute('UPDATE media SET body=?,available=1 WHERE id=?', (json.dumps(original), media_id))
        self.store._media_add_listing(self.store._media_add_get(task['id']))
        self.assertEqual(self.task()['stage'], 'listed')

    def test_owner_action_lands_even_while_progress_is_being_saved(self):
        self.apps.search['Home'] = [movie(1, 'Home', 2009), movie(2, 'Home', 2015)]
        self.add(link='magnet:?xt=urn:btih:' + 'c1' * 20 + '&dn=Home.mkv')
        t = self.task(); real = type(self.store)._native
        def progress_meanwhile(store, config, app, path, *args, **kwargs):
            if app == 'seerr' and path.startswith('/api/v1/movie/'): store.media_add_step()   # the scheduler saves progress during the call
            return real(store, config, app, path, *args, **kwargs)
        self.qb.torrents['c1' * 20].update(downloaded=5)
        with patch.object(type(self.store), '_native', progress_meanwhile):
            self.store.media_add_action({'action': 'match', 'id': t['id'], 'media_type': 'movie', 'tmdb_id': 2}, 'admin')
        t = self.task(); self.assertEqual((t['stage'], t['match']['tmdb_id']), ('matched', 2)); self.assertEqual(t['download']['progress'], 0.5)

    def test_magnet_with_a_decoded_name_and_a_film_not_reaching_the_catalog(self):
        self.apps.search['Inception'] = [movie(27205, '盗梦空间', 2010, 'Inception')]
        self.add(link='magnet:?xt=urn:btih:' + 'd1' * 20 + '&dn=Inception 2010 1080p')
        self.assertIn('%20', self.qb.added[-1][1]['uri']); self.assertEqual(self.task()['name'], 'Inception 2010 1080p')
        self.finish(None, {'Inception.mkv': GB}); self.store.media_add_step(); self.store.media_add_step()
        self.assertEqual(self.task()['stage'], 'imported')
        with self.store.db() as db:
            row = json.loads(db.execute('SELECT body FROM media_add').fetchone()[0]); row['imported_at'] -= 21 * 60 * 1000; self.store._media_add_save(db, row)
        self.store.media_add_step()
        t = self.task(); self.assertEqual(t['stage'], 'imported'); self.assertIn('Jellyfin 同步', t['error'])
        self.assertEqual(self.store.media_add_issues()[0]['title'], '1 部影片已归档但没进入待上架')
        self.appear_in_catalog(t); self.store.media_add_step(); self.assertEqual(self.task()['stage'], 'listed')

    def test_search_and_poster_proxy(self):
        self.apps.search['沙丘'] = [movie(438631, '沙丘', 2021), tv(90228, '沙丘：预言', 2024)]
        self.assertEqual([r['tmdb_id'] for r in self.store.media_add_search('沙丘', 'movie')['results']], [438631])
        self.assertEqual(len(self.store.media_add_search('沙丘')['results']), 2)
        with self.assertRaises(Problem): self.store.media_add_poster('/../../etc/passwd')
        with patch.object(media_add, 'request', return_value=(200, {'content-type': 'image/jpeg'}, b'\xff\xd8jpeg', '')) as fetch:
            self.assertEqual(self.store.media_add_poster('/p438631.jpg'), (b'\xff\xd8jpeg', 'image/jpeg'))
            self.store.media_add_poster('/p438631.jpg')
        self.assertEqual(fetch.call_count, 1)   # cached on the server
        self.assertEqual(fetch.call_args[0][0], 'https://image.tmdb.org/t/p/w185/p438631.jpg')

    def test_rename_applies_the_chosen_title_in_jellyfin_and_drops_hotel_overrides(self):
        body = {'id': 'jf-x', 'title': '酒店改过的名字', 'kind': 'movie', 'year': '2009', 'description': '旧简介', 'poster': '/media/_posters/old.jpg',
                'jellyfin_id': 'a' * 32, 'path': '/media/clip.mp4', '_jellyfin_meta': {'title': 'Home', 'year': '2009', 'description': '旧简介', 'poster': '/media/_posters/old.jpg', 'category': '电影'}}
        with self.store.db() as db: db.execute('INSERT INTO media VALUES(?,?,?,?,1)', ('jf-x', 'main', json.dumps(body), 0))
        calls = []
        def jellyfin(route, payload=None, image=False, timeout=20):
            calls.append((route, payload, timeout))
            return [{'Name': '疯狂外星人', 'ProviderIds': {'Tmdb': '2'}}, {'Name': 'Other', 'ProviderIds': {'Tmdb': '9'}}] if '/RemoteSearch/Movie' in route else {}
        with self.assertRaises(Problem) as error: self.store.media_rename({'media_id': 'jf-x', 'media_type': 'tv', 'tmdb_id': 2}, 'admin')
        self.assertIn('不能互换', str(error.exception))    # a TV id applied as a movie would be an unrelated film
        class Now:
            def __init__(self, target, **kwargs): self.target = target
            def start(self): self.target()
        with patch.object(self.store, 'jellyfin_request', side_effect=jellyfin), patch.object(media_add.threading, 'Thread', Now):
            result = self.store.media_rename({'media_id': 'jf-x', 'media_type': 'movie', 'tmdb_id': 2}, 'admin')
        self.assertIn('疯狂外星人', result['message'])
        self.assertEqual(calls[0][:2], ('/Items/RemoteSearch/Movie', {'SearchInfo': {'ProviderIds': {'Tmdb': '2'}}, 'ItemId': 'a' * 32, 'IncludeDisabledProviders': False}))
        self.assertEqual(calls[1], ('/Items/RemoteSearch/Apply/' + 'a' * 32 + '?ReplaceAllImages=true', {'Name': '疯狂外星人', 'ProviderIds': {'Tmdb': '2'}}, 900))
        with self.store.db() as db: item = json.loads(db.execute("SELECT body FROM media WHERE id='jf-x'").fetchone()[0])
        self.assertEqual(item['title'], 'Home')   # overrides dropped before Apply: the next sync brings the corrected data
        self.assertEqual(item['_renaming']['status'],'awaiting_sync')
        def slow(route, payload=None, image=False, timeout=20):
            if '/Apply/' in route: raise Problem('Jellyfin 暂时无法访问或返回无效数据；现有片库继续保留', 503)
            return jellyfin(route, payload, image, timeout)
        with patch.object(self.store, 'jellyfin_request', side_effect=slow), patch.object(media_add.threading, 'Thread', Now):
            self.assertIn('正在改为', self.store.media_rename({'media_id': 'jf-x', 'media_type': 'movie', 'tmdb_id': 2}, 'admin')['message'])
        self.assertEqual(self.state()['audit'][0]['action'], '改片名未完成')
        with self.assertRaises(Problem) as error: self.store.media_rename({'media_id': 'existing', 'media_type': 'movie', 'tmdb_id': 2}, 'admin')
        self.assertIn('编辑', str(error.exception))

    def test_disabled_or_unconfigured_automation(self):
        with self.store.db() as db: self.store.put_kv(db, 'media_automation', {'enabled': False})
        with self.assertRaises(Problem) as error: self.add(link='magnet:?xt=urn:btih:' + '3' * 40)
        self.assertEqual(error.exception.status, 409)
        self.store.media_add_step()   # no-op, no crash
        self.assertEqual(self.qb.added, [])


if __name__ == '__main__':
    unittest.main()
