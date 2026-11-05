#!/usr/bin/env python3
"""Fresh-host native media setup. Resumable; never imports a previous hotel's credentials/data."""
import argparse
import copy
import http.cookiejar
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import build_opener, HTTPCookieProcessor, ProxyHandler, Request

ROOT = Path(__file__).resolve().parents[2]
WATCH_MARKER = 'hotel-library-root.txt'
COMPAT_DIR = '.hotel-compat'   # AAC copies stay on the media disk, beside downloads/ and media/, never scanned
INTEGRATION = Path('/srv/hotel-tv/data/media-automation.json')
# hotel-tv.service runs with ProtectSystem=strict and may write only /srv/hotel-tv/{data,media}. A data disk elsewhere
# holds the AAC copies (.hotel-compat) and the 加片 links of series Sonarr cannot manage, so it is added by a drop-in.
SERVICE_DROPIN = Path('/etc/systemd/system/hotel-tv.service.d/50-media-storage.conf')
# Native UIs sit behind the hotel admin gateway (loopback ports + admin session), so their own first-run
# login dialog is replaced by External auth: one login for the owner, API keys still required for APIs.
AUTH_ENV = {'radarr': 'RADARR__AUTH__METHOD', 'sonarr': 'SONARR__AUTH__METHOD'}
# HEVC policy -> (custom format score, profile minimum score). Default allows HEVC: the TV plays the
# original file with its own decoder, and the existing precheck already treats HEVC as directly playable.
HEVC_POLICIES = {'allow': (0, 0), 'prefer_h264': (-100, -10000), 'h264_only': (-10000, 0)}
DEFAULT_HEVC_POLICY = 'allow'
WATCH_CONTENT = 'Hotel installer library root marker; not media. Keep this file so an empty library remains monitored.\n'
sys.path.insert(0, str(ROOT / 'deploy'))
from bootstrap import private_json, service_identity, validate_storage
from docker_images import pull_image
from media_quality import allow_library_qualities


class Native:
    def __init__(self, base, headers=None):
        self.base, self.headers = base.rstrip('/'), headers or {}
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, path, body=None, method=None, form=False):
        headers = dict(self.headers)
        data = None
        if body is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
            data = (urlencode(body, doseq=True) if form else json.dumps(body)).encode()
        request = Request(self.base + path, data=data, headers=headers, method=method)
        with self.opener.open(request, timeout=45) as response:
            raw = response.read(32 * 1024 * 1024)
            if not raw: return None
            try: return json.loads(raw)
            except ValueError: return raw.decode()

    def wait(self, path):
        for attempt in range(180):
            try: return self.call(path)
            except HTTPError as e:
                if e.code not in (502, 503, 504): raise
            except (URLError, OSError, ValueError): pass
            time.sleep(1)
        raise RuntimeError('原生应用启动超时：' + self.base.split(':')[-1])


def run(args):
    return subprocess.run([str(x) for x in args], check=True, stdout=subprocess.DEVNULL)


def inspect(name):
    return json.loads(subprocess.check_output(['docker', 'inspect', name]))[0]


def app_key(name):
    path = Path('/srv/nas/appdata') / name / 'config.xml'
    for _ in range(180):
        if path.exists():
            try:
                key = ET.fromstring(path.read_text()).findtext('ApiKey')
                if key: return key
            except ET.ParseError: pass
        time.sleep(1)
    raise RuntimeError(name + ' 尚未生成私有API配置')


def hardlink_probe(root):
    """Use only unpredictable, exclusively-created probe files. Never touch existing media."""
    root = Path(root)
    probe = root / ('.install-probe-' + uuid.uuid4().hex)
    probe.mkdir(mode=0o700)
    try:
        source, target = probe / 'download', probe / 'library'
        with source.open('xb') as f: f.write(b'hotel hardlink verification')
        os.link(source, target)
        a, b = source.stat(), target.stat()
        if (a.st_dev, a.st_ino, a.st_nlink) != (b.st_dev, b.st_ino, 2):
            raise RuntimeError('所选目录不支持硬链接，请选择同一Linux文件系统')
        return {'device': a.st_dev, 'hardlink': True}
    finally:
        for name in ('library', 'download'): (probe / name).unlink(missing_ok=True)
        probe.rmdir()


def field_values(model, values):
    result = copy.deepcopy(model)
    for field in result.get('fields', []):
        if field['name'] in values: field['value'] = values[field['name']]
    return result


def ensure(client, endpoint, model):
    """Owned names only; after a crash reuse exact objects rather than duplicating clients."""
    existing = next((x for x in client.call(endpoint) if x.get('name') == model['name']), None)
    if existing:
        # Do not send API-masked credentials back as real passwords when recovering a partial phase.
        return existing
    client.call(endpoint + '/test', model, 'POST')
    return client.call(endpoint, model, 'POST')


