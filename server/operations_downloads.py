"""Smart download (DL00 D01–D12): one Chinese sentence -> search -> pick -> real engine -> verify/organise -> scan,
Jellyfin metadata, compatibility copy -> (auto mode) publish -> TV catalogue.

Ownership rules: this module only manages engine jobs it created (category/tag/GID/task id), only publishes media it
imported, never deletes or overwrites existing media or seeding files, and keeps a persistent per-task state machine
so restarts, lost replies and stale callbacks cannot create duplicates or resurrect cancelled work."""
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from difflib import SequenceMatcher
from urllib.parse import quote, unquote, urlsplit

import downloads_engines as engines
import downloads_sources as sources
from downloads_net import NetError, check_url, magnet_hash, normalize_url, request, torrent_info
from downloads_parse import needs_clarification, normalize_title, parse_request
from downloads_storage import DownloadLocations


def core():
    import management
    return management


ACTIVE = ('needs_config', 'needs_clarify', 'searching', 'awaiting_choice', 'queued', 'downloading', 'paused', 'verifying',
          'organizing', 'importing', 'scraping', 'converting', 'publishing')
TERMINAL = ('ready', 'downloaded', 'failed', 'cancelled', 'no_results')
LABELS = {'needs_config': '待配置', 'needs_clarify': '待澄清', 'searching': '搜索中', 'no_results': '无结果', 'awaiting_choice': '待选择',
          'queued': '排队', 'downloading': '下载中', 'paused': '暂停', 'verifying': '校验', 'organizing': '整理', 'importing': '入库',
          'scraping': '刮削', 'converting': '兼容处理', 'publishing': '上架', 'ready': '已入库', 'downloaded': '已下载', 'failed': '失败', 'cancelled': '已取消'}
POST = ('verifying', 'organizing', 'importing', 'scraping', 'converting', 'publishing')
VIDEO = {'.mp4', '.mkv', '.m4v', '.ts', '.mov', '.avi', '.webm'}
PARTIAL = ('.!qb', '.part', '.aria2', '.tmp', '.crdownload', '.xltd')
DEFAULTS = {
    'auto_mode': False, 'auto_publish': True, 'preferred_resolution': '1080p', 'max_size_gb': 0, 'audio': [], 'subtitle': [],
    'concurrent_downloads': 2, 'speed_limit_kbps': 0, 'min_free_gb': 50, 'max_candidates_retry': 2, 'stall_minutes': 120,
    'staging_dir': '', 'save_dir': '', 'movie_dir': '电影', 'movie_category': '最新影片', 'series_dir': '电视剧', 'min_video_seconds': 60,
    'compat_auto': True, 'compat_wait_minutes': 180, 'scrape_wait_minutes': 20, 'sources': [],
    'engines': {'qbittorrent': {'enabled': False, 'url': '', 'username': '', 'directory': '/downloads', 'host_directory': ''},
                'aria2': {'enabled': False, 'url': '', 'directory': '/downloads', 'host_directory': ''}},
}
SECRET_FIELDS = {'qbittorrent': ('password',), 'aria2': ('secret',)}  # open-source engines only; Xunlei removed by owner decision


