"""Persistent single-hotel administration. Media files are never removed by this module."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
from urllib.parse import quote
from zoneinfo import ZoneInfo

from import_media import scan
from jellyfin_sync import JellyfinMixin
from media_automation import MediaAutomation
from media_center import MediaCenter
from media_add import MediaAdd


def now_ms():
    return int(time.time() * 1000)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def device_token_hash(value):
    if isinstance(value,str) and re.fullmatch(r'sha256:[a-f0-9]{64}',value): return value
    return 'sha256:'+hashlib.sha256(value.encode()).hexdigest()


class Problem(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def text(value, limit=200, required=False, label=None):
    if label:
        if not isinstance(value, str): raise Problem(label + '格式无效')
        if required and not value.strip(): raise Problem(label + '不能为空')
        if len(value) > limit: raise Problem(label + '最多' + str(limit) + '个字符（当前' + str(len(value)) + '）')
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise Problem('文字为空或超过允许长度')
    return value.strip()


def number(value, lower, upper):
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise Problem('数值超出允许范围')
    return value


def password_hash(password, salt=None):
    if not isinstance(password, str) or not 8 <= len(password) <= 200:
        raise Problem('密码需为 8–200 个字符')
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 260000).hex()
    return salt + ':' + digest


def password_matches(password, saved):
    try:
        return hmac.compare_digest(password_hash(password, saved.split(':')[0]), saved)
    except (Problem, ValueError):
        return False


from operations import OperationsMixin
from campaign_bundles import CampaignBundleOperations


class Store(CampaignBundleOperations, OperationsMixin, JellyfinMixin, MediaAutomation, MediaCenter, MediaAdd):
    def __init__(self, data, media, config, allowed_roots=None):
        self.data, self.media = Path(data), Path(media).resolve()
        self.path = self.data / 'events.sqlite3'
        self._configured_roots = [Path(p).resolve() for p in (allowed_roots or [self.media])]
        self.scan_lock = threading.Lock()
        self.workflow_lock = threading.Lock()
        self.sync_wakeup = threading.Event()
        self.file_lock = threading.RLock()
        self.init_compat_runtime()
        self.data.mkdir(parents=True, exist_ok=True)
        self.media.mkdir(parents=True, exist_ok=True)
        self.initialize(Path(config))

    @property
    def allowed_roots(self):
        from library_scan import mounted_roots
        return list(dict.fromkeys(self._configured_roots + mounted_roots()))

    def cleanup_files(self, db, item):
        from library_scan import source_mount_available
        row = db.execute('SELECT s.body FROM sources s JOIN media m ON m.source=s.id WHERE m.id=?', (item['id'],)).fetchone()
        if row and not source_mount_available(json.loads(row[0])):
            raise Problem('对应数据盘未挂载或已变化，不允许清理文件')
        return super().cleanup_files(db, item)

    @contextmanager
    def db(self):
        conn = sqlite3.connect(str(self.path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout=15000')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self, config):
        with self.db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, device TEXT NOT NULL, received INTEGER NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS users(name TEXT PRIMARY KEY,password TEXT NOT NULL,role TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,name TEXT NOT NULL,csrf TEXT NOT NULL,expires INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,body TEXT NOT NULL,token TEXT NOT NULL UNIQUE,last_seen INTEGER NOT NULL DEFAULT 0,ip TEXT NOT NULL DEFAULT '',last_event TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS categories(id TEXT PRIMARY KEY,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS media(id TEXT PRIMARY KEY,source TEXT NOT NULL,body TEXT NOT NULL,published INTEGER NOT NULL,available INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS campaigns(id TEXT PRIMARY KEY,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,at INTEGER NOT NULL,actor TEXT NOT NULL,action TEXT NOT NULL,object TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS releases(id INTEGER PRIMARY KEY AUTOINCREMENT,at INTEGER NOT NULL,actor TEXT NOT NULL,action TEXT NOT NULL,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS media_add(id TEXT PRIMARY KEY,body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_device_received ON events(device,received);
            ''')
            for row in db.execute('SELECT id,body,token FROM devices').fetchall():
                hashed=device_token_hash(row['token'])
                if hashed==row['token']:continue
                old_fp=hashlib.sha256(encoded([row['body'],row['token']]).encode()).hexdigest()
                new_fp=hashlib.sha256(encoded([row['body'],hashed]).encode()).hexdigest()
                db.execute('UPDATE devices SET token=? WHERE id=?',(hashed,row['id']))
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='software_jobs'").fetchone():
                    for jobrow in db.execute("SELECT id,body FROM software_jobs WHERE json_extract(body,'$.target')=?",(row['id'],)).fetchall():
                        job=json.loads(jobrow['body'])
                        if job.get('device_fingerprint')==old_fp:
                            job['device_fingerprint']=new_fp
                            db.execute('UPDATE software_jobs SET body=? WHERE id=?',(encoded(job),jobrow['id']))
            fresh = not db.execute("SELECT 1 FROM kv WHERE key='initialized'").fetchone()
            self.initialize_home_backgrounds(db, fresh)
            if fresh:
                settings = {'hotel': '酒店影院', 'timezone': 'Asia/Singapore', 'midroll_interval_seconds': 1200}
                catalog = self.read_seed('catalog.json', {'items': []})
                ads = self.read_seed('ads.json', {'ads': []})
                settings['hotel'] = catalog.get('hotel', settings['hotel'])
                settings['midroll_interval_seconds'] = ads.get('midroll_interval_seconds', 1200)
                if settings['midroll_interval_seconds'] == 12 and any(a.get('id') == 'welcome-midroll' for a in ads.get('ads', [])):
                    settings['midroll_interval_seconds'] = 1200  # Replace the known rapid acceptance-test interval.
                self.put_kv(db, 'settings', settings)
                self.put_kv(db, 'revision', 1)
                self.put_kv(db, 'initialized', True)
                db.execute('INSERT INTO sources VALUES(?,?)', ('main', encoded({'id': 'main', 'name': '默认本地片库', 'path': str(self.media), 'last_scan': 0})))
                for item in catalog.get('items', []):
                    db.execute('INSERT INTO media VALUES(?,?,?,?,?)', (item['id'], 'main', encoded(item), 1, 1))
                config_data = json.loads(config.read_text(encoding='utf-8'))
                for ident, d in config_data.get('devices', {}).items():
                    body = {'id': ident, 'room': str(d.get('room', ident)), 'group': str(d.get('group', '全部客房')), 'enabled': True, 'model': '', 'version': ''}
                    db.execute('INSERT INTO devices(id,body,token) VALUES(?,?,?)', (ident, encoded(body), device_token_hash(d['token'])))
                for ad in ads.get('ads', []):
                    campaign = self.from_manifest(ad)
                    db.execute('INSERT INTO campaigns VALUES(?,?)', (campaign['id'], encoded(campaign)))
                self.snapshot(db, 'system', '导入现有试点配置')
            self.register_categories(db)
            self.initialize_operations(db)
            if not db.execute('SELECT 1 FROM users LIMIT 1').fetchone():
                initial = secrets.token_urlsafe(18)
                path = self.data / 'admin-initial-password.txt'
                if path.exists():
                    previous = dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line)
                    initial = previous.get('password', '')
                    if len(initial) < 8: raise Problem('初始账号文件不完整，请管理员检查后再启动')
                else:
                    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'w') as output:
                        output.write('username=admin\npassword=' + initial + '\n')
                db.execute('INSERT INTO users VALUES(?,?,?,1)', ('admin', password_hash(initial), 'admin'))
            # Interrupted scans must not remain permanently displayed as running after a reboot.
            for row in db.execute('SELECT id,body FROM jobs').fetchall():
                job = json.loads(row['body'])
                if job.get('status') in ('queued', 'running'):
                    job.update(status='failed', error='上次扫描因服务重启中断，可重新扫描', finished=now_ms())
                    db.execute('UPDATE jobs SET body=? WHERE id=?', (encoded(job), row['id']))

    def read_seed(self, name, default):
        path = self.data / name
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default

    @staticmethod
    def put_kv(db, key, value):
        db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)', (key, encoded(value)))

    @staticmethod
    def kv(db, key):
        return json.loads(db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()[0])

    def audit(self, db, actor, action, obj):
        db.execute('INSERT INTO audit(at,actor,action,object) VALUES(?,?,?,?)', (now_ms(), actor, action, str(obj)[:500]))

    def check_revision(self, db, expected):
        if not db.in_transaction:
            db.execute('BEGIN IMMEDIATE')
        if expected != self.kv(db, 'revision'):
            raise Problem('数据已被其他操作更新，请刷新后重试', 409)

    def snapshot(self, db, actor, action):
        payload = {'media': [dict(r) for r in db.execute('SELECT * FROM media')],
                   'campaigns': [dict(r) for r in db.execute('SELECT * FROM campaigns')], 'settings': self.kv(db, 'settings')}
        db.execute('INSERT INTO releases(at,actor,action,body) VALUES(?,?,?,?)', (now_ms(), actor, action, encoded(payload)))
        db.execute('DELETE FROM releases WHERE id NOT IN (SELECT id FROM releases ORDER BY id DESC LIMIT 50)')

    def changed(self, db, actor, action, obj):
        self.__dict__.pop('_media_center_cache',None)
        self.__dict__.pop('_tv_route_cache',None)
        self.put_kv(db, 'revision', self.kv(db, 'revision') + 1)
        self.audit(db, actor, action, obj)
        self.snapshot(db, actor, action)

    def login(self, username, password):
        with self.db() as db:
            user = db.execute('SELECT * FROM users WHERE name=? AND enabled=1', (str(username),)).fetchone()
            if not user or not password_matches(password, user['password']):
                raise Problem('账号或密码错误', 401)
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
            db.execute('DELETE FROM sessions WHERE expires<?', (now_ms(),))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), user['name'], csrf, now_ms() + 12 * 3600 * 1000))
            self.audit(db, username, '登录后台', username)
            return token, {'name': user['name'], 'role': user['role'], 'csrf': csrf}

    def session(self, token):
        if not token:
            return None
        with self.db() as db:
            row = db.execute('SELECT u.name,u.role,s.csrf FROM sessions s JOIN users u ON s.name=u.name WHERE s.id=? AND s.expires>? AND u.enabled=1', (hashlib.sha256(token.encode()).hexdigest(), now_ms())).fetchone()
            return dict(row) if row else None

    def logout(self, token):
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE id=?', (hashlib.sha256(token.encode()).hexdigest(),))

    def device(self, token, ip=None, touch=False):
        if not token:
            return None
        with self.db() as db:
            hashed='sha256:'+hashlib.sha256(token.encode()).hexdigest()
            row = db.execute('SELECT id,body,token FROM devices WHERE token=? OR token=?', (hashed,token)).fetchone()
            if not row or not hmac.compare_digest(row['token'], hashed if row['token'].startswith('sha256:') else token):
                return None
            body = json.loads(row['body'])
            if not body.get('enabled', True):
                return None
            if touch:
                db.execute('UPDATE devices SET last_seen=?,ip=? WHERE id=?', (now_ms(), ip or '', row['id']))
            return body

    def catalog(self, device):
        def public(value):
            if isinstance(value,dict): return {k:public(v) for k,v in value.items() if not k.startswith('_') and k!='jellyfin_id'}
            if isinstance(value,list): return [public(v) for v in value]
            return value
        with self.db() as db:
            categories={c['name']:i for i,c in enumerate(self.category_list(db))}
            items=[json.loads(r['body']) for r in db.execute('SELECT body,source FROM media WHERE published=1 AND available=1') if self.source_enabled(db,r['source'])]
            ready_items=[]
            for item in items:
                if item.get('episodes'):
                    item['episodes']=[e for e in item['episodes'] if not e.get('_pending_publish') and self.automation_readiness(db, {**item, 'episodes':[e]})['ready']]
                    if item['episodes']: ready_items.append(item)
                elif self.automation_readiness(db,item)['ready']: ready_items.append(item)
            items=ready_items
            items=[m for m in items if not m.get('_archived')]
            routes=self.compat_routes(db)
            for m in items:  # the original stays available as a fallback source for playback recovery (I05)
                if m.get('path'): m['subtitles'] = self.subtitle_choices(m, m['path'])
                for e in m.get('episodes', []): e['subtitles'] = self.subtitle_choices(m, e['path'])
                if m.get('path') in routes: m['source_path']=m['path'];m['path']=routes[m['path']]
                for e in m.get('episodes',[]):
                    if e.get('path') in routes: e['source_path']=e['path'];e['path']=routes[e['path']]
            items.sort(key=lambda m:(categories.get(m.get('category'),len(categories)),not m.get('_pinned',False),
                                     -(m.get('_created_at',0) if type(m.get('_created_at',0)) in (int,float) else 0),m.get('title',''),m['id']))
            return {'hotel':self.kv(db,'settings')['hotel'],'timezone':self.kv(db,'settings')['timezone'],'items':public(items),'room':device.get('room',device['id']),'home':self.home_payload(db,{m['id'] for m in items})}

    def tv_route_allowed(self, device, route):
        """Only files referenced by this device's published manifests can be fetched."""
        from urllib.parse import unquote
        route = unquote(route)
        with self.db() as db: revision = self.kv(db, 'revision')
        key = (revision, device['id'], device.get('group'), now_ms() // 10000)
        cache = self.__dict__.setdefault('_tv_route_cache', {})
        allowed = cache.get(key)
        if allowed is None:
            allowed = set()
            media_fields={'path','source_path','poster','image','background','contact_qr','wifi_qr','logo','qr'}
            def collect(value, field=None):
                if isinstance(value, dict):
                    for name,child in value.items(): collect(child,name)
                elif isinstance(value, list):
                    for child in value: collect(child,field)
                elif field in media_fields and isinstance(value, str) and value.startswith(('/media/', '/api/v1/subtitles/')):
                    allowed.add(unquote(value))
            collect(self.catalog(device))
            collect(self.ad_manifest(device, protocol=2))
            # Bundled backgrounds are published system assets, including the other
            # choices in the home editor. Custom uploads still require a manifest.
            from home_backgrounds import available_backgrounds
            collect(available_backgrounds(self.media))
            try: collect(self.tv_channels())
            except (OSError, ValueError, KeyError, TypeError):
                # A broken channel configuration must not disconnect movie requests.
                pass
            if len(cache) >= 512: cache.clear()
            cache[key] = allowed
        return route in allowed

    @staticmethod
    def from_manifest(ad):
        names = {'welcome-preroll': '技术验证 · 片前欢迎视频', 'welcome-midroll': '技术验证 · 中插欢迎图片',
                 'hotel-ticker': '酒店服务 · 全天文字', 'timed-ticker': '酒店服务 · 夜间文字'}
        return {'id': ad['id'], 'name': ad.get('name', names.get(ad['id'], ad['id'])), 'merchant': ad.get('merchant', '酒店自营'),
                'tier': ad['tier'], 'format': {'video': '视频', 'image': '图片', 'text': '文字'}.get(ad.get('kind'), '文字'),
                'path': ad.get('path', ''), 'text': ad.get('text', ''), 'placements': [ad.get('placement', 'all')],
                'group': '全部客房', 'status': '投放中', 'priority': ad.get('priority', 0),
                'duration': ad.get('duration_seconds', 5), 'gap_seconds': ad.get('min_gap_seconds', 60), 'cap': ad.get('cap_per_session', 2),
                'start': ad.get('start_time', '00:00'), 'end': ad.get('end_time', '00:00'), 'timezone': ad.get('timezone', 'Asia/Singapore'),
                'days': ad.get('weekdays', [1, 2, 3, 4, 5, 6, 7]), 'from': '', 'until': '',
                'valid_from_ms': ad.get('valid_from_ms', 0), 'valid_until_ms': ad.get('valid_until_ms', 253402300799000)}

    @staticmethod
    def creatives(campaign):
        c = campaign
        result = []
        zone = ZoneInfo(c.get('timezone', 'Asia/Singapore'))
        begin = int(datetime.fromisoformat(c['from']).replace(tzinfo=zone).timestamp() * 1000) if c.get('from') else c.get('valid_from_ms', 0)
        end = int((datetime.fromisoformat(c['until']).replace(tzinfo=zone) + timedelta(days=1)).timestamp() * 1000) if c.get('until') else c.get('valid_until_ms', 253402300799000)
        for placement in c['placements']:
            ident = c['id'] if len(c['placements']) == 1 else c['id'] + '~' + placement
            result.append({'id': ident, 'campaign_id': c['id'], 'tier': c['tier'], 'kind': {'图片': 'image', '视频': 'video', '文字': 'text', '图文': 'card'}[c['format']],
                           'placement': placement, 'text': c.get('text', ''), 'path': c.get('path', ''), 'duration_seconds': c['duration'],
                           'merchant': c['merchant'], 'rotation_order': c.get('rotation_order', 100), 'rotation_enabled': c.get('rotation_enabled', True),
                           'card': c.get('creative_card') if c['format']=='图文' else None,
                           'priority': c['priority'], 'min_gap_seconds': c['gap_seconds'], 'cap_per_session': c['cap'],
                           'valid_from_ms': begin, 'valid_until_ms': end, 'timezone': c['timezone'], 'weekdays': c['days'],
                           'start_time': '' if c['tier'] == 2 else c['start'], 'end_time': '' if c['tier'] == 2 else c['end']})
        return result

    def ad_manifest(self, device, record=False, protocol=1):
        with self.db() as db:
            grouped = []
            merchants = {a['name']: a for a in self.objects(db, 'advertiser')}
            for row in db.execute('SELECT body FROM campaigns'):
                c = json.loads(row['body'])
                merchant = merchants.get(c['merchant'], {})
                if c['status'] == '投放中' and not merchant.get('_archived') and merchant.get('enabled', True) and c['group'] in ('全部客房', device.get('group')):
                    grouped.append((c.get('merchant', ''), self.creatives(c)))
            ads, control = self.control_manifest(db, device, grouped, record)
            ads, playback = self.playback_manifest(db, ads, protocol)
            result = {'midroll_interval_seconds': self.kv(db, 'settings')['midroll_interval_seconds'], 'ads': ads, 'control': control}
            if playback is not None: result['playback'] = playback
            return result

    def media_path(self, route):
        if not route.startswith('/media/') or '\x00' in route or '\\' in route:
            raise Problem('素材路径无效', 404)
        rel = route[len('/media/'):]
        if any(part.startswith('.') for part in Path(rel).parts):
            raise Problem('隐藏目录不能作为媒体读取', 404)
        root = self.media
        if rel.startswith('_compat/'):
            # Virtual route: compatible copies may live on the media data disk (installer-set compat root).
            root, rel = self.compat_root().resolve(), rel[len('_compat/'):]
            if root != (self.media / '_compat').resolve() and not any(root == a or a in root.parents for a in self.allowed_roots):
                raise Problem('兼容副本目录不在允许范围', 404)
        elif rel.startswith('@sources/'):
            parts = rel.split('/', 2)
            if len(parts) != 3:
                raise Problem('素材路径无效', 404)
            with self.db() as db:
                row = db.execute('SELECT body FROM sources WHERE id=?', (parts[1],)).fetchone()
            if not row:
                raise Problem('片库来源不存在', 404)
            source=json.loads(row['body'])
            if not source.get('enabled',True) or source.get('_archived'):raise Problem('片库来源已停用',404)
            from library_scan import source_mount_available
            if not source_mount_available(source):raise Problem('对应数据盘未挂载，或挂载的盘已变化',404)
            root, rel = Path(source['path']).resolve(), parts[2]
            if not any(root == allowed or allowed in root.parents for allowed in self.allowed_roots):
                raise Problem('片库目录已移出允许范围', 404)
        path = (root / rel).resolve()
        if root not in path.parents or not path.is_file() or not os.access(str(path), os.R_OK):
            raise Problem('文件不存在或服务器无读取权限', 404)
        if path.suffix.lower() not in {'.mp4', '.mkv', '.m4v', '.ts', '.mov', '.avi', '.webm', '.jpg', '.jpeg', '.png', '.webp', '.vtt', '.srt'}:
            raise Problem('此文件类型不能作为媒体读取', 404)
        return path

    def require_media_path(self, value, suffixes=None):
        from urllib.parse import unquote
        value = text(value, 2000, True)
        path = self.media_path(unquote(value))
        if suffixes and path.suffix.lower() not in suffixes:
            raise Problem('绑定文件与所选素材类型不符')
        return value

    def state(self):
        with self.db() as db:
            devices = []
            for row in db.execute('SELECT id,body,last_seen,ip,last_event FROM devices'):
                d = json.loads(row['body'])
                d.update(last_seen=row['last_seen'], ip=row['ip'], last_event=row['last_event'], online=bool(row['last_seen'] and now_ms() - row['last_seen'] < 120000))
                devices.append(d)
            media = []
            for row in db.execute('SELECT * FROM media'):
                m = json.loads(row['body'])
                m.update(source_id=row['source'], published=bool(row['published']), available=bool(row['available']))
                readiness=self.automation_readiness(db,m)
                m.update(compat_ready=readiness['ready'],compat_label=readiness['label'])
                media.append(m)
            sources = [json.loads(r['body']) for r in db.execute('SELECT body FROM sources')]
            for source in sources:
                members=[m for m in media if m['source_id']==source['id']]
                source.update(media_count=len(members),published_count=sum(bool(m['published']) for m in members),enabled=source.get('enabled',True))
                p = Path(source['path'])
                from library_scan import source_mount_available
                source['readable'] = p.is_dir() and os.access(str(p), os.R_OK | os.X_OK) and source_mount_available(source)
                if source['readable']:
                    disk = shutil.disk_usage(p)
                    source.update(total_bytes=disk.total, free_bytes=disk.free)
            categories = self.category_list(db)
            for category in categories:
                members = [m for m in media if str(m.get('category', '')).strip() == category['name']]
                category.update(media_count=len(members), published_count=sum(bool(m['published']) for m in members))
            return {'media_automation':self.automation_enabled(db),'endpoints':{'tv_port':getattr(self,'tv_port',18790)},'revision': self.kv(db, 'revision'), 'settings': self.kv(db, 'settings'), 'media': media, 'categories': categories, 'jellyfin': self.jellyfin_status(),
                    'campaigns': [json.loads(r['body']) for r in db.execute('SELECT body FROM campaigns')], 'devices': devices, 'sources': sources,
                    'jobs': [json.loads(r['body']) for r in db.execute('SELECT body FROM jobs ORDER BY rowid DESC LIMIT 10')],
                    'audit': [dict(r) for r in db.execute('SELECT at,actor,action,object FROM audit ORDER BY id DESC LIMIT 30')],
                    'releases': [dict(r) for r in db.execute('SELECT id,at,actor,action FROM releases ORDER BY id DESC LIMIT 20')],
                    'users': self.user_rows(db),
                    **self.managed_state(db),
                    'storage': {'default': str(self.media), 'movies': str(self.media / '电影'), 'series': str(self.media / '电视剧'),
                                'allowed_roots': [str(p) for p in self.allowed_roots], 'ffprobe': bool(shutil.which('ffprobe'))}}

    @staticmethod
    def category_list(db):
        # Stable insertion order resolves equal sort values without depending on random IDs.
        return sorted((json.loads(r['body']) for r in db.execute('SELECT body FROM categories ORDER BY rowid')), key=lambda c: c['sort'])

    def register_categories(self, db):
        """Register legacy/restored names without changing media or the business revision."""
        known = {c['name'] for c in self.category_list(db)}
        for row in db.execute('SELECT body FROM media ORDER BY rowid').fetchall():
            name = str(json.loads(row['body']).get('category', '')).strip()
            if name and name not in known:
                self.register_category(db, name)
                known.add(name)

    def register_category(self, db, name):
        categories = self.category_list(db)
        # A historical restore may reintroduce a canonical name that is now an alias.
        # Canonical media names take precedence; do not silently rewrite restored media.
        for c in categories:
            if name in c.get('aliases', []):
                c['aliases'].remove(name)
                db.execute('UPDATE categories SET body=? WHERE id=?', (encoded(c), c['id']))
        category = {'id': 'cat-' + secrets.token_hex(8), 'name': name, 'kind': 'all',
                    'sort': max((c['sort'] for c in categories), default=-10) + 10, 'enabled': True, 'aliases': []}
        db.execute('INSERT INTO categories VALUES(?,?)', (category['id'], encoded(category)))
        return name

    def mapped_category(self, db, name):
        name = str(name).strip()[:80]
        if not name: return ''
        categories = self.category_list(db)
        canonical = next((c for c in categories if c['name'] == name), None)
        if canonical: return canonical['name']
        alias = next((c for c in categories if name in c.get('aliases', [])), None)
        return alias['name'] if alias else self.register_category(db, name)

    def validate_media_category(self, db, name, current, kind):
        name = text(name, 80)
        if name == str(current or '').strip(): return name
        category = next((c for c in self.category_list(db) if c['name'] == name), None)
        if not category or not category['enabled']:
            raise Problem('请选择已登记且启用的分类')
        if category['kind'] not in ('all', kind):
            raise Problem('此分类不适用于当前影片类型')
        return name

    def save_category(self, ident, body, actor, revision):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', ident): raise Problem('分类标识无效')
        name = text(body.get('name'), 80, True)
        kind = body.get('kind', 'all')
        sort = body.get('sort', 0)
        if kind not in ('all', 'movie', 'series'): raise Problem('分类适用类型无效')
        if type(sort) not in (int, float) or not math.isfinite(sort) or abs(sort) > 1000000: raise Problem('分类排序需为有效数值')
        if type(body.get('enabled', True)) is not bool: raise Problem('分类启用状态无效')
        aliases = body.get('aliases', [])
        if not isinstance(aliases, list) or len(aliases) > 100: raise Problem('分类别名需为列表，最多100个')
        aliases = list(dict.fromkeys(text(a, 80, True) for a in aliases))
        with self.db() as db:
            self.check_revision(db, revision)
            categories = self.category_list(db)
            old = next((c for c in categories if c['id'] == ident), None)
            if old and old['name'] != name: aliases.append(old['name'])
            aliases = list(dict.fromkeys(a for a in aliases if a != name))
            for c in categories:
                if c['id'] != ident and ({name, *aliases} & {c['name'], *c.get('aliases', [])}):
                    raise Problem('分类名称或别名与「' + c['name'] + '」重复', 409)
            category = {'id': ident, 'name': name, 'kind': kind, 'sort': sort, 'enabled': body.get('enabled', True), 'aliases': aliases}
            db.execute('INSERT OR REPLACE INTO categories VALUES(?,?)', (ident, encoded(category)))
            if old and old['name'] != name: self.rename_media_category(db, {old['name']}, name)
            self.changed(db, actor, '保存分类', name)
            return category

    def rename_media_category(self, db, old_names, name):
        for row in db.execute('SELECT id,body FROM media').fetchall():
            item = json.loads(row['body'])
            altered = False
            if item.get('category') in old_names: item['category'] = name; altered = True
            meta = item.get('_jellyfin_meta', {})
            if meta.get('category') in old_names: meta['category'] = name; altered = True
            if altered:
                self.stamp_media(item, json.loads(row['body']))
                db.execute('UPDATE media SET body=? WHERE id=?', (encoded(item), row['id']))

    def merge_category(self, ident, body, actor, revision):
        into = text(body.get('into'), 100, True)
        if ident == into: raise Problem('不能把分类合并到自身')
        with self.db() as db:
            self.check_revision(db, revision)
            categories = {c['id']: c for c in self.category_list(db)}
            if ident not in categories or into not in categories: raise Problem('分类不存在', 404)
            old, target = categories[ident], categories[into]
            target['aliases'] = list(dict.fromkeys(a for a in target.get('aliases', []) + [old['name']] + old.get('aliases', []) if a != target['name']))
            self.rename_media_category(db, {old['name'], *old.get('aliases', [])}, target['name'])
            db.execute('UPDATE categories SET body=? WHERE id=?', (encoded(target), into))
            db.execute('DELETE FROM categories WHERE id=?', (ident,))
            self.changed(db, actor, '合并分类', old['name'] + ' → ' + target['name'])
            return target

    def delete_category(self, ident, actor, revision):
        with self.db() as db:
            self.check_revision(db, revision)
            row = db.execute('SELECT body FROM categories WHERE id=?', (ident,)).fetchone()
            if not row: raise Problem('分类不存在', 404)
            category = json.loads(row['body'])
            used = sum(str(json.loads(r['body']).get('category', '')).strip() == category['name'] for r in db.execute('SELECT body FROM media'))
            if used: raise Problem('分类仍有 ' + str(used) + ' 部影片使用，请先迁移或合并', 409)
            db.execute('DELETE FROM categories WHERE id=?', (ident,))
            self.changed(db, actor, '删除空分类', category['name'])

    def add_source(self, body, actor, revision):
        path = Path(text(body.get('path'), 1500, True))
        if not path.is_absolute():
            raise Problem('请填写 Ubuntu 上的绝对目录路径')
        path = path.resolve()
        if not any(path == root or root in path.parents for root in self.allowed_roots):
            raise Problem('目录不在管理员配置的片库允许范围内')
        if not path.is_dir() or not os.access(str(path), os.R_OK | os.X_OK):
            raise Problem('目录不存在，或 hotel-tv 账户无法读取；请先挂载硬盘并检查目录权限')
        with self.db() as db:
            self.check_revision(db, revision)
            for row in db.execute('SELECT body FROM sources'):
                existing_source = json.loads(row['body'])
                existing = Path(existing_source['path']).resolve()
                if existing in path.parents and existing_source.get('_auto_mount'):
                    continue
                if path == existing or path in existing.parents or existing in path.parents:
                    raise Problem('此目录或其父子目录已经接入，避免重复入库')
            source = {'id': 'lib-' + secrets.token_hex(6), 'name': text(body.get('name'), 100, True), 'path': str(path), 'last_scan': 0}
            from library_scan import mounted_volumes
            volume = next((v for v in mounted_volumes()['volumes'] if v.get('uuid') and (Path(v['path']) == path or Path(v['path']) in path.parents)), None)
            if volume:source['_mount_uuid'] = volume['uuid']
            db.execute('INSERT INTO sources VALUES(?,?)', (source['id'], encoded(source)))
            self.changed(db, actor, '接入片库目录', source['name'])
            return source

    def start_scan(self, source_id, actor):
        if not self.scan_lock.acquire(blocking=False):
            raise Problem('已有目录正在扫描，请等待完成', 409)
        ident = 'scan-' + secrets.token_hex(6)
        try:
            with self.db() as db:
                row = db.execute('SELECT body FROM sources WHERE id=?', (source_id,)).fetchone()
                if not row:
                    raise Problem('片库来源不存在', 404)
                source = json.loads(row['body'])
                if not source.get('enabled',True) or source.get('_archived'):raise Problem('来源已停用，不能扫描',409)
                job = {'id': ident, 'source_id': source_id, 'status': 'queued', 'started': now_ms(), 'added': 0, 'updated': 0, 'missing': 0}
                db.execute('INSERT INTO jobs VALUES(?,?)', (ident, encoded(job)))
            threading.Thread(target=self._scan, args=(source, job, actor), daemon=True).start()
            return job
        except Exception:
            self.scan_lock.release()
            raise

    def job(self, job):
        with self.db() as db:
            db.execute('UPDATE jobs SET body=? WHERE id=?', (encoded(job), job['id']))

    @staticmethod
    def stamp_media(item, previous=None, state_changed=False):
        """Timestamp actual index changes; never backfill creation time on legacy entries."""
        changed = previous is None or item != previous or state_changed
        if changed:
            stamp = now_ms()
            if previous is None: item['_created_at'] = stamp
            item['_updated_at'] = stamp
        return changed

    def _scan(self, source, job, actor):
        try:
            job['status'] = 'running'; self.job(job)
            root = Path(source['path']).resolve()
            from library_scan import source_mount_available
            if not source_mount_available(source):raise Problem('对应数据盘未挂载，或挂载的盘已变化；原索引保持不变')
            if not any(root == allowed or allowed in root.parents for allowed in self.allowed_roots):
                raise Problem('片库目录已移出允许范围，原索引保持不变')
            if not root.is_dir() or not os.access(str(root), os.R_OK | os.X_OK):
                raise Problem('片库未挂载或不可读；原片库索引保持不变')
            excluded = []
            if source.get('_auto_mount'):
                with self.db() as db:
                    others = [json.loads(r['body']) for r in db.execute('SELECT body FROM sources WHERE id!=?', (source['id'],))]
                    excluded = [Path(s['path']).resolve() for s in others if not s.get('_mount_uuid') or s['_mount_uuid'] == source.get('_mount_uuid')]
                excluded = [p for p in excluded if root in p.parents]
            result = scan(root, excluded_roots=excluded)
            # Never withdraw the whole library just because a removable disk is temporarily absent.
            with self.db() as db:
                existing = [dict(r) for r in db.execute('SELECT * FROM media WHERE source=?', (source['id'],))]
            if not result['items'] and existing:
                raise Problem('本次未找到视频，未覆盖原索引。请检查硬盘挂载及目录位置')
            prefix = '/media/' if source['id'] == 'main' else '/media/@sources/' + source['id'] + '/'
            def paths(item):
                return {item['path']} if item.get('path') else {e['path'] for e in item.get('episodes', [])}
            by_path = {}
            for row in existing:
                for path in paths(json.loads(row['body'])):
                    by_path[path] = row
            seen, prepared = set(), []
            for item in result['items']:
                item['id'] = source['id'] + '-' + item['id']
                for key in ('path', 'poster'):
                    if item.get(key): item[key] = prefix + item[key][len('/media/'):]
                for episode in item.get('episodes', []):
                    episode['id'] = source['id'] + '-' + episode['id']
                    episode['path'] = prefix + episode['path'][len('/media/'):]
                old = next((by_path[p] for p in paths(item) if p in by_path), None)
                if old:
                    old_body = json.loads(old['body']); seen.add(old['id'])
                    if old_body.get('kind') == 'series' and item.get('kind') != 'series':
                        # Preserve the legacy fixture's explicit series grouping on a flat disk layout.
                        prepared.append((old['id'], old_body, old['published'])); continue
                    item['id'] = old['id']
                    for key in ('title', 'category', 'year', 'description', 'poster', 'accent', 'jellyfin_id', '_jellyfin_meta', '_jellyfin_subtitles', '_archived', '_archived_at', '_pinned', '_created_at', '_updated_at', '_episode_memory', '_provider_ids', '_renaming'):
                        if key in old_body: item[key] = old_body[key]
                seen.add(item['id'])
                prepared.append((item['id'], item, old['published'] if old else 0))
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                changed = False
                if not self.source_enabled(db,source['id']):raise Problem('扫描来源已停用，未覆盖索引')
                for ident, item, published in prepared:
                    if self.media_blocked(db,item):
                        job['skipped']=job.get('skipped',0)+1; continue
                    existed = db.execute('SELECT body,published,available FROM media WHERE id=?', (ident,)).fetchone()
                    if not existed:
                        job['added'] += 1
                    else:
                        job['updated'] += 1
                        latest = json.loads(existed['body']); published = existed['published']
                        for key in ('title', 'category', 'year', 'description', 'poster', 'accent', 'jellyfin_id', '_jellyfin_meta', '_jellyfin_subtitles', '_archived', '_archived_at', '_pinned', '_created_at', '_updated_at', '_episode_memory', '_provider_ids', '_renaming'):
                            if key in latest: item[key] = latest[key]
                        previous_episodes = {**latest.get('_jellyfin_meta',{}).get('episodes',{}),**latest.get('_episode_memory',{}),**{e['path']:e for e in latest.get('episodes',[])}}
                        for episode in item.get('episodes', []):
                            previous = previous_episodes.get(episode['path'])
                            if previous:
                                for key in ('id','title','season','number','_pending_publish'):
                                    if key in previous:episode[key]=previous[key]
                        if item.get('episodes'): item['episodes'].sort(key=lambda e: (e['season'], e['number'], e['id']))
                    item['category'] = self.mapped_category(db, item.get('category', ''))
                    if item.get('_archived'): published = 0
                    self.remember_episodes(item,json.loads(existed['body']) if existed else None)
                    item_changed = self.stamp_media(item, json.loads(existed['body']) if existed else None,
                                                    bool(existed and (not existed['available'] or existed['published'] != published)))
                    changed = changed or item_changed
                    db.execute('INSERT OR REPLACE INTO media VALUES(?,?,?,?,1)', (ident, source['id'], encoded(item), published))
                for row in existing:
                    if row['id'] not in seen:
                        body = json.loads(row['body'])
                        try:
                            for p in paths(body): self.require_media_path(p)
                        except Problem:
                            latest_row = db.execute('SELECT body,available FROM media WHERE id=?', (row['id'],)).fetchone()
                            if latest_row and latest_row['available']:
                                latest_body = json.loads(latest_row['body'])
                                self.stamp_media(latest_body, latest_body, state_changed=True)
                                db.execute('UPDATE media SET body=?,available=0 WHERE id=?', (encoded(latest_body), row['id']))
                                job['missing'] += 1; changed = True
                current_source=json.loads(db.execute('SELECT body FROM sources WHERE id=?',(source['id'],)).fetchone()['body'])
                current_source['last_scan']=now_ms()
                db.execute('UPDATE sources SET body=? WHERE id=?',(encoded(current_source),source['id']))
                if changed: self.changed(db, actor, '扫描影片目录', source['name'])
                job.update(status='completed', finished=now_ms())
                db.execute('UPDATE jobs SET body=? WHERE id=?', (encoded(job), job['id']))
        except Exception as exc:
            job.update(status='failed', error=str(exc)[:400], finished=now_ms()); self.job(job)
        finally:
            self.scan_lock.release()

    def save_media(self, ident, body, actor, revision):
        with self.db() as db:
            self.check_revision(db, revision)
            row = db.execute('SELECT * FROM media WHERE id=?', (ident,)).fetchone()
            if not row: raise Problem('影片不存在', 404)
            item = json.loads(row['body'])
            category = self.validate_media_category(db, body.get('category', item.get('category', '')), item.get('category', ''), item.get('kind', 'movie'))
            for key, maximum in [('title', 200), ('category', 80), ('year', 10), ('description', 5000)]:
                item[key] = text(str(body.get(key, item.get(key, ''))), maximum, key == 'title')
            item['category'] = category
            if 'poster' in body:
                item['poster'] = self.require_media_path(body['poster'], {'.jpg', '.jpeg', '.png', '.webp'}) if body['poster'] else ''
            if item.get('kind') == 'series' and 'episodes' in body:
                episodes = body['episodes']
                if not isinstance(episodes, list) or not 1 <= len(episodes) <= 5000: raise Problem('分集列表无效')
                old = {e['id']: e for e in item.get('episodes', [])}
                if set(e.get('id') for e in episodes) != set(old) or len(episodes) != len(old): raise Problem('分集应先由目录扫描识别，再编辑顺序和名称')
                for e in episodes:
                    old[e['id']].update(title=text(e.get('title'), 200, True), season=number(e.get('season'), 1, 999), number=number(e.get('number'), 1, 9999))
                item['episodes'] = sorted(old.values(), key=lambda e: (e['season'], e['number'], e['id']))
            if body.get('approve_new_episodes') is True:
                for episode in item.get('episodes',[]): episode.pop('_pending_publish',None)
            published = bool(body.get('published', row['published']))
            if published and item.get('_archived'): raise Problem('影片已归档，请先恢复后再上架')
            warnings=[]
            if published:
                gate=self.automation_publish_errors(db,item)
                if gate:raise Problem('；'.join(gate),409)
                for entry in ([{'title':item['title'],'path':item['path']}] if item.get('path') else item.get('episodes',[])):
                    try:self.require_media_path(entry['path'])
                    except Problem:
                        warnings.append(entry.get('title',entry['path'])+'：'+entry['path'])
                if warnings and not row['published']:raise Problem('不可读的影片/分集：'+'；'.join(warnings[:10]),404)
            self.remember_episodes(item,json.loads(row['body']))
            self.stamp_media(item, json.loads(row['body']), published != bool(row['published']))
            db.execute('UPDATE media SET body=?,published=? WHERE id=?', (encoded(item), int(published), ident))
            self.changed(db, actor, '保存影片' if published else '保存未上架影片', item['title'])
            return {'ok':True,'warnings':warnings[:10]}

    def publication_errors(self, item, available):
        errors=[]
        if item.get('_archived'): errors.append('影片已归档，请先恢复')
        if not available: errors.append('源文件已标记缺失，请先扫描或同步确认可用')
        if not str(item.get('category', '')).strip(): errors.append('分类为空，请先设置分类')
        files=[(item.get('title', '影片'),item['path'])] if item.get('path') else [(e.get('title', e.get('id', '分集')),e.get('path', '')) for e in item.get('episodes', [])]
        if not files: errors.append('没有绑定视频文件')
        for title, path in files:
            if len(errors)>=10: break
            try:
                self.require_media_path(path)
            except Problem: errors.append(str(title)+'：文件不可读（'+str(path)+'）')
        return errors[:10]

    def batch_media(self, body, actor, revision):
        ids=body.get('ids');action=body.get('action')
        labels={'publish':'上架','unpublish':'下架','set_category':'修改分类','archive':'归档','restore':'恢复','pin':'置顶','unpin':'取消置顶'}
        if not isinstance(ids,list) or not 1<=len(ids)<=500 or any(not isinstance(i,str) or not i or len(i)>200 for i in ids): raise Problem('每次请选择 1–500 部影片')
        if len(set(ids))!=len(ids): raise Problem('影片列表含重复项')
        if action not in labels: raise Problem('批量操作无效')
        result={'ok':[],'failed':[]}
        with self.db() as db:
            self.check_revision(db,revision)
            for ident in ids:
                row=db.execute('SELECT * FROM media WHERE id=?',(ident,)).fetchone()
                item=json.loads(row['body']) if row else {}
                try:
                    if not row: raise Problem('影片不存在',404)
                    published=row['published']
                    if action=='publish':
                        errors=self.publication_errors(item,row['available'])+self.automation_publish_errors(db,item)
                        if errors: raise Problem('；'.join(errors))
                        published=1
                    elif action=='unpublish': published=0
                    elif action=='set_category': item['category']=self.validate_media_category(db,body.get('value'),item.get('category',''),item.get('kind','movie'))
                    elif action=='archive':
                        if not item.get('_archived'): item['_archived_at']=now_ms()
                        item['_archived']=True;published=0
                    elif action=='restore': item['_archived']=False;published=0
                    elif action in ('pin','unpin'): item['_pinned']=action=='pin'
                    self.stamp_media(item, json.loads(row['body']), published != row['published'])
                    db.execute('UPDATE media SET body=?,published=? WHERE id=?',(encoded(item),published,ident))
                    result['ok'].append(ident)
                except Problem as exc: result['failed'].append({'id':ident,'title':item.get('title',ident),'reason':str(exc)})
            if result['ok']: self.changed(db,actor,'批量'+labels[action]+' '+str(len(result['ok']))+' 部（失败 '+str(len(result['failed']))+'）',','.join(result['ok']))
        return result

    def _campaign_values(self, ident, body):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', ident): raise Problem('广告标识无效')
        c = {'id': ident, 'name': text(body.get('name'), 200, True, '广告名称'), 'merchant': text(body.get('merchant', '酒店自营'), 200, True, '广告主'),
             'tier': number(body.get('tier'), 1, 3), 'priority': number(body.get('priority', 0), 0, 100),
             'duration': number(body.get('duration', 5), 1, 120), 'gap_seconds': number(body.get('gap_seconds', 60), 0, 86400),
             'cap': number(body.get('cap', 2), 1, 100), 'format': body.get('format'), 'status': body.get('status'),
             'group': text(body.get('group', '全部客房'), 100, True, '客房组'), 'text': text(body.get('text', ''), 2000, label='滚动文字'),
             'path': text(body.get('path', ''), 2000, label='广告素材路径'), 'placements': body.get('placements'),
             'start': body.get('start', '00:00'), 'end': body.get('end', '00:00'), 'days': body.get('days'),
             'timezone': text(body.get('timezone', 'Asia/Singapore'), 100, True, '投放时区'), 'from': body.get('from', ''), 'until': body.get('until', '')}
        if c['status'] not in ('草稿', '投放中', '已暂停', '已归档'): raise Problem('广告状态无效')
        if c['tier'] == 1:
            if c['format'] not in ('视频', '图片', '图文'): raise Problem('一档广告需要图片、视频或图文卡片')
            if c['format']=='图文': c['creative_card'] = self.sponsor_card(body.get('creative_card', {}),c['merchant'],published=c['status']=='投放中')
            elif c['status'] == '投放中':
                suffix = {'.mp4', '.m4v', '.webm', '.mov', '.mkv'} if c['format'] == '视频' else {'.jpg', '.jpeg', '.png', '.webp'}
                self.require_media_path(c['path'], suffix)
            elif c['path'] and not c['path'].startswith('/media/'):
                raise Problem('广告素材必须来自本地媒体目录')
            allowed = {'home', 'preroll', 'midroll', 'pause', 'postroll', 'channel'}
        else:
            if c['format'] != '文字': raise Problem('二、三档广告需要文字形式')
            if not c['text']: raise Problem('滚动文字不能为空')
            allowed = {'all', 'home', 'playback'}
        if not isinstance(c['placements'], list) or not c['placements'] or len(c['placements']) != len(set(c['placements'])) or not set(c['placements']) <= allowed:
            raise Problem('请选择有效的展示位置')
        if not isinstance(c['days'], list) or not c['days'] or any(type(d) is not int or d not in range(1, 8) for d in c['days']): raise Problem('请选择有效的投放星期')
        try:
            ZoneInfo(c['timezone'])
            for clock in (c['start'], c['end']): datetime.strptime(clock, '%H:%M')
            for date in (c['from'], c['until']):
                if date: datetime.strptime(date, '%Y-%m-%d')
            if c['from'] and c['until'] and c['from'] > c['until']: raise ValueError()
        except (ValueError, KeyError, TypeError): raise Problem('日期、时间或时区无效')
        self.creatives(c)
        return c

    def _upsert_campaign(self, db, ident, body, actor, notify=True):
        c = self._campaign_values(ident, body)
        previous=db.execute('SELECT body FROM campaigns WHERE id=?',(ident,)).fetchone()
        old=json.loads(previous['body']) if previous else {}
        c['rotation_order'] = number(body.get('rotation_order', old.get('rotation_order', 100)), 0, 100000)
        c['rotation_enabled'] = body.get('rotation_enabled', old.get('rotation_enabled', True))
        if type(c['rotation_enabled']) is not bool: raise Problem('轮播开关无效')
        if c['format']=='图文': c['creative_card'] = self.prepare_sponsor_card(c['creative_card'])
        if self.archived('campaign',old) and c['status']!='已归档': raise Problem('广告已在回收站，请先恢复')
        for key in ('_created_at','_archived','_archived_at'):
            if key in old:c[key]=old[key]
        if not previous:c['_created_at']=now_ms()
        c['_updated_at']=now_ms()
        if c['status']=='已归档':c.update(_archived=True,_archived_at=old.get('_archived_at',now_ms()))
        self.ensure_named(db,'advertiser',c['merchant'])
        if c['status']=='投放中':self.campaign_validate_publication(db,c)
        elif c['group']!='全部客房' and c['group'] not in {json.loads(r['body']).get('group') for r in db.execute('SELECT body FROM devices')}|{g['name'] for g in self.objects(db,'group') if not g.get('_archived')}:
            raise Problem('所选客房组不存在')
        db.execute('INSERT OR REPLACE INTO campaigns VALUES(?,?)', (ident, encoded(c)))
        if notify: self.changed(db, actor, '保存广告：' + c['status'], c['name'])
        return c

    def save_campaign(self, ident, body, actor, revision):
        with self.db() as db:
            self.check_revision(db, revision)
            row = db.execute('SELECT body FROM campaigns WHERE id=?', (ident,)).fetchone()
            if row and json.loads(row['body']).get('_bundle_id'):
                raise Problem('组合广告请在组合编辑页统一保存，避免只修改一个档位')
            self._upsert_campaign(db, ident, body, actor)

    def save_device(self, ident, body, actor, revision):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', ident): raise Problem('设备标识只允许字母、数字、横线和下划线')
        d = {'id': ident, 'room': text(body.get('room'), 80, True), 'group': text(body.get('group', '全部客房'), 100, True),
             'enabled': body.get('enabled', True) is True, 'model': text(body.get('model', ''), 150), 'version': text(body.get('version', ''), 80)}
        with self.db() as db:
            self.check_revision(db, revision)
            exists = db.execute('SELECT body FROM devices WHERE id=?', (ident,)).fetchone()
            old=json.loads(exists['body']) if exists else {}
            if old.get('_archived'):raise Problem('设备在回收站，请先恢复')
            if d['group']!='全部客房':
                group=self.ensure_named(db,'group',d['group'])
                if (group.get('_archived') or not group.get('enabled',True)) and old.get('group')!=d['group']:raise Problem('请选择已启用的设备组')
            for key in ('_created_at','_archived'):
                if key in old:d[key]=old[key]
            d['_updated_at']=now_ms()
            if exists:
                db.execute('UPDATE devices SET body=? WHERE id=?', (encoded(d), ident)); token = None
            else:
                d['_created_at']=now_ms()
                token = secrets.token_urlsafe(32)
                db.execute('INSERT INTO devices(id,body,token) VALUES(?,?,?)', (ident, encoded(d), device_token_hash(token)))
            self.changed(db, actor, '保存客房设备', d['room'])
            return {'id': ident, 'token': token}

    def settings(self, body, actor, revision):
        settings = {'hotel': text(body.get('hotel'), 200, True), 'timezone': text(body.get('timezone'), 100, True),
                    'midroll_interval_seconds': number(body.get('midroll_interval_seconds'), 0, 86400)}
        try: ZoneInfo(settings['timezone'])
        except (KeyError, ValueError): raise Problem('时区无效')
        with self.db() as db:
            self.check_revision(db, revision); self.put_kv(db, 'settings', settings); self.changed(db, actor, '保存系统设置', settings['hotel'])

    def save_user(self, body, actor, revision):
        name = text(body.get('name'), 80, True)
        if not re.fullmatch(r'[A-Za-z0-9_.-]{2,80}', name): raise Problem('账号需为 2–80 个字母、数字或 ._-')
        role, enabled = body.get('role'), body.get('enabled', True) is True
        if role not in ('admin', 'operator', 'viewer'): raise Problem('角色无效')
        if name == actor and (role != 'admin' or not enabled): raise Problem('不能停用或降级当前管理员')
        with self.db() as db:
            self.check_revision(db, revision)
            old = db.execute('SELECT * FROM users WHERE name=?', (name,)).fetchone()
            if any(u['id']==name and u.get('_archived') for u in self.objects(db,'user')):raise Problem('账号在回收站，请先恢复')
            password = password_hash(body['password']) if body.get('password') else old['password'] if old else password_hash('')
            db.execute('INSERT OR REPLACE INTO users VALUES(?,?,?,?)', (name, password, role, int(enabled)))
            if old and (body.get('password') or not enabled or old['role'] != role): db.execute('DELETE FROM sessions WHERE name=?', (name,))
            self.changed(db, actor, '保存后台账号', name)

    def restore_release(self, ident, actor, revision):
        with self.db() as db:
            self.check_revision(db, revision)
            row = db.execute('SELECT body FROM releases WHERE id=?', (ident,)).fetchone()
            if not row: raise Problem('历史配置不存在', 404)
            state = json.loads(row['body'])
            sources = {r['id'] for r in db.execute('SELECT id FROM sources')}
            for m in state['media']:
                if m['source'] not in sources: raise Problem('历史片库来源已不存在，不能回退')
            db.execute('DELETE FROM media'); db.execute('DELETE FROM campaigns')
            for m in state['media']: db.execute('INSERT INTO media VALUES(?,?,?,?,?)', tuple(m[k] for k in ('id', 'source', 'body', 'published', 'available')))
            for m in state['media']:db.execute("DELETE FROM managed_objects WHERE kind='media_tombstone' AND id=?",(m['id'],))
            for c in state['campaigns']: db.execute('INSERT INTO campaigns VALUES(?,?)', (c['id'], c['body']))
            self.put_kv(db, 'settings', state['settings'])
            self.register_categories(db)
            self.changed(db, actor, '回退历史片库与广告配置', ident)

    def record_events(self, device, events):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            received = now_ms()
            recent = db.execute('SELECT count(*) FROM events WHERE device=? AND received>?', (device['id'], received-60000)).fetchone()[0]
            allowance = max(0, 300-recent)
            identifiers={device['id']+':'+event['id'] for event in events}
            existing={row[0] for row in db.execute('SELECT id FROM events WHERE device=? AND id IN ('+','.join('?' for _ in identifiers)+')',(device['id'],*identifiers))} if identifiers else set()
            if len(identifiers-existing)>allowance:
                raise Problem('事件回传过快，请稍后重试；未确认的事件应保留在本机',429)
            campaigns=[json.loads(r[0]) for r in db.execute('SELECT body FROM campaigns')]+self.objects(db,'campaign_history')
            permitted={creative['id'] for campaign in campaigns if campaign.get('group') in ('全部客房',device.get('group')) for creative in self.creatives(campaign)}
            permitted.update(campaign['id']+'~'+placement for campaign in campaigns if campaign.get('group') in ('全部客房',device.get('group')) for placement in campaign.get('placements',[]))
            for original in events:
                event = {k: v for k, v in original.items() if not k.startswith('_') and k != 'exclude_from_ad_reports'}
                if event.get('type') in ('ad_requested','ad_started','ad_completed','ad_failed','ad_cancelled','ad_interrupted','text_started','text_ended') and event.get('creative_id') not in permitted:
                    continue
                stamp = event.get('at_ms')
                if type(stamp) is int and stamp < received-7*86400000: continue
                if type(stamp) is not int or not received-7*86400000 <= stamp <= received+300000:
                    event['at_ms'] = received
                # Attach trusted campaign attribution at receipt time; old records remain usable.
                creative = str(event.get('creative_id', ''))
                campaign = creative.split('~', 1)[0]
                self.attach_attribution(db,device,event)
                db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?)', (device['id'] + ':' + event['id'], device['id'], now_ms(), encoded(event)))
                allowance -= 1
            meaningful = next((e for e in reversed(events) if e.get('type') != 'deployment_probe'), None)
            if meaningful: db.execute('UPDATE devices SET last_event=? WHERE id=?', (str(meaningful['type'])[:100], device['id']))

    def reports(self, days=7, filters=None):
        return self.build_reports(days,filters)

    def backup(self, actor):
        destination = self.data / 'backups'
        destination.mkdir(mode=0o700, exist_ok=True)
        name = 'hotel-' + str(now_ms()) + '-' + secrets.token_hex(3) + '.sqlite3'
        path = destination / name
        with self.db() as source, sqlite3.connect(str(path)) as target:
            source.backup(target)
            target.execute('VACUUM')
        path.chmod(0o600)
        with self.db() as db: self.audit(db, actor, '生成数据库备份', name)
        return path