class Setup:
    def __init__(self, state):
        self.state = state
        self.storage = Path(validate_storage(state.get('storage_root', '/srv/hotel-tv/media/.automation')))
        self.uid, self.gid = pwd.getpwnam(state['operator']).pw_uid, pwd.getpwnam('hotel-tv').pw_gid
        self.track = Path('/etc/hotel-tv/media-install-state.json')
        self.status = json.loads(self.track.read_text()) if self.track.exists() else {'completed': [], 'external': {}}
        self.owner = state.get('install_id', state['release'])
        self.clients = {}

    def phase(self, name, action):
        retry_external = name == 'indexers' and any('not verified' in self.status['external'].get(k, '') for k in ('Knaben', 'Nyaa.si'))
        if name in self.status['completed'] and not retry_external: return
        print('片库阶段：' + name, flush=True)
        action()
        if name not in self.status['completed']: self.status['completed'].append(name)
        private_json(self.track, self.status)

    def storage_prepare(self):
        if str(self.storage) != '/srv/hotel-tv/media/.automation' and not self.storage.parent.is_dir():
            raise RuntimeError('指定媒体盘父目录不存在；请先自行挂载磁盘，安装器不分区或挂盘')
        marker = self.storage / '.hotel-media-install.json'
        if self.storage.exists() and any(self.storage.iterdir()):
            if not marker.exists() or json.loads(marker.read_text()).get('owner') != self.owner:
                raise RuntimeError('指定目录含非本安装器数据，未修改；请选择新的独立目录')
        for rel in ('', 'downloads/movies', 'downloads/tv', 'downloads/manual', 'media/电影', 'media/电视剧'):
            p = self.storage / rel; p.mkdir(parents=True, exist_ok=True)
            os.chown(p, self.uid, self.gid); p.chmod(0o2775)
        self.compat_dir()
        self.status['storage'] = hardlink_probe(self.storage)
        private_json(marker, {'owner': self.owner})
        # Only the download subtree is excluded from hotel manual scans.
        marker = self.storage / 'downloads/.hotel-download-task'
        marker.touch(exist_ok=True)
        for stack, service, readonly in [('hotel-qbittorrent', 'qbittorrent', False), ('jellyfin', 'jellyfin', True)]:
            path = Path('/opt/stacks') / stack / 'compose.yaml'
            import yaml
            config = yaml.safe_load(path.read_text())
            svc = next(v for v in config['services'].values() if v.get('container_name') in ('hotel-qbittorrent', 'nas-jellyfin'))
            mount = str(self.storage) + ':' + str(self.storage) + (':ro' if readonly else '')
            if mount not in svc['volumes']: svc['volumes'].append(mount)
            private_json(path, config)
            run(['docker', 'compose', '-f', path, 'config', '--quiet'])
            run(['docker', 'compose', '-f', path, 'up', '-d'])

    def storage_access(self):
        inside = Path('/srv/hotel-tv/media')
        if self.storage == inside or inside in self.storage.parents: return False
        SERVICE_DROPIN.parent.mkdir(parents=True, exist_ok=True)
        wanted = '[Service]\nReadWritePaths="%s"\n' % self.storage   # validate_storage already refused quotes, backslashes and %
        if not SERVICE_DROPIN.exists() or SERVICE_DROPIN.read_text() != wanted:
            temp = SERVICE_DROPIN.with_name('.' + SERVICE_DROPIN.name + '.hotel'); temp.write_text(wanted); temp.chmod(0o644); os.replace(temp, SERVICE_DROPIN)
        run(['systemctl', 'daemon-reload'])
        return True

    def compat_dir(self):
        hotel = pwd.getpwnam('hotel-tv')
        path = self.storage / COMPAT_DIR
        if path.is_symlink(): raise RuntimeError('兼容副本目录异常，未覆盖')
        path.mkdir(exist_ok=True); os.chown(path, hotel.pw_uid, hotel.pw_gid); path.chmod(0o750)
        return path

    def containers(self):
        network = 'hotel-media-automation'
        net = subprocess.run(['docker', 'network', 'inspect', network], capture_output=True)
        if net.returncode:
            run(['docker', 'network', 'create', '--label', 'hotel.setup=' + self.owner, network])
        elif json.loads(net.stdout)[0].get('Labels', {}).get('hotel.setup') != self.owner:
            raise RuntimeError('发现其他安装创建的片库网络，未覆盖')
        external = {'default': {'external': True, 'name': network}}
        for app, container in [('qb', 'hotel-qbittorrent'), ('jf', 'nas-jellyfin'), ('prowlarr', 'hotel-prowlarr')]:
            info = inspect(container); nets = list(info['NetworkSettings']['Networks'])
            preferred = info['Config']['Labels']['com.docker.compose.project'] + '_default'
            chosen = preferred if preferred in nets else (nets[0] if len(nets) == 1 else None)
            if not chosen: raise RuntimeError('首装容器网络与预期不符：' + container)
            external[app] = {'external': True, 'name': chosen}
        images = json.loads((ROOT / 'deploy/images.lock.json').read_text())
        for name, port in [('radarr', 7878), ('sonarr', 8989), ('bazarr', 6767), ('seerr', 5055)]:
            path = Path('/opt/stacks') / name / 'compose.yaml'
            conf = Path('/srv/nas/appdata') / name
            if path.exists():
                value = json.loads(path.read_text())
                if value.get('x-hotel-setup') != self.owner: raise RuntimeError('发现已有栈，未覆盖：' + name)
                pull_image(value['services'][name]['image'])
                env = value['services'][name].setdefault('environment', {})
                if name in AUTH_ENV and env.get(AUTH_ENV[name]) != 'External':
                    env[AUTH_ENV[name]] = 'External'; private_json(path, value)
                    run(['docker', 'compose', '-f', path, 'config', '--quiet'])
                run(['docker', 'compose', '-f', path, 'up', '-d']); continue
            with socket.socket() as sock:
                if sock.connect_ex(('127.0.0.1', port)) == 0: raise RuntimeError('新服务端口已被占用：' + str(port))
            pull_image(images[name])
            conf.mkdir(parents=True, exist_ok=True); os.chown(conf, self.uid, self.gid); conf.chmod(0o700)
            service = {'image': images[name], 'container_name': 'nas-' + name, 'restart': 'unless-stopped',
                       'ports': [f'127.0.0.1:{port}:{port}'], 'environment': {'TZ': self.state['timezone']},
                       'volumes': [str(conf) + (':/app/config' if name == 'seerr' else ':/config')],
                       'networks': ['default'], 'security_opt': ['no-new-privileges:true'],
                       'logging': {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}},
                       'mem_limit': '1g', 'cpus': 1, 'pids_limit': 512}
            if name == 'seerr':
                service.update(user=f'{self.uid}:{self.gid}', init=True, cap_drop=['ALL'])
                service['environment'].update(LOG_LEVEL='info', CONFIG_DIRECTORY='/app/config')
                service['networks'].append('jf')
            else:
                service['environment'].update(PUID=str(self.uid), PGID=str(self.gid), UMASK='002')
                if name in AUTH_ENV: service['environment'][AUTH_ENV[name]] = 'External'
                service['volumes'].append(str(self.storage) + ':' + str(self.storage))
                if name != 'bazarr': service['networks'] += ['qb', 'jf', 'prowlarr']
            value = {'x-hotel-setup': self.owner, 'services': {name: service},
                     'networks': {key: external[key] for key in service['networks']}}
            private_json(path, value)
            run(['docker', 'compose', '-f', path, 'config', '--quiet'])
            run(['docker', 'compose', '-f', path, 'up', '-d'])

    def single_login(self, apps=(('radarr', 'nas-radarr', 7878, 'v3'), ('sonarr', 'nas-sonarr', 8989, 'v3'), ('prowlarr', 'hotel-prowlarr', 9696, 'v1'))):
        """Write External auth into each config.xml (read by every pinned version), so the owner never meets a
        second first-run login dialog behind the hotel gateway. APIs keep requiring their keys."""
        for name, container, port, api in apps:
            app_key(name)   # waits until the app has created its config
            path = Path('/srv/nas/appdata') / name / 'config.xml'
            root = ET.fromstring(path.read_text())
            wanted = {'AuthenticationMethod': 'External', 'AuthenticationRequired': 'Enabled'}
            if all((root.findtext(k) or '') == v for k, v in wanted.items()):
                run(['docker', 'start', container])   # idempotent; recovers a container left stopped by an interrupted run
                Native(f'http://127.0.0.1:{port}/api/{api}', {'X-Api-Key': app_key(name)}).wait('/system/status'); continue
            run(['docker', 'stop', container])
            try:
                root = ET.fromstring(path.read_text())   # re-read after the app flushed its config on stop
                for tag, value in wanted.items():
                    node = root.find(tag)
                    if node is None: node = ET.SubElement(root, tag)
                    node.text = value
                st = path.stat(); temp = path.with_name('.' + path.name + '.hotel')
                temp.write_text(ET.tostring(root, encoding='unicode')); os.chown(temp, st.st_uid, st.st_gid); temp.chmod(st.st_mode & 0o777)
                os.replace(temp, path)
            finally:
                run(['docker', 'start', container])
            Native(f'http://127.0.0.1:{port}/api/{api}', {'X-Api-Key': app_key(name)}).wait('/system/status')

    def watch_roots(self):
        # Jellyfin skips empty physical roots and never installs their directory watcher.
        # A non-media text marker materializes both roots without adding a fake movie/episode.
        for name in ('电影', '电视剧'):
            path = self.storage / 'media' / name / WATCH_MARKER
            try:
                with path.open('x') as f: f.write(WATCH_CONTENT)
            except FileExistsError:
                if path.is_symlink() or not path.is_file() or path.read_text() != WATCH_CONTENT:
                    raise RuntimeError('片库监听标记同名文件不同，未覆盖')
            os.chown(path, self.uid, self.gid); path.chmod(0o644)

    def arr(self, name):
        if name not in self.clients:
            port = 7878 if name == 'radarr' else 8989
            self.clients[name] = Native(f'http://127.0.0.1:{port}/api/v3', {'X-Api-Key': app_key(name)})
            self.clients[name].wait('/system/status')
        return self.clients[name]

    def jellyfin(self):
        config = json.loads(Path('/srv/hotel-tv/data/nas-integration.json').read_text())
        return Native(config['url'], {'Authorization': 'MediaBrowser Token="' + config['token'] + '"'})

    def downloaders(self):
        qb = Native('http://127.0.0.1:8098/api/v2', {'Origin': 'http://127.0.0.1:8098', 'Referer': 'http://127.0.0.1:8098/'})
        qb.call('/auth/login', {'username': 'admin', 'password': self.state.get('service_passwords',{}).get('qbittorrent',self.state['nas_password'])}, 'POST', form=True)
        cats = qb.call('/torrents/categories')
        for name, rel in [('radarr', 'movies'), ('sonarr', 'tv')]:
            target = str(self.storage / 'downloads' / rel)
            if name in cats:
                if cats[name]['savePath'] != target: raise RuntimeError('发现不同保存路径的分类：' + name)
            else: qb.call('/torrents/createCategory', {'category': name, 'savePath': target}, 'POST', form=True)
            client = self.arr(name)
            media = client.call('/config/mediamanagement'); media['copyUsingHardlinks'] = True; media['minimumFreeSpaceWhenImporting']=5120
            client.call('/config/mediamanagement', media, 'PUT')
            naming = client.call('/config/naming')
            if name == 'radarr':
                naming.update(renameMovies=True, movieFolderFormat='{Movie CleanTitle} ({Release Year}) [tmdbid-{TmdbId}]', standardMovieFormat='{Movie CleanTitle} ({Release Year}) [tmdbid-{TmdbId}] - {Quality Full}')
            else: naming.update(renameEpisodes=True, seriesFolderFormat='{Series Title} [tvdbid-{TvdbId}]', seasonFolderFormat='Season {season:00}')
            client.call('/config/naming', naming, 'PUT')
            root = str(self.storage / 'media' / ('电影' if name == 'radarr' else '电视剧'))
            if not any(x['path'] == root for x in client.call('/rootfolder')): client.call('/rootfolder', {'path': root}, 'POST')
            model = next(x for x in client.call('/downloadclient/schema') if x['implementation'] == 'QBittorrent')
            model = field_values(model, {'host': 'hotel-qbittorrent', 'port': 8098, 'username': 'admin', 'password': self.state.get('service_passwords',{}).get('qbittorrent',self.state['nas_password']), 'movieCategory': name, 'tvCategory': name})
            model.update(name='Hotel qB', enable=True, removeCompletedDownloads=False, removeFailedDownloads=False)
            ensure(client, '/downloadclient', model)
            profile = next(x for x in client.call('/qualityprofile') if x['name'] == 'HD-1080p')
            allow_library_qualities(profile)
            client.call('/qualityprofile/' + str(profile['id']), profile, 'PUT')
            self.status[name] = {'root': root, 'profile': profile['id']}
        qb.call('/auth/logout', {}, 'POST', form=True)

    def quality(self):
        policy = self.state.get('hevc_policy', DEFAULT_HEVC_POLICY)
        score, minimum = HEVC_POLICIES[policy]
        self.status['hevc_policy'] = policy
        for name in ('radarr', 'sonarr'):
            client = self.arr(name)
            formats = client.call('/customformat')
            custom = next((x for x in formats if x['name'] == 'Hotel HEVC x265'), None)
            if not custom:
                custom = client.call('/customformat', {'name': 'Hotel HEVC x265', 'includeCustomFormatWhenRenaming': False,
                    'specifications': [{'name': 'HEVC x265', 'implementation': 'ReleaseTitleSpecification', 'negate': False, 'required': False,
                        'fields': [{'name': 'value', 'value': '(?i)(?:x[ ._-]?265|hevc|h[ ._-]?265)'}]}]}, 'POST')
            profile = next(x for x in client.call('/qualityprofile') if x['name'] == 'HD-1080p')
            for item in profile.get('formatItems', []):
                if item.get('format', item.get('formatId')) == custom['id']: item['score'] = score
            profile['minFormatScore'] = minimum
            # Any language: Chinese-dubbed or Chinese-tagged releases are not filtered out.
            if name == 'radarr': profile['language'] = {'id': -1, 'name': 'Any'}
            client.call('/qualityprofile/' + str(profile['id']), profile, 'PUT')
            saved = client.call('/qualityprofile/' + str(profile['id']))
            if not any(x.get('format', x.get('formatId')) == custom['id'] and x['score'] == score for x in saved['formatItems']) or saved.get('minFormatScore', 0) != minimum:
                raise RuntimeError('HEVC片源规则读回不符，未宣称已配置')
            self.status.setdefault('hevc', {})[name] = {'format': custom['id'], 'profile': profile['id']}
            for item in client.call('/qualitydefinition'):
                if item.get('quality', {}).get('resolution') == 1080:
                    item.update(maxSize=40, preferredSize=30, minSize=min(item.get('minSize') or 0, 30))
                    client.call('/qualitydefinition/' + str(item['id']), item, 'PUT')

    def notifications(self):
        key = json.loads(Path('/srv/hotel-tv/data/nas-integration.json').read_text())['token']
        for name in ('radarr', 'sonarr'):
            client = self.arr(name)
            model = next(x for x in client.call('/notification/schema') if any(s in (x.get('implementation', '') + x.get('name', '')) for s in ('MediaBrowser', 'Emby', 'Jellyfin')))
            model = field_values(model, {'host': 'nas-jellyfin', 'port': 8096, 'apiKey': key, 'useSsl': False, 'updateLibrary': True})
            model.update(name='Hotel Jellyfin', onDownload=True, onUpgrade=True, onImportComplete=True)
            ensure(client, '/notification', model)

    def indexers(self):
        prowlarr = Native('http://127.0.0.1:9696/api/v1', {'X-Api-Key': app_key('prowlarr')})
        prowlarr.wait('/system/status')
        for name in ('radarr', 'sonarr'):
            model = next(x for x in prowlarr.call('/applications/schema') if x['implementation'].lower() == name)
            model = field_values(model, {'prowlarrUrl': 'http://hotel-prowlarr:9696', 'baseUrl': 'http://nas-' + name + (':7878' if name == 'radarr' else ':8989'), 'apiKey': app_key(name)})
            model.update(name='Hotel ' + name, syncLevel='fullSync')
            ensure(prowlarr, '/applications', model)
        # Public, no-account sources only. Never register PT or paid accounts.
        for label in ('Knaben', 'Nyaa.si'):
            if any(x['name'] == label for x in prowlarr.call('/indexer')):
                self.status['external'][label] = 'configured'; continue
            try:
                model = next(x for x in prowlarr.call('/indexer/schema') if x.get('name', '').lower() == label.lower())
                model.update(enable=True, name=label, appProfileId=prowlarr.call('/appprofile')[0]['id'])
                model = field_values(model, {'baseUrl': model['indexerUrls'][0]})
                prowlarr.call('/indexer/test', model, 'POST'); prowlarr.call('/indexer', model, 'POST')
                self.status['external'][label] = 'configured'
            except (HTTPError, URLError, OSError, StopIteration): self.status['external'][label] = 'not verified; configure in Prowlarr'
        prowlarr.call('/command', {'name': 'ApplicationIndexerSync'}, 'POST')

    def libraries(self):
        jf = self.jellyfin()
        libraries = jf.call('/Library/VirtualFolders')
        for name, kind in [('电影（自动归档）', 'movies'), ('电视剧（自动归档）', 'tvshows')]:
            root = str(self.storage / 'media' / ('电影' if kind == 'movies' else '电视剧'))
            old = next((x for x in libraries if x['Name'] == name), None)
            if old:
                if old['Locations'] != [root]: raise RuntimeError('同名Jellyfin库路径不同，未覆盖')
                continue
            options = {'PathInfos': [{'Path': root}], 'PreferredMetadataLanguage': 'zh-CN', 'MetadataCountryCode': 'CN', 'SaveLocalMetadata': False, 'EnableRealtimeMonitor': True, 'EnableChapterImageExtraction': False, 'EnableTrickplayImageExtraction': False}
            jf.call('/Library/VirtualFolders?' + urlencode({'name': name, 'collectionType': kind}), {'LibraryOptions': options}, 'POST')

    def scan_libraries(self):
        # Creating virtual folders with refresh=false does not materialize their monitored roots.
        jf = self.jellyfin()
        before = next(x for x in jf.call('/ScheduledTasks') if x.get('Key') == 'RefreshLibrary')
        previous_end = before.get('LastExecutionResult', {}).get('EndTimeUtc')
        jf.call('/Library/Refresh', {}, 'POST')
        for _ in range(120):
            tasks = jf.call('/ScheduledTasks')
            task = next(x for x in tasks if x.get('Key') == 'RefreshLibrary')
            result = task.get('LastExecutionResult', {})
            if task['State'] == 'Idle' and result.get('Status') == 'Completed' and result.get('EndTimeUtc') != previous_end: return
            time.sleep(1)
        raise RuntimeError('Jellyfin首次扫描尚未完成，保留状态后重试')

    def subtitles(self):
        import yaml
        config = Path('/srv/nas/appdata/bazarr/config/config.yaml')
        for _ in range(180):
            if config.exists(): break
            time.sleep(1)
        key = yaml.safe_load(config.read_text())['auth']['apikey']
        client = Native('http://127.0.0.1:6767/api', {'X-API-KEY': key})
        client.wait('/system/status')
        profile = {'profileId': 1, 'name': '酒店简中优先', 'cutoff': 1, 'items': [{'id': i, 'language': lang, 'audio_exclude': 'False', 'audio_only_include': 'False', 'hi': 'False', 'forced': 'False'} for i, lang in enumerate(['zh', 'zt'], 1)], 'mustContain': [], 'mustNotContain': [], 'originalFormat': False}
        settings = {'settings-general-use_sonarr': 'true', 'settings-general-use_radarr': 'true', 'settings-general-enabled_providers': 'embeddedsubtitles', 'languages-enabled': ['zh', 'zt'], 'languages-profiles': json.dumps([profile]), 'settings-general-serie_default_enabled': 'true', 'settings-general-movie_default_enabled': 'true', 'settings-general-serie_default_profile': '1', 'settings-general-movie_default_profile': '1',
                    'settings-subsync-use_subsync': 'true', 'settings-subsync-force_audio': 'true', 'settings-subsync-use_original_language': 'true',
                    'settings-subsync-no_fix_framerate': 'false', 'settings-subsync-gss': 'true', 'settings-subsync-max_offset_seconds': '60',
                    'settings-subsync-use_subsync_threshold': 'false',
                    'settings-subsync-use_subsync_movie_threshold': 'false'}
        for name, port in [('radarr', 7878), ('sonarr', 8989)]:
            settings.update({f'settings-{name}-ip': 'nas-' + name, f'settings-{name}-port': str(port), f'settings-{name}-apikey': app_key(name)})
        client.call('/system/settings', settings, 'POST', form=True)
        self.status['external']['subtitles'] = 'embedded enabled; online providers require owner configuration; Whisper disabled'

    def seerr(self):
        client = Native('http://127.0.0.1:5055/api/v1')
        public = client.wait('/settings/public')
        login = {'username': 'admin', 'password': self.state.get('service_passwords',{}).get('qbittorrent',self.state['nas_password'])}
        # A partial initial binding may already exist when resuming.
        config = Path('/srv/nas/appdata/seerr/settings.json')
        configured = config.exists() and json.loads(config.read_text()).get('jellyfin', {}).get('ip')
        if not configured: login.update(hostname='nas-jellyfin', port=8096, useSsl=False, urlBase='', serverType=2, email='admin@hotel.invalid')
        user = client.call('/auth/jellyfin', login, 'POST')
        if user.get('id') != 1: raise RuntimeError('Seerr首装管理员核验失败')
        client.headers['X-Api-Key'] = json.loads(config.read_text())['main']['apiKey']
        client.call('/settings/main', {'applicationTitle': '酒店加片', 'locale': 'zh'}, 'POST')
        for name in ('radarr', 'sonarr'):
            values = {'name': name, 'hostname': 'nas-' + name, 'port': 7878 if name == 'radarr' else 8989, 'apiKey': app_key(name), 'useSsl': False, 'baseUrl': '', 'activeProfileId': self.status[name]['profile'], 'activeProfileName': 'HD-1080p', 'activeDirectory': self.status[name]['root'], 'is4k': False, 'isDefault': True, 'externalUrl': '', 'minimumAvailability': 'released', 'tags': [], 'syncEnabled': True, 'preventSearch': False, 'enableSeasonFolders': True}
            saved = client.call('/settings/' + name)
            if not saved: client.call('/settings/' + name + '/test', values, 'POST'); client.call('/settings/' + name, values, 'POST')
        libs = client.call('/settings/jellyfin/library/sync', {}, 'POST')
        for library in libs:
            client.call('/settings/jellyfin/library/' + library['id'], {'enabled': '自动归档' in library['name']}, 'PUT')
        if not public.get('initialized'): client.call('/settings/initialize', {}, 'POST')
        client.call('/user/1/settings/main', {'locale': 'zh'}, 'POST')
        client.call('/auth/logout', {}, 'POST')

    def hotel(self):
        sys.path.insert(0, '/opt/hotel-tv/current/server')
        from management import Store
        with service_identity():
            store = Store(Path('/srv/hotel-tv/data'), Path('/srv/hotel-tv/media'), Path('/etc/hotel-tv/devices.json'), [Path('/srv/hotel-tv/media'), self.storage, Path('/mnt'), Path('/media')])
            source = self.storage / 'media'
            with store.db() as db:
                exists = any(Path(json.loads(r[0])['path']) == source or Path(json.loads(r[0])['path']) in source.parents for r in db.execute('SELECT body FROM sources'))
                revision = store.kv(db, 'revision')
            if not exists: store.add_source({'name': 'Jellyfin自动归档', 'path': str(source)}, 'system:安装器', revision)
            with store.db() as db:
                store.put_kv(db, 'media_automation', {'enabled': True, 'external': self.status['external']})
                store.put_kv(db, 'compat_root', str(self.storage / COMPAT_DIR))
                store.put_kv(db, 'media_policy', {'hevc': self.status.get('hevc_policy', DEFAULT_HEVC_POLICY)})
        self.compat_dir()
        self.integration()
        self.storage_access()
        run(['systemctl', 'restart', 'hotel-tv'])

    def integration(self):
        """Private endpoints/keys so the hotel admin can show health and do the few owner actions itself."""
        import yaml
        seerr = json.loads(Path('/srv/nas/appdata/seerr/settings.json').read_text())['main']['apiKey']
        bazarr = yaml.safe_load(Path('/srv/nas/appdata/bazarr/config/config.yaml').read_text())['auth']['apikey']
        value = {'schema': 1,
                 'radarr': {'url': 'http://127.0.0.1:7878', 'api_key': app_key('radarr'), **self.status.get('hevc', {}).get('radarr', {})},
                 'sonarr': {'url': 'http://127.0.0.1:8989', 'api_key': app_key('sonarr'), **self.status.get('hevc', {}).get('sonarr', {})},
                 'prowlarr': {'url': 'http://127.0.0.1:9696', 'api_key': app_key('prowlarr')},
                 'bazarr': {'url': 'http://127.0.0.1:6767', 'api_key': bazarr},
                 'seerr': {'url': 'http://127.0.0.1:5055', 'api_key': seerr},
                 'qbittorrent': {'url': 'http://127.0.0.1:8098'},
                 'storage_root': str(self.storage), 'storage_guard':True, 'require_storage_marker':True, 'compat_root': str(self.storage / COMPAT_DIR)}
        with service_identity(): private_json(INTEGRATION, value)

    def verify(self):
        import yaml
        checks = {}
        for name in ('radarr', 'sonarr'):
            client = self.arr(name)
            if not any(x['path'] == self.status[name]['root'] for x in client.call('/rootfolder')):
                raise RuntimeError(name + ' 根目录验收失败')
            profile = client.call('/qualityprofile/' + str(self.status[name]['profile']))
            expected = HEVC_POLICIES[self.status.get('hevc_policy', DEFAULT_HEVC_POLICY)][0]
            if not any(x.get('name') == 'Hotel HEVC x265' and x['score'] == expected for x in profile['formatItems']):
                raise RuntimeError(name + ' 片源规则验收失败')
            if str(client.call('/system/status').get('authentication', '')).lower() != 'external':
                raise RuntimeError(name + ' 仍会要求单独登录，免二次登录验收失败')
            for endpoint, expected in [('/downloadclient', 'Hotel qB'), ('/notification', 'Hotel Jellyfin')]:
                if not any(x['name'] == expected for x in client.call(endpoint)):
                    raise RuntimeError(name + ' 原生连接缺失')
            checks[name] = True
        for name in ('radarr', 'sonarr', 'bazarr', 'seerr'):
            info = inspect('nas-' + name)
            if not info['State']['Running']: raise RuntimeError(name + ' 未运行')
            bindings = info['HostConfig']['PortBindings']
            if any(binding['HostIp'] != '127.0.0.1' for group in bindings.values() for binding in group):
                raise RuntimeError(name + ' 监听范围验收失败')
        jf = self.jellyfin(); libs = jf.call('/Library/VirtualFolders')
        if sum('自动归档' in x['Name'] for x in libs) != 2: raise RuntimeError('Jellyfin自动归档库验收失败')
        roots = {str(self.storage / 'media' / name) for name in ('电影', '电视剧')}
        physical = {x.get('Path') for x in jf.call('/Items?Recursive=true&IncludeItemTypes=Folder&Fields=Path')['Items']}
        if not roots.issubset(physical): raise RuntimeError('Jellyfin物理片库根尚未建立，不能宣称首片自动同步已就绪')
        baz = yaml.safe_load(Path('/srv/nas/appdata/bazarr/config/config.yaml').read_text())
        if not baz['general']['use_radarr'] or not baz['general']['use_sonarr']:
            raise RuntimeError('Bazarr连接开关验收失败')
        if baz['general']['enabled_providers'] != ['embeddedsubtitles']:
            raise RuntimeError('Bazarr内嵌字幕源验收失败')
        seerr = json.loads(Path('/srv/nas/appdata/seerr/settings.json').read_text())
        native = Native('http://127.0.0.1:5055/api/v1', {'X-Api-Key': seerr['main']['apiKey']})
        if not native.call('/settings/public')['initialized']: raise RuntimeError('Seerr初始化验收失败')
        if len([x for x in native.call('/settings/jellyfin/library') if x['enabled']]) != 2:
            raise RuntimeError('Seerr片库选择验收失败')
        try:
            result = native.call('/search?' + urlencode({'query': '\u529f\u592b', 'language': 'zh-CN'}))
            self.status['external']['TMDB'] = 'verified' if any(x.get('id') == 9470 for x in result['results']) else 'not verified'
        except (HTTPError, URLError, OSError, KeyError): self.status['external']['TMDB'] = 'not verified; local services remain installed'
        prowlarr = Native('http://127.0.0.1:9696/api/v1', {'X-Api-Key': app_key('prowlarr')})
        if str(prowlarr.call('/system/status').get('authentication', '')).lower() != 'external':
            raise RuntimeError('Prowlarr 仍会要求单独登录，免二次登录验收失败')
        integration = json.loads(INTEGRATION.read_text())
        if INTEGRATION.stat().st_uid != pwd.getpwnam('hotel-tv').pw_uid or INTEGRATION.stat().st_mode & 0o077:
            raise RuntimeError('片库自动化私有配置权限验收失败')
        if not all(integration.get(k, {}).get('api_key') for k in ('radarr', 'sonarr', 'prowlarr', 'bazarr', 'seerr')):
            raise RuntimeError('片库自动化私有配置不完整')
        compat = self.storage / COMPAT_DIR
        if compat.stat().st_uid != pwd.getpwnam('hotel-tv').pw_uid or compat.stat().st_dev != self.storage.stat().st_dev:
            raise RuntimeError('兼容副本目录未与片库同盘或属主不符')
        inside = Path('/srv/hotel-tv/media')
        if not (self.storage == inside or inside in self.storage.parents):
            if not SERVICE_DROPIN.is_file() or ('"%s"' % self.storage) not in SERVICE_DROPIN.read_text():
                raise RuntimeError('影院服务没有数据盘写入权限（兼容副本和加片无法写入），systemd 配置验收失败')
        checks.update(jellyfin=True, bazarr=True, seerr=True, loopback=True, single_login=True, compat_on_media_disk=True, service_can_write_storage=True,
                      storage=hardlink_probe(self.storage)['hardlink'])
        self.status['verification'] = checks
        private_json(self.track, self.status)
        print('片库本机验收通过；联网服务状态见 /etc/hotel-tv/media-install-report.json', flush=True)

    def execute(self):
        if 'storage' in self.status['completed']:
            marker = self.storage / '.hotel-media-install.json'
            if not marker.is_file() or json.loads(marker.read_text()).get('owner') != self.owner:
                raise RuntimeError('媒体目录未挂载或已被替换，恢复流程停止')
        for name, action in [('storage', self.storage_prepare), ('watch-roots', self.watch_roots), ('containers', self.containers), ('single-login', self.single_login), ('gateway', lambda: run([sys.executable, ROOT / 'deploy/nas/download-tools/install.py', '--listen', self.state['host'], '--refresh-proxy'])), ('downloaders', self.downloaders), ('quality', self.quality), ('libraries', self.libraries), ('library-scan', self.scan_libraries), ('notifications', self.notifications), ('indexers', self.indexers), ('subtitles', self.subtitles), ('seerr', self.seerr), ('hotel', self.hotel)]:
            self.phase(name, action)
        self.verify()
        private_json('/etc/hotel-tv/media-install-report.json', {'storage': str(self.storage), 'hardlink': self.status['storage'], 'services_configured': self.status['completed'], 'external': self.status['external'], 'verification': self.status['verification'], 'credentials': 'generated on this host only'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--state', type=Path, required=True); args = parser.parse_args()
    try:
        if os.geteuid() != 0: raise RuntimeError('仅供root首装编排调用')
        state = json.loads(args.state.read_text())
        if state['profile'] != 'full': raise RuntimeError('媒体组件仅属于full首装')
        Setup(state).execute()
    except Exception as exc:
        if isinstance(exc, RuntimeError): print(str(exc), file=sys.stderr)
        print('片库安装未完成：' + type(exc).__name__ + '；原因详情只在本机核对，不输出密钥。修复后用同一安装目录 --resume。', file=sys.stderr)
        sys.exit(1)
