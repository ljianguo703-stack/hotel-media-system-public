#!/usr/bin/env python3
"""1.1.0 disposable browser fixture for the media automation center. Loopback only; fake native apps; no secrets."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from management import Store, password_hash
from server import make_server
from test_media_center import FakeNative, KEYS
import io
import media_add
from test_media_add import FakeQB

root = Path(tempfile.mkdtemp(prefix='hotel-v110-mock-'))
data, media = root / 'data', root / 'media'; data.mkdir(); media.mkdir()
config = root / 'devices.json'; config.write_text('{"devices":{"test-tv":{"token":"v110-mock-only","room":"101"}}}')
(data / 'catalog.json').write_text(json.dumps({'hotel': '1.1.0 隔离浏览器验收', 'items': []}, ensure_ascii=False))
(data / 'ads.json').write_text('{"ads":[]}')
native = FakeNative()
native.state['testall_valid'] = False                                           # broken downloader link
native.state['indexerstatus'] = [{'indexerId': 2, 'disabledTill': '2099-01-01'}]  # Knaben failing
integration = {app: {'url': native.url(app), 'api_key': key} for app, key in KEYS.items()}
integration['radarr'].update(format=9, profile=4); integration['sonarr'].update(format=7, profile=6)
integration.update(storage_root=str(media), compat_root=str(media / '_compat'))
(data / 'media-automation.json').write_text(json.dumps(integration))
(data / 'download-secrets.json').write_text(json.dumps({'qbittorrent': {'password': 'v110-mock-test-only'}}))
store = Store(data, media, config, [media])
with store.db() as db:
    db.execute('UPDATE users SET password=? WHERE name=?', (password_hash('v110-mock-test-only'), 'admin'))
    store.put_kv(db, 'media_automation', {'enabled': True}); store.put_kv(db, 'media_policy', {'hevc': 'allow'})
native.state['heal_on_write'] = True
native.state['bazarr']['subsync'] = {'use_subsync': True, 'use_subsync_threshold': True,
                                   'use_subsync_movie_threshold': True, 'subsync_movie_threshold': 70}
# 1.1.0 加片: TMDB answers through the fake Seerr, the built-in qBittorrent is a fake, posters are generated.
def result(i, title, year, kind='movie', original=''):
    key = 'title' if kind == 'movie' else 'name'
    return {'id': i, 'mediaType': kind, key: title, ('originalTitle' if kind == 'movie' else 'originalName'): original or title,
            ('releaseDate' if kind == 'movie' else 'firstAirDate'): '%d-01-01' % year, 'posterPath': '/mock%06d.jpg' % i, 'externalIds': {'tvdbId': 1000 + i}}
native.state['search_results'] = [result(9470, '功夫', 2004, original='Kung Fu Hustle'), result(1, 'Home', 2009), result(2, 'Home', 2015, original='Home'),
                                  result(842675, '流浪地球2', 2023, original='The Wandering Earth II'), result(209867, '繁花', 2023, 'tv')]
qb = FakeQB()
Store._media_add_engine = lambda self, config: qb
def poster(url, **kwargs):
    from PIL import Image
    seed = sum(map(ord, url)); buffer = io.BytesIO()
    Image.new('RGB', (120, 180), ((seed * 37) % 200 + 30, (seed * 11) % 160 + 40, (seed * 7) % 180 + 50)).save(buffer, 'JPEG')
    return 200, {'content-type': 'image/jpeg'}, buffer.getvalue(), url
media_add.request = poster
store.media_add_action({'action': 'add', 'link': 'magnet:?xt=urn:btih:' + '1' * 40 + '&dn=功夫.Kung.Fu.Hustle.2004.1080p.BluRay.x264'}, 'admin')
store.media_add_action({'action': 'add', 'link': 'magnet:?xt=urn:btih:' + '2' * 40 + '&dn=Home.mkv'}, 'admin')
store.media_add_action({'action': 'add', 'link': 'magnet:?xt=urn:btih:' + '3' * 40 + '&dn=[电影天堂www.dytt89.com]流浪地球2-2023_HD国语中字.mp4'}, 'admin')
store.media_add_action({'action': 'add', 'link': 'magnet:?xt=urn:btih:' + '5' * 40 + '&dn=Office.Tools.2024', 'kind': 'file'}, 'admin')
# IN17 file/episode confirmation: the same persisted shape returned by native import checks.
with store.db() as db:
    for digit,stage,title,kind in [('6','needs_file','多视频正片确认','movie'),('7','needs_episodes','分集顺序确认','series')]:
        ident='add-selection-'+digit
        store._media_add_save(db,{'id':ident,'hash':digit*40,'name':title,'kind':kind,'stage':stage,'created_at':int(time.time()*1000),
            'match':{'title':title,'media_type':'tv' if kind=='series' else 'movie','tmdb_id':int(digit),'year':2024},
            'download':{'state':'complete','complete':True,'progress':1},'file_candidates':[{'path':name,'size':1024**3} for name in ['资源文件夹/First.1080p.mkv','资源文件夹/Second.1080p.mkv']],
            'error':'请确认文件或集号','notes':[]})
qb.torrents['1' * 40].update(downloaded=7, total=10); qb.torrents['3' * 40].update(state='complete', downloaded=10)
with store.db() as db:   # one finished and one failed task, as the scheduler would leave them
    rows = {json.loads(r[0])['hash']: json.loads(r[0]) for r in db.execute('SELECT body FROM media_add')}
    done = rows['3' * 40]; done.update(stage='listed', notes=['已进入待上架（未上架）：《流浪地球2》']); done['download'].update(complete=True, progress=1); store._media_add_save(db, done)
    (media / 'jf').mkdir(); (media / 'jf' / 'home.mkv').write_bytes(b'0' * 64)
    item = {'id': 'jf-mock', 'title': 'Home', 'kind': 'movie', 'year': '2009', 'category': '电影', 'description': '认错的影片', 'poster': '',
            'jellyfin_id': 'a' * 32, 'path': '/media/jf/home.mkv', '_jellyfin_meta': {'title': 'Home', 'year': '2009', 'description': '认错的影片', 'poster': '', 'category': '电影'}}
    db.execute('INSERT INTO media VALUES(?,?,?,?,1)', ('jf-mock', 'main', json.dumps(item, ensure_ascii=False), 0))
def jellyfin(route, body=None, image=False, timeout=20):
    if '/RemoteSearch/Apply/' in route: return {}
    return [{'Name': '流浪地球2', 'ProviderIds': {'Tmdb': str(body['SearchInfo']['ProviderIds']['Tmdb'])}}]
store.jellyfin_request = jellyfin
servers = [make_server('127.0.0.1', 0, config, data, media, store=store),
           make_server('127.0.0.1', 0, config, data, media, store=store, admin_web=Path(__file__).resolve().parents[1] / 'admin')]
for server in servers: threading.Thread(target=server.serve_forever, daemon=True).start()
info = {'url': 'http://127.0.0.1:' + str(servers[1].server_port), 'fixture_root': str(root)}
output = Path(__file__).resolve().parents[1] / 'local/evidence/V110'; output.mkdir(parents=True, exist_ok=True)
(output / 'mock-info.json').write_text(json.dumps(info)); print(json.dumps(info), flush=True)
try:
    while True: time.sleep(1)
except KeyboardInterrupt:
    for server in servers: server.shutdown(); server.server_close()
    native.close(); shutil.rmtree(root)