class DownloadOperations(DownloadLocations):
    _dl_lock = threading.RLock()
    _dl_worker = None
    _dl_post = None

    # ---------- settings and secrets (D01) ----------
    def download_settings(self, db):
        row = db.execute("SELECT value FROM kv WHERE key='download_settings'").fetchone()
        stored = {k: v for k, v in (json.loads(row[0]) if row else {}).items() if k in DEFAULTS}  # drops retired keys (e.g. Xunlei routing)
        settings = {**DEFAULTS, **stored}
        settings['engines'] = {k: {**v, **stored.get('engines', {}).get(k, {})} for k, v in DEFAULTS['engines'].items()}
        if not settings['staging_dir']: settings['staging_dir'] = str(self.media / '.downloads')
        return settings

    @property
    def _secret_path(self): return self.data / 'download-secrets.json'

    def download_secrets(self):
        try: return json.loads(self._secret_path.read_text())
        except (OSError, ValueError): return {}

    def _write_secrets(self, secrets_):
        temp = self._secret_path.with_suffix('.tmp')
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f: json.dump(secrets_, f)
        os.chmod(temp, 0o600); os.replace(temp, self._secret_path)

    def secret_for(self, kind, ident=None):
        s = self.download_secrets()
        if kind == 'source': return (s.get('sources') or {}).get(ident, {}).get('api_key', '')
        if kind == 'qbittorrent': return (s.get('qbittorrent') or {}).get('password', '')
        if kind == 'aria2': return (s.get('aria2') or {}).get('secret', '')
        return ''

    def download_settings_view(self):
        with self.db() as db:
            settings = self.download_settings(db)
            health = self.kv(db, 'download_health') if db.execute("SELECT 1 FROM kv WHERE key='download_health'").fetchone() else {}
        s = self.download_secrets()
        present = {'sources': {src['id']: bool((s.get('sources') or {}).get(src['id'], {}).get('api_key')) for src in settings['sources']},
                   **{k: {f: bool((s.get(k) or {}).get(f)) for f in fields} for k, fields in SECRET_FIELDS.items()}}
        return {'settings': settings, 'secrets_present': present, 'health': health, 'labels': LABELS}

    def save_download_settings(self, body, user, revision):
        m = core()
        new = body.get('settings') or {}
        with self.db() as db:
            self.check_revision(db, revision)
            current = self.download_settings(db)
        item = dict(current)
        for key in ('auto_mode', 'auto_publish', 'compat_auto'):
            if key in new: item[key] = new[key] is True
        if 'preferred_resolution' in new:
            if new['preferred_resolution'] not in ('2160p', '1080p', '720p', 'any'): raise m.Problem('首选画质无效')
            item['preferred_resolution'] = new['preferred_resolution']
        limits = {'max_size_gb': (0, 1000), 'concurrent_downloads': (1, 10), 'speed_limit_kbps': (0, 10000000), 'min_free_gb': (0, 100000),
                  'max_candidates_retry': (0, 10), 'stall_minutes': (5, 10080), 'min_video_seconds': (0, 3600), 'compat_wait_minutes': (5, 1440), 'scrape_wait_minutes': (1, 240)}
        for key, (lo, hi) in limits.items():
            if key in new: item[key] = m.number(new[key], lo, hi)
        for key in ('audio', 'subtitle'):
            if key in new:
                if not isinstance(new[key], list) or any(not isinstance(v, str) for v in new[key]): raise m.Problem('偏好格式无效')
                item[key] = [m.text(v, 20) for v in new[key]][:6]
        for key in ('movie_dir', 'series_dir', 'movie_category'):
            if key in new:
                value = m.text(new[key], 60, True)
                if '/' in value or '\\' in value or value.startswith('.') or value in ('..',): raise m.Problem('目录名称无效：' + value)
                item[key] = value
        if 'staging_dir' in new:
            item['staging_dir'] = self._check_staging(str(new['staging_dir'] or self.media / '.downloads'))
        if 'save_dir' in new:
            _, chosen = self._location(new['save_dir'] or item['staging_dir'], writable=True)
            item['save_dir'] = str(chosen)
        if 'sources' in new:
            if not isinstance(new['sources'], list) or len(new['sources']) > 20: raise m.Problem('搜索源列表无效（最多 20 个）')
            out, ids = [], set()
            for src in new['sources']:
                ident = m.text(src.get('id'), 40, True)
                if not re.fullmatch(r'[A-Za-z0-9_-]+', ident) or ident in ids: raise m.Problem('搜索源标识无效或重复')
                if src.get('type') not in sources.SOURCE_TYPES: raise m.Problem('搜索源类型不支持')
                url = self._check_service_url(src.get('url', ''), '搜索源地址')
                ids.add(ident)
                out.append({'id': ident, 'type': src['type'], 'name': m.text(src.get('name') or ident, 60), 'url': url, 'enabled': src.get('enabled', True) is True,
                            'timeout': m.number(src.get('timeout', 20), 3, 120)})
            item['sources'] = out
        if 'engines' in new:
            for kind, conf in (new['engines'] or {}).items():
                if kind not in DEFAULTS['engines'] or not isinstance(conf, dict): raise m.Problem('下载器类型无效')
                merged = dict(item['engines'][kind])
                if 'enabled' in conf: merged['enabled'] = conf['enabled'] is True
                if 'url' in conf: merged['url'] = self._check_service_url(conf['url'], '下载器地址') if conf['url'] else ''
                if 'username' in conf: merged['username'] = m.text(conf['username'], 80)
                for key in ('directory',):
                    if key in conf:
                        value = str(conf[key] or '').strip()
                        if value and (not value.startswith('/') or '..' in value.split('/')): raise m.Problem('路径必须是绝对路径：' + value)
                        merged[key] = value[:300]
                if 'host_directory' in conf:
                    merged['host_directory'] = self._check_staging(conf['host_directory']) if conf['host_directory'] else ''
                item['engines'][kind] = merged
        secrets_ = self.download_secrets(); touched = []
        for kind, fields in SECRET_FIELDS.items():
            for field in fields:
                value = ((body.get('secrets') or {}).get(kind) or {}).get(field)
                if value is None: continue
                if not isinstance(value, str) or len(value) > 500: raise m.Problem('密钥格式无效')
                bucket = secrets_.setdefault(kind, {})
                if value == '': bucket.pop(field, None)
                else: bucket[field] = value
                touched.append(kind + '.' + field)
        for ident, value in ((body.get('secrets') or {}).get('sources') or {}).items():
            if not isinstance(value, str) or len(value) > 500: raise m.Problem('密钥格式无效')
            bucket = secrets_.setdefault('sources', {}).setdefault(ident, {})
            if value == '': bucket.pop('api_key', None)
            else: bucket['api_key'] = value
            touched.append('source.' + ident)
        secrets_ = {k: v for k, v in secrets_.items() if k in SECRET_FIELDS or k == 'sources'}  # e.g. drops legacy Xunlei credentials
        valid_sources = {s['id'] for s in item['sources']}
        secrets_['sources'] = {k: v for k, v in (secrets_.get('sources') or {}).items() if k in valid_sources}
        with self.db() as db:
            self.check_revision(db, revision)
            self.put_kv(db, 'download_settings', item)
            old_sources = {s['id']: s for s in current['sources']}
            health = self.kv(db, 'download_health') if db.execute("SELECT 1 FROM kv WHERE key='download_health'").fetchone() else {}
            for target in list(health):
                if not target.startswith('source:'): continue
                ident = target[7:]
                old = old_sources.get(ident, {})
                new_source = next((s for s in item['sources'] if s['id'] == ident), {})
                if (old.get('type'), old.get('url')) != (new_source.get('type'), new_source.get('url')) or 'source.' + ident in touched:
                    health.pop(target, None)
            self.put_kv(db, 'download_health', health)
            self._write_secrets(secrets_)
            for task in self.dl_tasks(db):  # new settings apply now, not after the current back-off
                if task['state'] in ACTIVE and task.get('next_at'): task['next_at'] = 0; task['attempts'] = 0; self.dl_save(db, task)
            self.changed(db, user['name'], '保存智能下载设置', '更新密钥：' + ('、'.join(touched) if touched else '无'))
        return self.download_settings_view()

    # ---------- paths and services ----------
    def _check_service_url(self, url, label):
        m = core(); url = str(url or '').strip()
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password: raise m.Problem(label + '必须是 http(s) 地址，且不能内含账号密码')
        if parts.hostname.startswith('169.254.') or parts.hostname in ('metadata.google.internal',): raise m.Problem(label + '不能指向云主机元数据地址')
        return url[:300]

    def _check_staging(self, value):
        """Staging must be invisible to every library scanner: outside the media root, or inside it under a hidden folder."""
        m = core(); path = Path(str(value)).expanduser()
        if not path.is_absolute() or '..' in path.parts: raise m.Problem('暂存目录必须是绝对路径')
        resolved = path.resolve()
        if resolved == self.media or (self.media in resolved.parents and not any(p.startswith('.') for p in resolved.relative_to(self.media).parts)):
            raise m.Problem('暂存目录在片库内时必须位于隐藏目录（以 . 开头），否则未完成的文件会被扫描入库')
        if path.is_symlink(): raise m.Problem('暂存目录不能是符号链接')
        resolved.mkdir(parents=True, exist_ok=True)
        return str(resolved)

    def engine_for(self, settings, kind):
        conf = settings['engines'][kind]
        return engines.build(kind, conf, self.secret_for(kind))

    def host_path(self, settings, kind, engine_path):
        """Map a path as the engine sees it to this host, and refuse anything outside the engine's staging area."""
        m = core(); conf = settings['engines'][kind]
        engine_dir = (conf.get('directory') or '').rstrip('/'); host_dir = conf.get('host_directory') or settings['staging_dir']
        path = str(engine_path)
        if engine_dir and (path == engine_dir or path.startswith(engine_dir + '/')): path = host_dir + path[len(engine_dir):]
        resolved = Path(path).resolve()
        root = Path(host_dir).resolve()
        if resolved != root and root not in resolved.parents: raise m.Problem('下载器返回的文件不在暂存目录内，已拒绝：' + Path(path).name)
        return resolved

    def engine_dir(self, settings, kind, task_id):
        conf = settings['engines'][kind]
        base = (conf.get('directory') or conf.get('host_directory') or settings['staging_dir']).rstrip('/')
        return base + '/' + settings.get('_folder_name', task_id)

    def task_download_settings(self, task, settings):
        if not task.get('storage'): return settings
        frozen = task['storage']
        out = {**settings, 'staging_dir': frozen['path'], 'save_dir': frozen['path'], '_folder_name': task['folder_name']}
        out['engines'] = {k: {**v, 'host_directory': frozen['path'], 'directory': frozen['engine_paths'].get(k, '')} for k, v in settings['engines'].items()}
        return out

    def test_download_service(self, target, user):
        m = core()
        with self.db() as db: settings = self.download_settings(db)
        if target.startswith('source:'):
            conf = next((s for s in settings['sources'] if s['id'] == target[7:]), None)
            if not conf: raise m.Problem('搜索源不存在', 404)
            runner = lambda: sources.build(conf, self.secret_for('source', conf['id'])).test()
        elif target in DEFAULTS['engines']:
            runner = lambda: self.engine_for(settings, target).test()
        else: raise m.Problem('测试对象无效')
        try: result = runner()
        except NetError as exc:
            result = {'status': {'auth': 'auth_failed', 'unconfigured': 'unconfigured', 'unavailable': 'unavailable'}.get(exc.kind, 'unreachable'), 'message': str(exc)}
        except Exception as exc:  # a broken service must never break the admin page
            result = {'status': 'unreachable', 'message': '测试失败：' + type(exc).__name__}
        result = {k: v for k, v in result.items() if k in ('status', 'message', 'version', 'capabilities')}
        result['at'] = m.now_ms()
        with self.db() as db:
            health = self.kv(db, 'download_health') if db.execute("SELECT 1 FROM kv WHERE key='download_health'").fetchone() else {}
            health[target] = result
            self.put_kv(db, 'download_health', health)
            self.audit(db, user['name'], '测试智能下载连接', '%s：%s' % (target, result['status']))
        return result

    def configured_sources(self, settings):
        return [(s, self.secret_for('source', s['id'])) for s in settings['sources'] if s.get('enabled') and s.get('url')]

    def engine_ready(self, settings, kind):
        conf = settings['engines'][kind]
        return bool(conf.get('enabled') and conf.get('url'))

    def route_engine(self, settings, source):
        """Pick the engine that really supports this protocol; never hand BT to an engine that cannot seed/handle it."""
        if source['kind'] in ('magnet', 'torrent'):
            if self.engine_ready(settings, 'qbittorrent'): return 'qbittorrent'
            return None
        return 'aria2' if self.engine_ready(settings, 'aria2') else None

    # ---------- task records ----------
    def dl_task(self, db, ident):
        row = db.execute("SELECT body FROM managed_objects WHERE kind='download_task' AND id=?", (ident,)).fetchone()
        return json.loads(row['body']) if row else None

    def dl_tasks(self, db):
        return sorted(self.objects(db, 'download_task'), key=lambda t: t.get('created_at', 0), reverse=True)

    def dl_save(self, db, task, generation=None):
        """Write a task unless a newer control action (pause/cancel/retry) bumped its generation meanwhile."""
        if generation is not None:
            current = self.dl_task(db, task['id'])
            if current and current.get('generation', 0) != generation: return False
        task['updated_at'] = core().now_ms()
        self.put_object(db, 'download_task', task)
        return True

    def dl_state(self, task, state, note=''):
        if task.get('state') != state or note:
            task.setdefault('history', []).append({'state': state, 'at': core().now_ms(), 'note': note[:300]})
            task['history'] = task['history'][-60:]
        task['state'] = state; task['note'] = note[:300]

    def dl_notice(self, db, task, level, text):
        m = core()
        ident = 'n-%d-%s' % (m.now_ms(), task['id'][-6:])
        self.put_object(db, 'download_notice', {'id': ident, 'task_id': task['id'], 'level': level, 'text': text[:300], 'at': m.now_ms(), 'read': False})
        rows = sorted(self.objects(db, 'download_notice'), key=lambda n: n['at'])
        for old in rows[:-300]: db.execute("DELETE FROM managed_objects WHERE kind='download_notice' AND id=?", (old['id'],))

    def create_download(self, body, user, revision):
        if self.automation_enabled(): raise core().Problem('新片请从Seerr加片；旧下载流水线已冻结',409)
        m = core()
        text = m.text(body.get('request', ''), 500)
        manual = body.get('manual') or {}
        selected = body.get('candidate_from') or {}
        if not text and not manual and not selected: raise m.Problem('请输入搜索词，粘贴链接或选择种子文件')
        flow = m.number(body.get('flow_version', 2), 1, 2)
        parsed = parse_request(text) if text else {'original': '', 'title': '', 'kind': '', 'year': None, 'resolution': '', 'season': None,
                                                   'episodes': None, 'audio': [], 'subtitle': [], 'max_size_gb': None, 'fields': {}, 'questions': [], 'warnings': []}
        if flow == 2 and text and not re.search(r'[《「“\"\'『]', text):
            sentence = re.match(r'^(?:请|麻烦|帮我|帮忙|替我|给我|我要|我想|想要|想看|下载|搜索|搜|找|电影|电视剧|剧集)', text)
            filename = bool(re.search(r'\.[A-Za-z0-9]{1,8}$', text))
            if filename or (not sentence and not any(parsed[k] for k in ('year', 'resolution', 'season', 'episodes', 'max_size_gb'))):
                # A keyword/file name is data: do not strip Chinese words such as “的/要/资源”.
                parsed.update(title=text[:120], kind='', year=None, resolution='', season=None, episodes=None, audio=[], subtitle=[], max_size_gb=None,
                              fields={'title':'搜索关键词'}, questions=[], warnings=[])
        with self.db() as db:
            self.check_revision(db, revision)
            settings = self.download_settings(db)
            pick, parent = None, None
            if selected:
                parent = self.dl_task(db, m.text(selected.get('id'), 100, True))
                if not parent or parent.get('intent') != 'search' or parent.get('_archived'): raise m.Problem('搜索结果已失效，请重新搜索')
                pick = next((c for c in parent['candidates'] if c['key'] == selected.get('key')), None)
                if not pick: raise m.Problem('候选不存在，请重新搜索')
                if pick.get('protocol') not in ('torrent', 'http'): raise m.Problem('该下载协议暂不支持')
                parsed = json.loads(json.dumps(parent['parsed']))
                text = parent['request']
            self._apply_overrides(parsed, body.get('overrides') or {})
            if flow == 1:
                if not parsed['resolution'] and settings['preferred_resolution'] != 'any':
                    parsed['resolution'] = settings['preferred_resolution']; parsed['fields']['resolution'] = '默认首选画质'
                for key in ('audio', 'subtitle'):
                    if not parsed[key] and settings[key]: parsed[key] = list(settings[key])
                if not parsed['max_size_gb'] and settings['max_size_gb']: parsed['max_size_gb'] = settings['max_size_gb']
            else:
                if not parsed['title'] and text: parsed['title'] = text[:120]
                parsed['questions'] = []
            norm = (normalize_title(parsed['title']), parsed['kind'], parsed['year'], parsed['season'], tuple(parsed['episodes'] or ()), parsed['resolution'])
            if parsed['title'] and not body.get('force') and (flow == 1 or pick):
                dup = next((t for t in self.dl_tasks(db) if not t.get('_archived') and t['state'] in ACTIVE and t.get('intent') != 'search' and tuple(t.get('norm', [])) == tuple(map(lambda x: list(x) if isinstance(x, tuple) else x, norm))), None)
                if dup and flow == 1: raise m.Problem('已有相同需求的任务在进行：' + dup['id'], 409)
            ident = 'dl-%d-%s' % (m.now_ms(), hashlib.sha1(os.urandom(8)).hexdigest()[:6])
            task = {'id': ident, 'request': text, 'parsed': parsed, 'norm': [norm[0], norm[1], norm[2], norm[3], list(norm[4]), norm[5]], 'state': '', 'note': '',
                    'auto': body.get('auto', settings['auto_mode'] if flow == 1 else False) is True, 'created_by': user['name'], 'created_at': m.now_ms(), 'generation': 0,
                    'flow_version': flow, 'intent': 'download' if manual or pick or flow == 1 else 'search',
                    'organize': body.get('organize', flow == 1) is True, 'publish_after': body.get('publish', False) is True,
                    'candidates': [], 'sources_status': [], 'tried': [], 'retries': 0, 'progress': {}, 'history': [], 'errors': []}
            if flow == 2: task['auto'] = False
            if manual:
                task['manual'] = self._manual_source(manual)
                if not parsed['title']: parsed['title'] = m.text(manual.get('title') or task['manual']['label'], 120); parsed['fields']['title'] = '下载名称'
                if not parsed['kind']: parsed['kind'] = manual.get('kind') if manual.get('kind') in ('movie', 'series') else ''
                if flow == 1 and (not parsed['title'] or not parsed['kind']): self.dl_state(task, 'needs_clarify', '人工链接需要填写片名和类型，用于整理入库')
                else: self.dl_state(task, 'queued', '已交给下载器')
            elif pick:
                used = next((t for t in self.dl_tasks(db) if (t.get('chosen') or {}).get('key') == pick['key'] and t['state'] not in ('failed', 'cancelled', 'no_results')), None)
                if used: raise m.Problem('该资源已在下载任务中：' + used['id'], 409)
                task.update(chosen=pick, candidates=[pick], sources_status=parent.get('sources_status', []), manual_pick=True)
                self.dl_state(task, 'queued', '已选择：' + pick['title'][:120])
            elif flow == 1 and needs_clarification(parsed): self.dl_state(task, 'needs_clarify', '；'.join(parsed['questions'] + parsed['warnings'])[:300] or '请确认识别结果')
            else: self.dl_state(task, 'searching', '正在搜索符合条件与相似资源')
            if flow == 2 and task['intent'] == 'download':
                if task['organize'] and parsed['kind'] not in ('movie', 'series'): raise m.Problem('整理入影视库时请选择电影或电视剧；普通下载无需类型')
                task['publish_after'] = task['publish_after'] and task['organize']
                task['storage'] = self.freeze_download_location(body.get('download_dir'))
                task['folder_name'] = self._safe_name(parsed['title'], 50) + '-' + ident
            existing = self._existing_media(db, parsed)
            if existing: task['existing_media'] = existing
            if flow == 2 and task['intent'] == 'search':
                for old in self.dl_tasks(db):
                    if old.get('intent') == 'search' and old.get('created_by') == user['name'] and not old.get('_archived'):
                        old['_archived'] = True; old['generation'] += 1; self.dl_save(db, old)
            self.dl_save(db, task)
            self.changed(db, user['name'], '搜索资源' if task['intent'] == 'search' else '新建下载任务', text or task['manual']['label'])
        self.start_download_workers()
        return self.download_task_view(task)

    @staticmethod
    def _apply_overrides(parsed, overrides):
        m = core()
        if not isinstance(overrides, dict): return
        if overrides.get('title'): parsed['title'] = m.text(overrides['title'], 120); parsed['fields']['title'] = '人工确认'
        if overrides.get('kind') in ('movie', 'series'): parsed['kind'] = overrides['kind']; parsed['fields']['kind'] = '人工确认'
        if 'year' in overrides: parsed['year'] = m.number(overrides['year'], 1900, 2100) if overrides['year'] else None; parsed['fields']['year'] = '人工确认'
        if overrides.get('resolution') in ('2160p', '1080p', '720p', ''): parsed['resolution'] = overrides['resolution']; parsed['fields']['resolution'] = '人工确认'
        if 'season' in overrides: parsed['season'] = m.number(overrides['season'], 1, 200) if overrides['season'] else None
        if 'episodes' in overrides:
            e = overrides['episodes']
            parsed['episodes'] = [m.number(e[0], 1, 5000), m.number(e[1], 1, 5000)] if isinstance(e, list) and len(e) == 2 and e[0] <= e[1] else None
        parsed['questions'] = [q for q in parsed['questions'] if not (('片名' in q and parsed['title']) or ('电影还是电视剧' in q and parsed['kind']))]
        if parsed['fields'].get('title') == '人工确认': parsed['warnings'] = [w for w in parsed['warnings'] if '由句子推断' not in w]

    def _manual_source(self, manual):
        m = core()
        if manual.get('torrent_b64'):
            try: data = base64.b64decode(manual['torrent_b64'], validate=True)
            except ValueError: raise m.Problem('种子文件内容无效')
            if len(data) > 700 * 1024: raise m.Problem('种子文件过大')
            try: info = torrent_info(data)
            except (ValueError, KeyError, TypeError, RecursionError): raise m.Problem('不是有效的种子文件')
            store = self.data / 'download-torrents'; store.mkdir(exist_ok=True)
            (store / (info['infohash'] + '.torrent')).write_bytes(data)
            return {'kind': 'torrent', 'infohash': info['infohash'], 'label': info['name'][:120], 'size': info['size']}
        uri = str(manual.get('uri') or '').strip()
        if uri.startswith('magnet:'):
            h = magnet_hash(uri)
            if not h: raise m.Problem('磁力链接缺少有效的 btih')
            return {'kind': 'magnet', 'uri': uri[:4000], 'infohash': h, 'label': '磁力 ' + h[:12]}
        parts = urlsplit(uri)
        if parts.scheme not in ('http', 'https') or not parts.hostname: raise m.Problem('只支持磁力链接、http/https 链接或上传种子')
        return {'kind': 'url', 'uri': uri[:4000], 'label': (parts.hostname + parts.path)[-120:]}

    def _existing_media(self, db, parsed):
        if not parsed['title']: return []
        want = normalize_title(parsed['title']); hits = []
        for r in db.execute('SELECT id,body FROM media'):
            body = json.loads(r['body'])
            if body.get('_archived') or normalize_title(body.get('title', '')) != want: continue
            if parsed['year'] and str(body.get('year') or '') not in ('', str(parsed['year'])): continue
            if parsed['kind'] and body.get('kind') and body['kind'] != parsed['kind']: continue
            hits.append({'id': r['id'], 'title': body.get('title', ''), 'year': body.get('year', ''), 'published': None})
        return hits[:5]

    # ---------- selection (D04) ----------
    def rank_candidates(self, db, settings, parsed, candidates, task_id='', broad=False):
        want = normalize_title(parsed['title']); used = set()
        for t in self.dl_tasks(db):
            if t['id'] != task_id and t['state'] not in ('failed', 'cancelled', 'no_results') and t.get('chosen'): used.add(t['chosen'].get('key'))
        ranked = []
        for c in candidates:
            a, reasons, blockers = c['attrs'], [], []
            title_part = re.split(r'(?:19|20)\d{2}|\bs\d{1,2}|2160p|1080p|720p|4k|第', c['title'], maxsplit=1, flags=re.I)[0]
            name = normalize_title(title_part) or normalize_title(c['title'])
            if want and want == name: reasons.append('片名匹配')
            elif broad and want and want in normalize_title(c['title']): reasons.append('标题包含搜索关键词')
            else: blockers.append('片名不匹配')
            if parsed['year']:
                if a['year'] == parsed['year']: reasons.append('年份 %d 一致' % parsed['year'])
                elif a['year']: blockers.append('年份 %d 不符' % a['year'])
                else: blockers.append('名称未标年份，需人工确认')
            if parsed['kind'] == 'movie' and a['season']: blockers.append('像是剧集')
            if parsed['kind'] == 'series':
                if parsed['season'] and a['season'] and a['season'] != parsed['season']: blockers.append('季数不符')
                if parsed['episodes'] and a['episodes'] and not (a['episodes'][0] <= parsed['episodes'][0] and a['episodes'][1] >= parsed['episodes'][1]): blockers.append('不含所需集数')
                if parsed['season'] and not a['season']: reasons.append('名称未标季数')
            quality_ok = True
            if parsed['resolution']:
                if a['resolution'] == parsed['resolution']: reasons.append('画质 %s 符合' % parsed['resolution'])
                else: quality_ok = False; blockers.append('画质为 %s，不是要求的 %s' % (a['resolution'] or '未标注', parsed['resolution']))
            limit = (parsed.get('max_size_gb') or 0) * 1024 ** 3
            if limit and c['size'] > limit: blockers.append('超过体积上限')
            if c['protocol'] not in ('torrent', 'http'): blockers.append('协议 %s 不受支持' % c['protocol'])
            if c['protocol'] == 'torrent' and c.get('seeders') == 0: blockers.append('当前无人做种')
            if c['key'] in used: blockers.append('已被其他任务使用')
            for pref in parsed.get('audio') or []:
                if pref in a['audio']: reasons.append('含' + pref)
                else: blockers.append('未确认含' + pref)
            for pref in parsed.get('subtitle') or []:
                if pref in a['subtitle']: reasons.append('含' + pref)
                else: blockers.append('未确认含' + pref)
            score = (5 if a['source'] == 'remux' else 4 if a['source'] == 'bluray' else 3 if a['source'] == 'web' else 1) + \
                    min(4, math.log10((c.get('seeders') or 0) + 1) * 2) + sum(1 for r in reasons if r.startswith('含'))
            ranked.append({**c, 'reasons': reasons, 'blockers': blockers, 'eligible': not blockers, 'quality_ok': quality_ok, 'score': round(score, 2),
                           'match': 'match' if not [b for b in blockers if b != '当前无人做种'] else 'similar',
                           'similarity': round(SequenceMatcher(None, want, name).ratio(), 3),
                           'can_download': c['protocol'] in ('torrent', 'http') and bool(c.get('link') or c.get('magnet')) and c['key'] not in used})
        ranked.sort(key=lambda c: (c['match'] != 'match', -c['similarity'], len(c['blockers']), not c['quality_ok'], -c['score'], c['size']))
        return ranked

    def _auto_allowed(self, task):
        p = task['parsed']
        return task['auto'] and p['kind'] in ('movie', 'series') and not p['questions'] and not p['fields'].get('title', '').startswith('句子剩余部分') and not task.get('existing_media')

    # ---------- actions ----------
    def download_action(self, body, user, revision):
        if self.automation_enabled(): raise core().Problem('新片请从Seerr加片；原下载流水线已冻结',409)
        m = core(); action = body.get('op'); ident = m.text(body.get('id'), 100, True)
        if action == 'download_candidate':
            return self.create_download({**body, 'flow_version': 2, 'candidate_from': {'id': ident, 'key': body.get('key')}, 'request': ''}, user, revision)
        with self._dl_lock, self.db() as db:
            self.check_revision(db, revision)
            task = self.dl_task(db, ident)
            if not task: raise m.Problem('任务不存在', 404)
            state = task['state']
            if action == 'choose':
                if state not in ('awaiting_choice', 'no_results', 'failed', 'searching'): raise m.Problem('当前状态不能选择来源')
                pick = next((c for c in task['candidates'] if c['key'] == body.get('key')), None)
                if not pick: raise m.Problem('候选不存在')
                if 'protocol' in pick and pick['protocol'] not in ('torrent', 'http'): raise m.Problem('该候选的协议不受支持')
                task['chosen'] = pick; task['generation'] += 1; task['manual_pick'] = True
                self.dl_state(task, 'queued', '人工选择：' + pick['title'][:120])
            elif action == 'clarify':
                if state not in ('needs_clarify', 'no_results', 'awaiting_choice', 'failed'): raise m.Problem('当前状态不能修改需求')
                self._apply_overrides(task['parsed'], body.get('overrides') or {})
                p = task['parsed']; task['norm'] = [normalize_title(p['title']), p['kind'], p['year'], p['season'], list(p['episodes'] or []), p['resolution']]
                task['existing_media'] = self._existing_media(db, p) if not body.get('accept_existing') else []
                task['generation'] += 1; task['candidates'] = []; task['chosen'] = None
                if task.get('manual'): self.dl_state(task, 'queued', '已补充片名与类型')
                elif needs_clarification(p) and not p['fields'].get('title') == '人工确认': self.dl_state(task, 'needs_clarify', '；'.join(p['questions'] + p['warnings']))
                else: self.dl_state(task, 'searching', '按确认后的条件重新搜索')
            elif action == 'pause':
                if state not in ('queued', 'downloading'): raise m.Problem('当前状态不能暂停')
                task['generation'] += 1; task['paused_from'] = state
                if task.get('engine_ref'): self._engine_call(task, 'pause')
                self.dl_state(task, 'paused', '人工暂停')
            elif action == 'resume':
                if state != 'paused': raise m.Problem('任务未暂停')
                if task.get('storage'): self.guard_download_location(task['storage'])
                task['generation'] += 1
                if task.get('engine_ref'): self._engine_call(task, 'resume'); self.dl_state(task, 'downloading', '继续下载')
                else: self.dl_state(task, 'queued', '继续排队')
            elif action == 'reannounce':
                if state != 'downloading' or task.get('engine') != 'qbittorrent' or not task.get('engine_ref'):
                    raise m.Problem('仅进行中的 BT / 磁力任务可以重新查找节点')
                now = m.now_ms()
                if now - task.get('reannounce_at', 0) < 60000: raise m.Problem('刚刚已查找，请等待一分钟让节点响应')
                self._engine_call(task, 'reannounce')
                task['reannounce_at'] = now
                # Preserve the hash, saved path, payload and stall timer.
                task['note'] = '已请求重新查找节点；是否开始传输取决于资源当前可用节点'
            elif action == 'cancel':
                if state in TERMINAL and state != 'no_results': raise m.Problem('任务已结束')
                task['generation'] += 1
                if task.get('engine_ref') and state in ('queued', 'downloading', 'paused'):
                    try: self._engine_call(task, 'cancel')
                    except NetError as exc: task['errors'].append('取消时下载器不可达：%s（已下载文件保留在暂存目录）' % exc)
                self.dl_state(task, 'cancelled', '已取消；已下载的文件保留在暂存目录，不会自动删除')
            elif action == 'retry':
                if state not in ('failed', 'no_results', 'cancelled'): raise m.Problem('只有失败、无结果或已取消的任务可以重试')
                task['generation'] += 1; task['errors'] = task['errors'][-5:]
                if task.get('download_done') and task.get('failed_at') in POST: self.dl_state(task, task['failed_at'], '从失败的后续步骤重试（不重新下载）')
                elif task.get('chosen') or task.get('manual'): task['engine_ref'] = None; task['engine'] = None; self.dl_state(task, 'queued', '重新下载')
                else: self.dl_state(task, 'searching', '重新搜索')
            elif action == 'retry_scrape':
                if not task.get('media_id'): raise m.Problem('任务尚未入库')
                task['generation'] += 1; task['scrape'] = {}; self.dl_state(task, 'scraping', '人工重试刮削')
            elif action == 'publish':
                if state != 'ready' or not task.get('media_id'): raise m.Problem('任务尚未就绪')
                task['generation'] += 1; task['force_publish'] = True; self.dl_state(task, 'publishing', '人工上架')
            elif action == 'organize':
                if state != 'downloaded': raise m.Problem('请等待下载完成后再整理入库')
                self._apply_overrides(task['parsed'], body.get('overrides') or {})
                if task['parsed']['kind'] not in ('movie', 'series'): raise m.Problem('整理入库请选择电影或电视剧')
                task.update(organize=True, publish_after=body.get('publish') is True)
                task['generation'] += 1; self.dl_state(task, 'verifying', '开始整理此下载到影视库')
            elif action == 'archive':
                if state in ACTIVE and state not in ('needs_clarify', 'awaiting_choice', 'needs_config'): raise m.Problem('进行中的任务请先取消')
                task['_archived'] = True
            else: raise m.Problem('操作无效')
            self.dl_save(db, task)
            self.audit(db, user['name'], '智能下载：' + action, ident)
        self.start_download_workers()
        return self.download_task_view(task)

    def download_task_view(self, value):
        """Download URLs may contain a tracker's passkey; clients only need opaque candidate keys."""
        if isinstance(value, dict): return {k: self.download_task_view(v) for k, v in value.items() if k not in ('uri', 'link', 'magnet', 'torrent_b64', 'engine_paths')}
        if isinstance(value, list): return [self.download_task_view(v) for v in value]
        if isinstance(value, str): return re.sub(r'([?&](?:api_?key|passkey|token|password|secret)=)[^&\s]+', r'\1[已隐藏]', value, flags=re.I)
        return value

    def _engine_call(self, task, method):
        with self.db() as db: settings = self.download_settings(db)
        engine = self.engine_for(settings, task['engine'])
        getattr(engine, method)(task['engine_ref'])

    def download_rows(self, args):
        arg = lambda k: args.get(k, [''])[0]
        with self.db() as db:
            tasks = [t for t in self.dl_tasks(db) if bool(t.get('_archived')) == (arg('archived') == '1')]
            notices = sorted(self.objects(db, 'download_notice'), key=lambda n: n['at'], reverse=True)[:30]
            settings = self.download_settings(db)
        if arg('state'): tasks = [t for t in tasks if t['state'] == arg('state') or (arg('state') == 'active' and t['state'] in ACTIVE)]
        if arg('q'): tasks = [t for t in tasks if arg('q').casefold() in (t.get('request', '') + t['parsed'].get('title', '')).casefold()]
        ready = {k: self.engine_ready(settings, k) for k in DEFAULTS['engines']}
        disk = self.download_disk({**settings, 'staging_dir': settings.get('save_dir') or settings['staging_dir']})
        tasks.sort(key=lambda t: t.get('created_at', 0), reverse=True)
        counts = {}
        for t in tasks: counts[t['state']] = counts.get(t['state'], 0) + 1
        return {'rows': self.download_task_view(tasks[:200]), 'total': len(tasks), 'counts': counts, 'labels': LABELS, 'notices': self.download_task_view(notices),
                'ready': {'sources': len(self.configured_sources(settings)), 'engines': ready, 'auto_mode': settings['auto_mode']}, 'disk': disk,
                'default_dir': settings.get('save_dir') or settings['staging_dir']}

    def download_disk(self, settings):
        """min_free_gb = 0 disables the guard (only sensible on test instances)."""
        try:
            probe = Path(settings['staging_dir'])
            while not probe.exists() and probe != probe.parent: probe = probe.parent
            usage = shutil.disk_usage(probe)
            return {'free_gb': round(usage.free / 1024 ** 3, 1), 'total_gb': round(usage.total / 1024 ** 3, 1), 'min_free_gb': settings['min_free_gb'],
                    'low': usage.free < settings['min_free_gb'] * 1024 ** 3}
        except OSError:
            return {'free_gb': None, 'total_gb': None, 'min_free_gb': settings['min_free_gb'], 'low': True}

    # ---------- workers (D06, D10, D11) ----------
    _dl_interval = 3.0

    def start_download_workers(self):
        if self.automation_enabled(): return
        with self._dl_lock:
            for attr, target in (('_dl_worker_thread', self._dl_loop), ('_dl_post_thread', self._dl_post_loop)):
                thread = self.__dict__.get(attr)
                if thread is None or not thread.is_alive():
                    thread = threading.Thread(target=target, name=attr, daemon=True); self.__dict__[attr] = thread; thread.start()

    def stop_download_workers(self, timeout=5):
        self.__dict__['_dl_stop'] = True
        for attr in ('_dl_worker_thread', '_dl_post_thread'):
            thread = self.__dict__.get(attr)
            if thread is not None: thread.join(timeout)

    def _dl_running(self): return self.data.exists() and not self.__dict__.get('_dl_stop')

    def _dl_loop(self):
        while self._dl_running():
            try: self._dl_tick()
            except Exception as exc: print('smart-download tick failed:', type(exc).__name__, str(exc)[:200], flush=True)
            time.sleep(self._dl_interval)

    def _dl_tick(self):
        m = core(); now = m.now_ms()
        with self.db() as db:
            settings = self.download_settings(db)
            tasks = [t for t in self.dl_tasks(db) if t['state'] in ACTIVE and not t.get('_archived')]
        tasks.sort(key=lambda t: t.get('created_at', 0))
        active = [t for t in tasks if t['state'] == 'downloading']
        for task in tasks:
            if task.get('next_at', 0) > now: continue
            effective = self.task_download_settings(task, settings)
            disk = self.download_disk(effective)
            try:
                if task['state'] == 'needs_config': self._dl_check_config(task, settings)
                elif task['state'] == 'searching': self._dl_launch_search(task, settings)
                elif task['state'] == 'queued':
                    if disk['low']: self._dl_wait(task, 60, '磁盘余量低于 %s GB，暂不开始新下载' % settings['min_free_gb'], notify='warn')
                    elif len(active) >= settings['concurrent_downloads']: continue
                    elif self._dl_start(task, effective): active.append(task)
                elif task['state'] == 'downloading': self._dl_poll(task, effective, disk)
                elif task['state'] == 'paused' and task.get('paused_by') == 'system' and not disk['low']:
                    self._dl_resume_system(task, settings)
            except NetError as exc:
                if exc.kind in ('blocked', 'invalid', 'conflict'):
                    self._dl_update(task, lambda t: (t.update(failed_at=t['state']), self.dl_state(t, 'failed', str(exc))), ('error', str(exc)))
                else: self._dl_wait(task, self._backoff(task), '%s，稍后自动重试' % exc, kind=exc.kind)
        self._dl_share_bandwidth(settings)

    def _dl_launch_search(self, task, settings):
        with self._dl_lock:
            active = self.__dict__.setdefault('_dl_searching', set())
            key = (task['id'], task['generation'])
            if key in active or len(active) >= 2: return
            active.add(key)
        def run():
            try:
                self._dl_search(task, settings)
            except (OSError, core().Problem):
                if self._dl_running(): self._dl_wait(task, 30, '搜索暂时失败，请重试')
            finally:
                with self._dl_lock: active.discard(key)
        threading.Thread(target=run, name='download-search', daemon=True).start()

    def _backoff(self, task):
        task['attempts'] = task.get('attempts', 0) + 1
        return min(900, 15 * 2 ** min(task['attempts'], 6))

    def _dl_wait(self, task, seconds, note, kind='', notify=''):
        with self.db() as db:
            current = self.dl_task(db, task['id'])
            if not current or current.get('generation') != task.get('generation'): return
            current['next_at'] = core().now_ms() + int(seconds * 1000); current['note'] = note[:300]
            current['attempts'] = task.get('attempts', current.get('attempts', 0))
            if kind in ('unreachable', 'timeout'): current['waiting_network'] = True
            if notify and current.get('last_notice') != note: self.dl_notice(db, current, notify, note); current['last_notice'] = note
            self.dl_save(db, current)

    def _dl_update(self, task, mutate, notice=None):
        """Apply `mutate` to the stored task if no newer control action happened; returns the saved task or None."""
        with self._dl_lock, self.db() as db:
            current = self.dl_task(db, task['id'])
            if not current or current.get('generation') != task.get('generation') or current['state'] != task['state']: return None
            mutate(current); current['next_at'] = 0; current['attempts'] = 0; current['waiting_network'] = False
            if notice: self.dl_notice(db, current, *notice)
            self.dl_save(db, current)
            return current

    def _dl_check_config(self, task, settings):
        if task.get('manual') or task.get('chosen'):
            if self.route_engine(settings, task.get('source') or task.get('manual') or {'kind': 'magnet' if task['chosen'].get('protocol') == 'torrent' else 'url'}):
                self._dl_update(task, lambda t: self.dl_state(t, 'queued', '下载器已就绪'))
        elif self.configured_sources(settings):
            self._dl_update(task, lambda t: self.dl_state(t, 'searching', '搜索源已就绪'))

    def _dl_search(self, task, settings):
        configured = self.configured_sources(settings)
        if not configured:
            self._dl_update(task, lambda t: self.dl_state(t, 'needs_config', '尚未启用搜索源，请在连接与策略设置中添加'), ('warn', '智能下载缺少搜索源配置'))
            return
        p = task['parsed']
        broad = task.get('flow_version') == 2
        query = p['title'] if broad else p['title'] + (' %d' % p['year'] if p['year'] else '') + (' S%02d' % p['season'] if p['kind'] == 'series' and p['season'] else '')
        found, status = sources.search_all(configured, query.strip(), p['kind'], limit=300 if broad else 100)
        if not self._dl_running(): return
        with self.db() as db: first_ranked = self.rank_candidates(db, settings, p, found, task['id'], broad=broad)
        if query != p['title'] and (not found or (task.get('flow_version') == 2 and not any(c['eligible'] for c in first_ranked))):
            more, status2 = sources.search_all(configured, p['title'], p['kind'])
            seen = {c['key'] for c in found}; found.extend(c for c in more if c['key'] not in seen)
            status = status + [dict(s, retry=True) for s in status2]
        if not self._dl_running(): return
        failed = [s for s in status if s['status'] != 'ok']
        if not found and len(failed) == len(status):
            self._dl_wait(task, self._backoff(task), '所有搜索源都不可用（%s），等待恢复后自动重试' % '；'.join('%s：%s' % (s['name'], s.get('message', s['status'])) for s in failed)[:250], kind='unreachable')
            return
        with self.db() as db: ranked = self.rank_candidates(db, settings, p, found, task['id'], broad=broad)
        eligible = [c for c in ranked if c['eligible']]
        def apply(t):
            t['candidates'] = ranked[:300 if broad else 60]; t['sources_status'] = status; t['searched_at'] = core().now_ms()
            note = ('部分搜索源失败：' + '、'.join(s['name'] for s in failed) + '。') if failed else ''
            if not ranked: self.dl_state(t, 'no_results', note + '没有找到任何结果'); return
            if task.get('flow_version') == 2:
                exact = sum(c['match'] == 'match' for c in ranked)
                self.dl_state(t, 'awaiting_choice', note + '找到 %d 个符合条件、%d 个相似结果，选择即可下载' % (exact, len(ranked) - exact)); return
            if not eligible:
                quality = [c for c in ranked if not c['quality_ok'] and len(c['blockers']) == 1]
                hint = '未找到 %s 资源，另有 %d 个其他画质候选，需人工确认（不会自动降级）' % (p['resolution'], len(quality)) if quality else '找到 %d 个结果，但都不符合条件' % len(ranked)
                self.dl_state(t, 'awaiting_choice', note + hint); return
            if self._auto_allowed(t):
                t['chosen'] = eligible[0]; self.dl_state(t, 'queued', note + '自动选择：' + eligible[0]['title'][:100] + '（' + '、'.join(eligible[0]['reasons'][:4]) + '）')
            else:
                why = '已有同名媒体，' if t.get('existing_media') else '' if t['auto'] else '自动模式未开启，'
                self.dl_state(t, 'awaiting_choice', note + why + '请从 %d 个合格候选中选择' % len(eligible))
        updated = self._dl_update(task, apply)
        if updated and updated['state'] in ('no_results', 'awaiting_choice'):
            self._dl_update(updated, lambda t: None, ('info', '「%s」%s' % (p['title'], updated['note'][:120])))

    def _resolve_source(self, settings, task):
        """Turn the chosen candidate / manual entry into what an engine can take; torrents are fetched here so the
        info-hash is known before anything is added (idempotent adds, duplicate detection)."""
        manual = task.get('manual')
        if manual:
            if manual['kind'] == 'torrent': return {'kind': 'torrent', 'data': (self.data / 'download-torrents' / (manual['infohash'] + '.torrent')).read_bytes(), 'infohash': manual['infohash']}
            if manual['kind'] == 'magnet': return {'kind': 'magnet', 'uri': manual['uri'], 'infohash': manual['infohash']}
            if urlsplit(manual['uri']).path.lower().endswith('.torrent'):
                status, h, body, _ = request(manual['uri'], timeout=20, limit=4 * 1024 * 1024)
                info = torrent_info(body); return {'kind': 'torrent', 'data': body, 'infohash': info['infohash']}
            name = unquote(urlsplit(manual['uri']).path.rsplit('/', 1)[-1])[:200]
            check_url(manual['uri'])
            return {'kind': 'url', 'uri': manual['uri'], 'filename': re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', name) if name else ''}
        c = task['chosen']
        if c.get('protocol') == 'http':
            check_url(c['link'])
            return {'kind': 'url', 'uri': c['link'], 'filename': ''}
        if c.get('link'):
            source_conf = next((s for s in settings['sources'] if s['id'] == c['source']), None)
            trusted = sources.trusted(source_conf['url']) if source_conf else set()
            status, h, body, _ = request(c['link'], timeout=25, limit=4 * 1024 * 1024, trusted_hosts=trusted)
            if status == 'magnet':
                digest = magnet_hash(body.decode())
                if not digest or (c.get('infohash') and c['infohash'].lower() != digest):
                    raise NetError('invalid', '磁力跳转与搜索结果的 info-hash 不一致')
                return {'kind': 'magnet', 'uri': body.decode(), 'infohash': digest}
            try: info = torrent_info(body)
            except ValueError: raise NetError('invalid', '候选链接返回的不是种子文件')
            if c.get('infohash') and c['infohash'].lower() != info['infohash']:
                raise NetError('invalid', '种子文件与搜索结果的 info-hash 不一致')
            return {'kind': 'torrent', 'data': body, 'infohash': info['infohash']}
        if c.get('magnet'): return {'kind': 'magnet', 'uri': c['magnet'], 'infohash': magnet_hash(c['magnet']) or c.get('infohash', '')}
        raise NetError('invalid', '候选没有可用的下载地址')

    def _dl_start(self, task, settings):
        source = self._resolve_source(settings, task)
        kind = self.route_engine(settings, source)
        if not kind:
            need = 'BT/磁力需要 qBittorrent' if source['kind'] != 'url' else '直链需要 aria2'
            self._dl_update(task, lambda t: self.dl_state(t, 'needs_config', '没有可用的下载器：' + need), ('warn', '智能下载缺少下载器配置：' + need))
            return False
        if source.get('infohash'):
            with self.db() as db:
                clash = next((t for t in self.dl_tasks(db) if t['id'] != task['id'] and t.get('infohash') == source['infohash'] and t['state'] not in ('failed', 'cancelled')), None)
            if clash:
                self._dl_update(task, lambda t: self.dl_state(t, 'failed', '相同资源（info-hash %s）已在任务 %s 中，避免重复下载' % (source['infohash'][:12], clash['id'])))
                return False
        engine = self.engine_for(settings, kind)
        if task.get('storage'):
            if not task['storage']['engine_paths'].get(kind):
                raise NetError('blocked', '所选文件夹尚未开放给此下载器')
            try: self.prepare_download_directory(task)
            except (core().Problem, OSError) as exc: raise NetError('blocked', str(exc))
        directory = self.engine_dir(settings, kind, task['id'])
        if kind != 'qbittorrent' and not settings['engines'][kind].get('directory'): Path(directory).mkdir(parents=True, exist_ok=True)
        key = '%s-%d' % (task['id'], task.get('retries', 0))
        limit = max(1, settings['speed_limit_kbps'] // max(1, settings['concurrent_downloads'])) if settings['speed_limit_kbps'] else 0
        # Record the deterministic reference BEFORE the remote call. A lost response or a concurrent
        # cancellation can still reconcile/control that exact job, even while its state is queued.
        with self._dl_lock:
            with self.db() as db:
                current = self.dl_task(db, task['id'])
                if not current or current['generation'] != task['generation'] or current['state'] != task['state']: return False
                current.update(engine=kind, engine_ref=source.get('infohash') if kind == 'qbittorrent' else engines.Aria2.gid(key),
                               infohash=source.get('infohash', ''), engine_dir=directory)
                self.dl_save(db, current)
            ref = engine.add(key, source, directory, limit)
            def apply(t):
                t.update(engine_ref=ref, source_kind=source['kind'], started_at=core().now_ms(),
                         progress={'downloaded': 0, 'total': None, 'speed': 0, 'eta': None}, stall={'bytes': 0, 'since': core().now_ms()})
                self.dl_state(t, 'downloading', '已交给 %s' % {'qbittorrent': 'qBittorrent', 'aria2': 'aria2'}[kind])
            return self._dl_update(task, apply) is not None

    def _dl_poll(self, task, settings, disk):
        engine = self.engine_for(settings, task['engine'])
        if task.get('storage'):
            try: self.guard_download_location(task['storage'])
            except (core().Problem, OSError) as exc:
                engine.pause(task['engine_ref'])
                self._dl_update(task, lambda t: (t.update(paused_by='storage'), self.dl_state(t, 'paused', str(exc))))
                return
        st = engine.status(task['engine_ref'])
        now = core().now_ms()
        if settings['min_free_gb'] and disk['free_gb'] is not None and disk['free_gb'] < settings['min_free_gb'] / 2:
            engine.pause(task['engine_ref'])
            self._dl_update(task, lambda t: (t.update(paused_by='system', paused_from='downloading'), self.dl_state(t, 'paused', '磁盘余量严重不足，已自动暂停')),
                            ('error', '磁盘余量低于 %s GB，智能下载已自动暂停' % (settings['min_free_gb'] / 2)))
            return
        if st['state'] == 'complete':
            def done(t):
                t['progress'] = {'downloaded': st['downloaded'] or st['total'], 'total': st['total'], 'speed': 0, 'eta': 0}
                t['download_done'] = True; t['engine_files'] = st['files']; t['engine_directory'] = st['directory']
                self.dl_state(t, 'verifying', '下载完成（下载器确认），开始校验文件')
            self._dl_update(task, done); return
        if st['state'] in ('error', 'missing'):
            if st['state'] == 'missing' and not task.get('readded'):
                source = self._resolve_source(settings, task)
                with self._dl_lock:
                    with self.db() as db: current = self.dl_task(db, task['id'])
                    if not current or current['generation'] != task['generation'] or current['state'] != task['state']: return
                    ref = engine.add('%s-%d' % (task['id'], task.get('retries', 0)), source, task['engine_dir'], 0)
                    self._dl_update(task, lambda t: (t.update(engine_ref=ref, readded=True), self.dl_state(t, 'downloading', '下载器中任务丢失，已按原来源重新登记')))
                return
            self._dl_next_candidate(task, st['error'] or '下载失败'); return
        stall = task.get('stall') or {'bytes': 0, 'since': now}
        if st['downloaded'] > stall['bytes']: stall = {'bytes': st['downloaded'], 'since': now}
        elif now - stall['since'] > settings['stall_minutes'] * 60000:
            engine.pause(task['engine_ref'])
            self._dl_next_candidate(task, '%d 分钟没有任何进度（可能无人做种或来源失效）' % settings['stall_minutes']); return
        def progress(t):
            t['progress'] = {k: st[k] for k in ('downloaded', 'total', 'speed', 'eta')}
            t['progress'].update(engine_state=st['state'], **{k: st[k] for k in ('seeds', 'peers') if k in st})
            t['progress']['waiting_seconds'] = max(0, (now - stall['since']) // 1000) if not st['speed'] else 0
            t['stall'] = stall
            t['note'] = {'queued': '下载器正在排队，等待空闲下载名额',
                         'metadata': '等待磁力元数据，尚未开始下载文件；可重新查找节点，或返回搜索结果选择其他版本',
                         'stalled': '暂时没有文件传输，等待可用节点；可重新查找节点，或选择其他版本',
                         'checking': '下载器正在校验已有文件', 'allocating': '下载器正在准备磁盘空间',
                         'moving': '下载器正在移动文件', 'unknown': '下载器状态暂不明确，请稍后刷新或查看下载器',
                         'paused': '下载器中已暂停'}.get(st['state'], '正在接收文件' if st['speed'] else '等待文件传输')
        self._dl_update(task, progress)

    def _dl_next_candidate(self, task, reason):
        with self.db() as db: settings = self.task_download_settings(task, self.download_settings(db))
        def apply(t):
            t['errors'].append(reason[:200]); t['tried'].append((t.get('chosen') or {}).get('key') or 'manual')
            alternatives = [c for c in t.get('candidates', []) if c['eligible'] and c['key'] not in t['tried']]
            if t.get('manual') or t.get('manual_pick') or t['retries'] >= settings['max_candidates_retry'] or not alternatives or not self._auto_allowed(t):
                self.dl_state(t, 'failed', reason + ('；可在详情中选择其他来源或重试' if alternatives else '')); t['failed_at'] = 'downloading'; return
            t['retries'] += 1; t['chosen'] = alternatives[0]; t['engine_ref'] = None; t['readded'] = False
            self.dl_state(t, 'queued', reason + '；改用下一个候选：' + alternatives[0]['title'][:100])
        updated = self._dl_update(task, apply)
        if updated and updated['state'] == 'failed':
            self._dl_update(updated, lambda t: None, ('error', '「%s」下载失败：%s' % (updated['parsed']['title'] or updated['id'], reason[:120])))

    def _dl_resume_system(self, task, settings):
        if task.get('storage'):
            try: self.guard_download_location(task['storage'])
            except (core().Problem, OSError): return
        with self._dl_lock:
            with self.db() as db: current = self.dl_task(db, task['id'])
            if not current or current['generation'] != task['generation'] or current['state'] != task['state']: return
            self.engine_for(settings, task['engine']).resume(task['engine_ref'])
            self._dl_update(task, lambda t: (t.pop('paused_by', None), self.dl_state(t, 'downloading', '磁盘余量恢复，自动继续')))

    def _dl_share_bandwidth(self, settings):
        with self.db() as db: running = [t for t in self.dl_tasks(db) if t['state'] == 'downloading' and t.get('engine_ref')]
        share = max(1, settings['speed_limit_kbps'] // max(1, len(running))) if settings['speed_limit_kbps'] else 0
        for t in running:
            if t.get('limit_kbps') == share: continue
            try: self.engine_for(settings, t['engine']).set_limit(t['engine_ref'], share)
            except NetError: continue
            self._dl_update(t, lambda x: x.update(limit_kbps=share))

    # ---------- post-download pipeline (D07–D10) ----------
    class Retry(Exception):
        def __init__(self, seconds, note): super().__init__(note); self.seconds = seconds

    class Stop(Exception):
        pass

    def _dl_post_loop(self):
        while self._dl_running():
            try:
                with self.db() as db: tasks = sorted([t for t in self.dl_tasks(db) if t['state'] in POST and not t.get('_archived')], key=lambda t: t.get('updated_at', 0))
                due = [t for t in tasks if t.get('next_at', 0) <= core().now_ms()]
                if due: self._dl_post_step(due[0])
                else: time.sleep(self._dl_interval)
            except Exception as exc:
                print('smart-download post step failed:', type(exc).__name__, str(exc)[:200], flush=True); time.sleep(self._dl_interval)

    def _dl_post_step(self, task):
        if task['state'] == 'publishing':
            with self._dl_lock: return self._dl_post_step_locked(task)
        return self._dl_post_step_locked(task)

    def _dl_post_step_locked(self, task):
        with self.db() as db:
            current = self.dl_task(db, task['id'])
            if not current or current['generation'] != task['generation'] or current['state'] != task['state']: return
        with self.db() as db: settings = self.task_download_settings(task, self.download_settings(db))
        step = {'verifying': self._dl_verify, 'organizing': self._dl_organize, 'importing': self._dl_import, 'scraping': self._dl_scrape,
                'converting': self._dl_convert, 'publishing': self._dl_publish}[task['state']]
        try:
            changes, next_state, note = step(task, settings)
            notice = None
            if next_state in ('ready', 'downloaded'): notice = ('success', note)
            self._dl_update(task, lambda t: (t.update(changes), self.dl_state(t, next_state, note)), notice)
        except self.Retry as r:
            self._dl_wait(task, r.seconds, str(r))
        except (self.Stop, core().Problem, OSError, ValueError, subprocess.SubprocessError) as exc:
            reason = str(exc) or type(exc).__name__
            self._dl_update(task, lambda t: (t['errors'].append(reason[:200]), t.update(failed_at=task['state']),
                                             self.dl_state(t, 'failed', '%s失败：%s（已下载文件保留，可单独重试后续步骤）' % (LABELS[task['state']], reason[:150]))),
                            ('error', '「%s」%s失败：%s' % (task['parsed']['title'] or task['id'], LABELS[task['state']], reason[:100])))

    def _ffprobe(self, path):
        tool = self.compat_tools()['ffprobe']
        if not tool: raise self.Stop('服务器未安装 ffprobe，无法校验媒体文件')
        out = subprocess.run([tool, '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', str(path)], capture_output=True, timeout=180, check=False)
        if out.returncode != 0: return None
        info = json.loads(out.stdout.decode('utf-8', 'replace') or '{}')
        video = next((s for s in info.get('streams', []) if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')), None)
        if not video: return None
        height, width = int(video.get('height') or 0), int(video.get('width') or 0)
        res = '2160p' if height >= 1800 or width >= 3400 else '1080p' if height >= 900 or width >= 1800 else '720p' if height >= 600 or width >= 1200 else 'sd'
        return {'duration': float(info.get('format', {}).get('duration') or 0), 'resolution': res, 'codec': video.get('codec_name', ''), 'width': width, 'height': height}

    @staticmethod
    def _episode_of(name):
        m = re.search(r'(?<![a-z0-9])s(\d{1,2})[ ._-]*e(\d{1,3})', name, re.I) or re.search(r'第\s*(\d{1,2})\s*季.*?第\s*(\d{1,3})\s*集', name)
        if m: return int(m.group(1)), int(m.group(2))
        m = re.search(r'(?<![a-z0-9])e(?:p)?(\d{1,3})(?![0-9])|第\s*(\d{1,3})\s*集|(?<![0-9])(\d{1,3})(?![0-9p])', name, re.I)
        return (None, int(next(g for g in m.groups() if g))) if m else (None, None)

    def _dl_verify(self, task, settings):
        """Only files the engine reports complete, that exist with the reported size, stay unchanged and pass ffprobe."""
        if task.get('flow_version') == 2 and not task.get('organize'): return self._verify_plain_download(task, settings)
        if task.get('storage'):
            try: self.guard_download_location(task['storage'])
            except (core().Problem, OSError) as exc: raise self.Stop(str(exc))
        files = []
        largest = max([f['size'] for f in task.get('engine_files') or [] if Path(f['path']).suffix.lower() in VIDEO] or [0])
        for f in task.get('engine_files') or []:
            raw = f['path'] if f['path'].startswith('/') else (task.get('engine_directory') or task.get('engine_dir', '')).rstrip('/') + '/' + f['path']
            path = self.host_path(settings, task['engine'], raw)
            if task.get('storage') and (Path(task['storage']['path']) / task['folder_name']).resolve() not in path.parents:
                raise self.Stop('下载器返回了任务文件夹之外的文件，已拒绝')
            name = path.name.lower()
            # A sample is a small extra clip named "sample" (or inside a Sample folder) — not a title that contains the word.
            sample = (re.search(r'(^|[ ._\-\[(])sample([ ._\-\])]|$)', path.stem.lower()) or path.parent.name.lower() == 'sample') and f['size'] < largest * 0.2
            if path.suffix.lower() not in VIDEO or name.endswith(PARTIAL) or sample: continue
            if not path.is_file() or path.is_symlink(): raise self.Retry(30, '等待文件出现在暂存目录：' + path.name)
            st = path.stat()
            if f['size'] and st.st_size != f['size']: raise self.Retry(30, '文件大小与下载器不一致，等待写入完成：' + path.name)
            files.append({'path': str(path), 'size': st.st_size, 'mtime': st.st_mtime_ns, 'engine_size': f['size']})
        if not files: raise self.Stop('下载内容中没有可用的视频文件（已排除样片、未完成和非视频文件）')
        time.sleep(2)
        for f in files:
            st = Path(f['path']).stat()
            if (st.st_size, st.st_mtime_ns) != (f['size'], f['mtime']): raise self.Retry(30, '文件仍在变化，稍后再校验')
        p = task['parsed']; picked = []
        for f in sorted(files, key=lambda x: -x['size']):
            probe = self._ffprobe(f['path'])
            if not probe or probe['duration'] < settings['min_video_seconds']: continue
            f['probe'] = probe
            if p['kind'] == 'series':
                season, number = self._episode_of(Path(f['path']).name)
                season = season or p['season'] or 1
                if p['season'] and season != p['season']: continue
                if p['episodes'] and (number is None or not p['episodes'][0] <= number <= p['episodes'][1]): continue
                f['season'], f['episode'] = season, number
            picked.append(f)
            if p['kind'] != 'series': break
        if not picked: raise self.Stop('没有通过 ffprobe 检查、且符合要求的视频（时长至少 %d 秒）' % settings['min_video_seconds'])
        if p['kind'] == 'series' and any(f.get('episode') is None for f in picked): raise self.Stop('无法从文件名识别集数，请人工整理')
        missing = []
        if p['kind'] == 'series' and p['episodes']:
            have = {f['episode'] for f in picked}
            missing = [n for n in range(p['episodes'][0], p['episodes'][1] + 1) if n not in have]
        actual = picked[0]['probe']['resolution']
        warnings = []
        order = {'sd': 0, '720p': 1, '1080p': 2, '2160p': 3}
        if p['resolution'] and order.get(actual, 0) < order.get(p['resolution'], 0):
            warnings.append('名称或要求为 %s，ffprobe 实测为 %s；不会自动上架，请人工确认' % (p['resolution'], actual))
        if missing: warnings.append('缺少第 %s 集' % '、'.join(map(str, missing)))
        note = '校验通过：%d 个视频，实测 %s' % (len(picked), actual) + ('；' + '；'.join(warnings) if warnings else '')
        return {'verified': picked, 'probe_resolution': actual, 'quality_warning': bool(warnings and p['resolution'] and order.get(actual, 0) < order.get(p['resolution'], 0)), 'warnings': warnings}, 'organizing', note

    def _verify_plain_download(self, task, settings):
        try: self.guard_download_location(task['storage'])
        except (core().Problem, OSError) as exc: raise self.Stop(str(exc))
        root = (Path(task['storage']['path']) / task['folder_name']).resolve()
        files = []
        for f in task.get('engine_files') or []:
            raw = f['path'] if f['path'].startswith('/') else (task.get('engine_directory') or task['engine_dir']).rstrip('/') + '/' + f['path']
            path = self.host_path(settings, task['engine'], raw)
            if root not in path.parents or path.is_symlink(): raise self.Stop('下载器返回了任务文件夹之外的文件，已拒绝')
            if not path.is_file(): raise self.Retry(5, '等待文件保存完成：' + path.name)
            stat = path.stat()
            if f['size'] and stat.st_size != f['size']: raise self.Retry(5, '等待文件大小与下载器确认一致：' + path.name)
            files.append({'path': str(path), 'size': stat.st_size, 'mtime': stat.st_mtime_ns})
        if not files: raise self.Stop('下载器已结束，但没有返回已保存的文件')
        time.sleep(.3)
        for f in files:
            stat = Path(f['path']).stat()
            if (stat.st_size, stat.st_mtime_ns) != (f['size'], f['mtime']): raise self.Retry(5, '文件仍在写入，稍后确认')
        return {'saved_files': files[:1000], 'saved_file_count': len(files), 'final_path': str(root)}, 'downloaded', '下载完成，%d 个文件已保存到 %s' % (len(files), root)

    @staticmethod
    def _safe_name(text, limit=80):
        text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', ' ', str(text)).strip().strip('.')
        return re.sub(r'\s+', ' ', text)[:limit] or '未命名'

    @staticmethod
    def _sample_hash(path):
        h = hashlib.sha256(); size = os.path.getsize(path)
        with open(path, 'rb') as f:
            for offset in (0, max(0, size // 2 - 524288), max(0, size - 1048576)):
                f.seek(offset); h.update(f.read(1048576))
        return '%d:%s' % (size, h.hexdigest()[:32])

    def _inside_media(self, path):
        m = core(); resolved = path.resolve()
        if self.media not in resolved.parents: raise m.Problem('整理目标不在片库目录内')
        probe = self.media
        for part in path.relative_to(self.media).parts[:-1]:
            probe = probe / part
            if probe.is_symlink(): raise m.Problem('整理目标路径含符号链接，已拒绝：' + part)
        return resolved

    def _dl_organize(self, task, settings):
        """Publish into the library without moving or touching the seeding files: hard link when possible, otherwise copy
        to a hidden temp file in the destination folder, verify, then atomically rename. Never overwrite."""
        p = task['parsed']; organized = list(task.get('organized') or [])
        done = {o['src'] for o in organized}
        title = self._safe_name(p['title']); year = str(p['year'] or '')
        if p['kind'] == 'series':
            show_dir = self.media / settings['series_dir'] / title
            meta_dir, meta = show_dir, {'title': p['title'][:120], 'category': settings['series_dir']}
        else:
            folder = '%s (%s)' % (title, year) if year else title
            show_dir = self.media / settings['movie_dir'] / settings['movie_category'] / folder
            meta_dir, meta = show_dir, {'title': p['title'][:120], 'year': year, 'category': settings['movie_category']}
        for f in task['verified']:
            if f['path'] in done: continue
            src = Path(f['path']); ext = src.suffix.lower(); res = f['probe']['resolution']
            if p['kind'] == 'series':
                dest = show_dir / ('Season %02d' % f['season']) / ('%s - S%02dE%02d%s' % (title, f['season'], f['episode'], ext))
            else:
                dest = show_dir / ('%s%s - %s%s' % (title, ' (%s)' % year if year else '', res, ext))
            self._inside_media(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            src_hash = self._sample_hash(src)
            n = 1
            while dest.exists() or dest.is_symlink():
                if dest.is_file() and not dest.is_symlink() and self._sample_hash(dest) == src_hash: break   # already published (idempotent)
                n += 1; dest = dest.with_name('%s (版本%d)%s' % (dest.stem.split(' (版本')[0], n, ext))
            method = 'existing'
            if not dest.exists():
                temp = dest.parent / ('.dl-%s.part' % task['id'])
                if temp.exists(): temp.unlink()
                try: os.link(src, temp); method = 'hardlink'
                except OSError:
                    shutil.copyfile(src, temp); method = 'copy'
                    if self._sample_hash(temp) != src_hash or temp.stat().st_size != src.stat().st_size:
                        temp.unlink(); raise core().Problem('复制校验失败（可能磁盘已满），原文件保留，可重试')
                os.replace(temp, dest)
            route = '/media/' + quote(dest.relative_to(self.media).as_posix(), safe='/')
            organized.append({'src': str(src), 'dest': str(dest), 'route': route, 'method': method, 'season': f.get('season'), 'episode': f.get('episode')})
        if not (meta_dir / 'metadata.json').exists():
            temp = meta_dir / '.metadata.json.part'; temp.write_text(json.dumps(meta, ensure_ascii=False)); os.replace(temp, meta_dir / 'metadata.json')
        return {'organized': organized}, 'importing', '已整理到片库：%s（%s）' % (show_dir.relative_to(self.media), '、'.join(sorted({o['method'] for o in organized})))

    def _find_media(self, db, routes):
        for r in db.execute('SELECT id,body,published,available FROM media'):
            body = json.loads(r['body'])
            paths = {body.get('path')} | {e.get('path') for e in body.get('episodes', [])}
            if paths & set(routes): return r['id'], body, bool(r['published']), bool(r['available'])
        return None

    def _dl_import(self, task, settings):
        routes = [o['route'] for o in task['organized']]
        with self.db() as db: found = self._find_media(db, routes)
        if found and not task.get('scan_job'):
            return {'media_id': found[0]}, 'scraping', '已在片库索引中：%s' % found[1].get('title', '')
        job_id = task.get('scan_job')
        if job_id:
            with self.db() as db: row = db.execute('SELECT body FROM jobs WHERE id=?', (job_id,)).fetchone()
            job = json.loads(row['body']) if row else {'status': 'missing'}
            if job['status'] in ('queued', 'running'): raise self.Retry(5, '片库扫描进行中')
            with self.db() as db: found = self._find_media(db, routes)
            if found: return {'media_id': found[0], 'scan_job': None}, 'scraping', '扫描入库完成：%s' % found[1].get('title', '')
            if job['status'] == 'failed': return {'scan_job': None}, 'importing', '片库扫描失败，稍后重试'
            raise self.Stop('扫描完成但没有找到整理后的文件，请检查片库来源配置')
        try: job = self.start_scan('main', 'system:智能下载')
        except core().Problem as exc:
            if exc.status == 409: raise self.Retry(20, '片库扫描或同步正在进行，稍后入库')
            raise
        return {'scan_job': job['id']}, 'importing', '已触发片库扫描'

    def _revision(self):
        with self.db() as db: return self.kv(db, 'revision')

    def _dl_scrape(self, task, settings):
        m = core()
        try: configured = bool(self.jellyfin_config())
        except m.Problem: configured = False
        if not configured: return {'scrape': {'status': 'skipped'}}, 'converting', '未配置 Jellyfin，使用整理时写入的片名信息（metadata.json）'
        with self.db() as db: found = self._find_media(db, [o['route'] for o in task['organized']])
        if not found: return {'media_id': None}, 'importing', '索引中暂时找不到该条目，重新入库'
        media_id, body = found[0], found[1]
        scrape = dict(task.get('scrape') or {}); now = m.now_ms()
        if body.get('jellyfin_id'):
            return {'scrape': {**scrape, 'status': 'done'}, 'media_id': media_id}, 'converting', 'Jellyfin 元数据已同步（%s）' % body.get('title', '')
        scrape.setdefault('started_at', now)
        if now - scrape['started_at'] > settings['scrape_wait_minutes'] * 60000:
            return {'scrape': {**scrape, 'status': 'failed'}}, 'converting', 'Jellyfin 在 %d 分钟内未识别该文件；可播放的文件已入库，可在详情中单独重试刮削' % settings['scrape_wait_minutes']
        if now - scrape.get('last_sync', 0) > 30000:
            try: self.start_jellyfin_sync('system:智能下载', refresh=not scrape.get('refreshed'))
            except m.Problem as exc:
                if exc.status in (409,): raise self.Retry(30, '片库同步正在进行，稍后刮削')
                return {'scrape': {**scrape, 'status': 'failed', 'error': str(exc)[:120]}}, 'converting', 'Jellyfin 同步失败（%s），文件已入库，可稍后重试刮削' % str(exc)[:80]
            scrape.update(refreshed=True, last_sync=now)
            with self.db() as db:
                current = self.dl_task(db, task['id'])
                if current and current.get('generation') == task.get('generation'): current['scrape'] = scrape; self.dl_save(db, current)
        raise self.Retry(5, '等待 Jellyfin 识别并同步元数据')

    def _dl_convert(self, task, settings):
        m = core()
        if not settings['compat_auto'] or not self.compat_tools()['ffprobe']: return {}, 'publishing', '未启用自动兼容检查'
        routes = [o['route'] for o in task['organized']]
        ids = [self.compat_id(r) for r in routes]
        with self.db() as db: records = [self.compat_record(db, i) for i in ids]
        if any(r is None for r in records):
            try: self.compat_probe([task['media_id']], 'system:智能下载', self._revision())
            except m.Problem as exc:
                if exc.status == 409: raise self.Retry(10, '版本冲突，稍后兼容检查')
                raise
            with self.db() as db: records = [self.compat_record(db, i) for i in ids]
        if any(r is None or r.get('verdict') == 'error' for r in records): raise self.Stop('兼容检查未能读取全部媒体，拒绝自动上架')
        need = [r for r in records if r and r.get('verdict') in ('remux', 'audio', 'video')]
        if not need: return {'compat': {'status': 'ok'}}, 'publishing', '兼容检查：可直接播放'
        started = (task.get('compat') or {}).get('started_at') or m.now_ms()
        failed = [r for r in need if r.get('job_status') in ('failed', 'cancelled')]
        if failed: raise self.Stop('兼容版生成失败，请在兼容作业中重试后再继续：' + (failed[0].get('job_error') or failed[0]['job_status']))
        if m.now_ms() - started > settings['compat_wait_minutes'] * 60000: raise self.Stop('兼容转码超过 %d 分钟仍未完成' % settings['compat_wait_minutes'])
        todo = [r['id'] for r in need if r.get('job_status') not in ('queued', 'running', 'paused', 'done')]
        if todo:
            try: self.compat_enqueue(todo, 'system:智能下载', self._revision())
            except m.Problem as exc:
                if exc.status in (409, 503): raise self.Retry(30, '兼容转码暂不可用：%s' % exc)
                raise
            with self.db() as db:
                current = self.dl_task(db, task['id'])
                if current and current.get('generation') == task.get('generation'): current['compat'] = {'status': 'queued', 'started_at': started}; self.dl_save(db, current)
            raise self.Retry(20, '已加入兼容转码队列（不修改原文件）')
        if all(r.get('job_status') == 'done' for r in need): return {'compat': {'status': 'done'}}, 'publishing', '兼容版已生成，电视将播放兼容版'
        failed = [r for r in need if r.get('job_status') in ('failed', 'cancelled')]
        if failed:
            if any(r['verdict'] == 'video' for r in failed): raise self.Stop('电视无法直接解码，且兼容版生成失败：' + (failed[0].get('job_error') or '未知原因'))
            return {'compat': {'status': 'partial'}}, 'publishing', '兼容版生成失败，但原文件电视可播放（已记录）'
        if m.now_ms() - started > settings['compat_wait_minutes'] * 60000: raise self.Stop('兼容转码超过 %d 分钟仍未完成' % settings['compat_wait_minutes'])
        raise self.Retry(30, '兼容转码进行中（低优先级，不抢占播放）')

    def _dl_publish(self, task, settings):
        m = core()
        with self.db() as db:
            current = self.dl_task(db, task['id'])
            if not current or current['generation'] != task['generation'] or current['state'] != 'publishing': raise self.Stop('任务已取消或变更，停止上架')
            row = db.execute('SELECT body,published,available FROM media WHERE id=?', (task['media_id'],)).fetchone()
        if not row: return {'media_id': None}, 'importing', '条目已不在索引中，重新入库'
        body = json.loads(row['body'])
        if body.get('_archived'): raise self.Stop('该条目已被归档，不自动上架')
        automatic = task.get('publish_after') if task.get('flow_version') == 2 else task['auto'] and settings['auto_publish']
        want = task.get('force_publish') or (automatic and not task.get('quality_warning'))
        published = bool(row['published'])
        if want and not published:
            try: result = self.batch_media({'ids': [task['media_id']], 'action': 'publish'}, 'system:智能下载', self._revision())
            except m.Problem as exc:
                if exc.status == 409: raise self.Retry(5, '版本冲突，稍后上架')
                raise
            if result.get('failed'): raise self.Stop('上架被拒绝：' + result['failed'][0].get('reason', ''))
            published = True
        playable = []
        if published:
            catalog = self.catalog({'id': 'smart-download-check', 'room': '-', 'group': '全部客房'})
            item = next((i for i in catalog['items'] if i['id'] == task['media_id']), None)
            if not item: raise self.Retry(10, '已上架，等待电视片库返回该条目')
            for route in ([item['path']] if item.get('path') else [e['path'] for e in item.get('episodes', [])])[:50]:
                with open(self.media_path(unquote(route)), 'rb') as f:
                    if not f.read(65536): raise self.Stop('电视可见的文件无法读取：' + route)
                playable.append(route)
        final = task['organized'][0]['dest']
        status = '已自动上架，电视片库已返回该条目且文件可读（现场播放另验）' if published and want else '已上架' if published else \
                 '已入库，未上架：' + ('实测画质与要求不符，请人工确认' if task.get('quality_warning') else '未选择上架电视' if task.get('flow_version') == 2 else '自动模式未开启')
        return {'published': published, 'playable_routes': playable, 'final_path': final, 'force_publish': False}, 'ready', \
               '「%s」%s；媒体 ID %s；文件 %s' % (body.get('title', ''), status, task['media_id'], Path(final).name)

    def recover_download_tasks(self):
        if self.automation_enabled(): return
        """Called at service start: persisted tasks resume from their stored state; engines are reconciled by reference."""
        with self.db() as db:
            if any(t['state'] in ACTIVE for t in self.objects(db, 'download_task')): self.start_download_workers()
