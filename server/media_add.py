"""1.1.0 加片：the owner's own resources next to automatic search (owner decision 2026-10-30).

The owner finds a resource elsewhere and pastes its magnet link (or uploads the .torrent). Only the built-in
qBittorrent downloads it, into a folder of its own on the media disk. Meanwhile the title is identified from the
torrent name with the Chinese TMDB search Seerr already provides. A single exact match is accepted; anything less
waits for the owner to pick the title. The title is registered in Radarr/Sonarr unmonitored and without any search,
and when the download completes they import the files the system names for them (hardlink, so the torrent keeps
seeding). Jellyfin fetches posters and the synopsis, and the hotel sync brings the film to 待上架 behind the usual
AAC gate. A series that has no TVDB id (Sonarr cannot manage it) is hardlinked into the TV library directly.
A film identified wrongly is corrected from 待上架 ("改片名") through Jellyfin.

Only this module's own qBittorrent tasks (tagged per task) are touched. Existing library files are never replaced:
a title that already has a file in Radarr/Sonarr is not imported over. Downloaded files are deleted only when the
owner removes a task that was never imported, and only inside that task's own folder."""
import base64
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import threading
import time
import uuid
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from downloads_engines import TAG as QB_TAG, QBittorrent
from downloads_net import NetError, magnet_hash, request, torrent_info
from downloads_parse import cn_number, normalize_title, release_attributes
from media_center import NativeError

VIDEO = {'.mkv', '.mp4', '.avi', '.ts', '.m2ts', '.mov', '.wmv', '.flv', '.rmvb', '.webm', '.mpg', '.mpeg', '.m4v'}
DONE = ('listed', 'downloaded', 'removed')   # 'imported' stays active until the film shows up in 待上架
TAG = 'hotel-manual'
STEP_SECONDS = 10
IDENTIFY_RETRY_MS = 5 * 60 * 1000          # Seerr/TMDB outage: try again later, never spin
IMPORT_TIMEOUT_MS = 30 * 60 * 1000         # Radarr/Sonarr unreachable or busy for longer than this: give up
EPISODES_WAIT_MS = 15 * 60 * 1000          # a series Sonarr has just added gets its episode list from a refresh
QB_BAD_PASSWORD_SECONDS = 3600            # after qBittorrent refused the stored password: one try per hour (qB bans after 5)
LIST_WAIT_MS = 20 * 60 * 1000             # archived but not in 待上架 after this long: tell the owner
POSTER = re.compile(r'/[A-Za-z0-9_-]{6,64}\.(?:jpg|png)')
CJK = '\u3400-\u4dbf\u4e00-\u9fff'
DOMAIN = re.compile(r'(?:https?://)?(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|cc|tv|me|top|xyz|vip|la|cn|co|in|io|info|link|club|site|win|pw|app)(?![a-z])', re.I)
SITE_WORDS = ('电影天堂', '阳光电影', '高清影视', '影视之家', '迅雷', '下载', '发布', '字幕组', '论坛', '分享', '首发', '原创', 'bt')
TAGS = re.compile(r'(?<![a-z0-9])(?:2160p|1080[pi]|720p|480p|4k|uhd|hdr(?:10\+?)?|dovi|bluray|blu-ray|bdrip|brrip|bd|web-?dl|webrip|web|hdtv|hdrip|dvdrip|remux|x264|x265|h\.?264|h\.?265|hevc|avc|aac(?:2\.0)?|ac3|e-?ac3|dts(?:-hd)?|ddp?(?:5\.1|2\.0)?|atmos|truehd|10bit|8bit|60fps|hd|hq|proper|repack|internal)(?![a-z0-9])'
                  r'|国语|国粤|粤语|英语|中字|双字|双语|中英|简繁|简中|繁中|中文字幕|特效字幕|内封|内嵌|官方中字|无水印|高清|超清|蓝光|完整版|未删减|导演剪辑版|国英|多音轨', re.I)
SERIES = re.compile(r'第\s*[0-9一二三四五六七八九十百]+\s*[集话話季]|全\s*\d+\s*集|(?<![a-z0-9])(?:s\d{1,2}(?:e\d{1,3})?|ep?\d{2,3}|season\s*\d+)(?![0-9p])', re.I)
# Extras are recognised by Chinese markers, by an extras folder, or by an English tag at the very end of the name
# (Jellyfin/Plex convention "-trailer"). English words inside a title ("The.Interview.2014") are not extras.
EXTRA_WORDS = re.compile(r'花絮|预告片?|片花')
EXTRA_FOLDERS = {'extras', 'extra', 'featurettes', 'trailers', 'samples', 'sample', 'behind the scenes', 'interviews', 'scenes', 'shorts', '花絮', '特典', '预告'}
EXTRA_SUFFIX = re.compile(r'(?:^|[ ._-])(?:sample|trailer|teaser|featurette|behind[ ._-]?the[ ._-]?scenes|interview|deleted[ ._-]?scenes?|extras?)$', re.I)
EPISODE = [re.compile(r'(?<![a-z0-9])s(\d{1,2})[ ._-]?e(\d{1,3})(?![0-9])', re.I), re.compile(r'season[ ._-]*(\d{1,2})[ ._-]*episode[ ._-]*(\d{1,3})(?![0-9])', re.I),
           re.compile(r'第\s*([0-9一二三四五六七八九十百]+)\s*[集话話]'), re.compile(r'(?<![a-z0-9])(?:ep?|episode)[ ._-]?(\d{1,3})(?![0-9p])', re.I),
           re.compile(r'(?:^|[ ._\-\[【])(\d{1,3})(?:[ ._\]】]|$)')]


def clean_name(name):
    """A release name without site watermarks, brackets of tags and the extension."""
    base = PurePosixPath(str(name)).name
    if PurePosixPath(base).suffix.lower() in VIDEO: base = base[:-len(PurePosixPath(base).suffix)]
    out, leading = [], True
    for part in re.split(r'(\[[^\]]*\]|【[^】]*】|\([^)]*\)|（[^）]*）)', base):
        if not part: continue
        if part[:1] in '[【(（':
            inner = part[1:-1]
            # The first leading group is a site or release group; others only go when they carry watermarks or tags.
            drop = (leading and not ''.join(out).strip()) or DOMAIN.search(inner) or TAGS.search(inner) or any(w in inner.lower() for w in SITE_WORDS)
            leading = False
            if drop: continue
            part = ' ' + inner + ' '
        elif part.strip(): leading = False
        out.append(part)
    text = DOMAIN.sub(' ', ''.join(out))
    for word in SITE_WORDS[:7]: text = text.replace(word, ' ')
    return re.sub(r'\s+', ' ', text).strip(' ._-')


def guess(name, kind='auto'):
    """Queries (Chinese part first, then the Latin part), year and kind, read from a release name.
    kind 'auto' stays 'auto' unless the name carries a series marker: a season pack without one is searched as both."""
    clean = clean_name(name)
    attrs = release_attributes(clean)
    marked = bool(attrs['season'] or attrs['episodes'] or SERIES.search(clean))
    kind = kind if kind in ('movie', 'series') else ('series' if marked else 'auto')
    cuts = [m.start() for m in (TAGS.search(clean), SERIES.search(clean), re.search(r'(?<![0-9])(?:19|20)\d{2}(?![0-9p])', clean)) if m and m.start() > 0]
    head = clean[:min(cuts)] if cuts else clean
    head = re.sub(r'[._]+', ' ', head).strip(' -:：')
    # "X战警：天启": a few Latin letters or digits directly in front belong to the Chinese title.
    cjk = re.search('(?:[A-Za-z0-9]{1,3})?[%s][%s0-9０-９：:·・！!？?，,、 ]*' % (CJK, CJK), head)
    chinese = re.sub(r'[\s：:·・！!？?，,、]+$', '', cjk.group(0)).strip() if cjk else ''
    latin = re.sub('[%s]+' % CJK, ' ', head.replace(cjk.group(0), ' ') if cjk else head)   # "流浪地球2" must not leave its "2"
    latin = re.sub(r"[^A-Za-z0-9'&: -]+", ' ', latin)
    latin = re.sub(r'\s+', ' ', latin).strip(' -:')
    if len(re.sub('[^A-Za-z]', '', latin)) < 3: latin = ''   # "X", "A": too short to trust an exact match
    queries = []
    for q in (chinese, latin):
        if q and q not in queries: queries.append(q[:80])
    return {'queries': queries, 'year': attrs['year'], 'kind': kind, 'season': attrs['season'], 'clean': clean[:200]}


def is_extra(path):
    p = PurePosixPath(str(path))
    if EXTRA_WORDS.search(p.stem) or EXTRA_SUFFIX.search(p.stem): return True
    return any(part.lower() in EXTRA_FOLDERS for part in p.parts[-4:-1])


def episode_of(filename, default_season=1):
    """(season, episode) from a file name when Sonarr could not tell; None when unsure."""
    base = PurePosixPath(filename).name
    for pattern in EPISODE[:2]:
        m = pattern.search(base)
        if m: return int(m.group(1)), int(m.group(2))
    season = default_season
    m = re.search(r'第\s*([0-9一二三四五六七八九十]+)\s*季', base)
    if m: season = cn_number(m.group(1)) or default_season
    for pattern in EPISODE[2:]:
        m = pattern.search(clean_name(base) if pattern is EPISODE[4] else base)
        if m:
            number = cn_number(m.group(1)) if not m.group(1).isdigit() else int(m.group(1))
            if number and 0 < number < 1000 and not (pattern is EPISODE[4] and 1900 <= number): return season, number
    return None


def candidate(raw):
    kind = raw.get('mediaType')
    if kind not in ('movie', 'tv') or not isinstance(raw.get('id'), int): return None
    date = str(raw.get('releaseDate') or raw.get('firstAirDate') or '')
    poster = str(raw.get('posterPath') or '')
    return {'media_type': kind, 'tmdb_id': raw['id'], 'title': str(raw.get('title') or raw.get('name') or '')[:200],
            'original_title': str(raw.get('originalTitle') or raw.get('originalName') or '')[:200],
            'year': int(date[:4]) if re.match(r'\d{4}', date) else None,
            'poster': poster if POSTER.fullmatch(poster) else '', 'overview': str(raw.get('overview') or '')[:240]}


def safe_name(text):
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', ' ', str(text)).strip(' .')
    return re.sub(r'\s+', ' ', text)[:120] or '未命名'


class Retry(Exception):
    """Not a failure yet: the step is tried again later (with a note for the owner)."""


class MediaAdd:
    # ---------- plumbing ----------
    def _media_add_engine(self, config):
        from management import Problem
        try: secret = json.loads((self.data / 'download-secrets.json').read_text())
        except (OSError, ValueError): secret = {}
        password = (secret.get('qbittorrent') or {}).get('password')
        url = (config.get('qbittorrent') or {}).get('url', '')
        parts = urlsplit(url)
        if parts.scheme != 'http' or parts.hostname != '127.0.0.1': raise Problem('内置 qBittorrent 地址无效', 409)
        if not password: raise Problem('后台没有保存 qBittorrent 密码；请先在“密码管理”中设置一次 qBittorrent 密码', 409)
        bad = self.__dict__.get('_media_add_bad_qb')
        if bad and bad[0] == password and time.monotonic() < bad[1]:
            raise Problem('内置 qBittorrent 拒绝了后台保存的密码；请在“密码管理”中重新设置 qBittorrent 密码（为免被封，暂停尝试 1 小时）', 409)
        cached = self.__dict__.get('_media_add_qb')
        if cached and cached[0] == (url, password): return cached[1]   # one login, not one per step
        engine = QBittorrent({'url': url, 'username': 'admin'}, password)
        self.__dict__['_media_add_qb'] = ((url, password), engine)
        return engine

    def _media_add_config(self):
        from management import Problem
        if not self.automation_enabled(): raise Problem('本机未安装片库自动化', 409)
        try: config = self.media_center_config()
        except NativeError as exc: raise Problem(str(exc), 409) from None
        if not config or not config.get('storage_root'): raise Problem('缺少片库自动化私有配置；请用安装包 --resume 补齐', 409)
        return config

    def _media_add_tasks(self, db, active_only=False):
        rows = [json.loads(r[0]) for r in db.execute('SELECT body FROM media_add')]
        if active_only: rows = [t for t in rows if t['stage'] not in DONE]
        return sorted(rows, key=lambda t: t['created_at'], reverse=True)

    def _media_add_get(self, task_id):
        with self.db() as db:
            row = db.execute('SELECT body FROM media_add WHERE id=?', (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def _media_add_save(self, db, task, expect_rev=None):
        """Optimistic: a result computed from an older revision never overwrites a newer one."""
        from management import encoded, now_ms
        if expect_rev is not None:
            row = db.execute('SELECT body FROM media_add WHERE id=?', (task['id'],)).fetchone()
            if not row or json.loads(row[0]).get('rev') != expect_rev: return False
        task['rev'] = task.get('rev', 0) + 1; task['updated_at'] = now_ms()
        db.execute('INSERT OR REPLACE INTO media_add VALUES(?,?)', (task['id'], encoded(task)))
        self.__dict__.pop('_media_center_cache', None)
        return True

    def _media_add_current(self, task):
        """True while nobody changed the task since it was read (checked right before an irreversible call)."""
        latest = self._media_add_get(task['id'])
        return bool(latest) and latest.get('rev') == task.get('rev')

    @staticmethod
    def _media_add_view(task):
        keys = ('id', 'name', 'kind', 'stage', 'match', 'candidates', 'download', 'error', 'notes', 'created_at', 'updated_at', 'unmapped', 'file_candidates', 'published', 'selection_needed')
        return {**{k: task.get(k) for k in keys}, 'query': ((task.get('guess') or {}).get('queries') or [''])[0]}

    # ---------- read ----------
    def media_add_list(self):
        with self.db() as db:
            tasks = self._media_add_tasks(db)
            states = {r['id']: bool(r['published']) for r in db.execute('SELECT id,published FROM media')}
        for task in tasks: task['published'] = states.get(task.get('media_id'), False)
        visible = [self._media_add_view(t) for t in tasks if t['stage'] != 'removed'][:100]
        return {'enabled': self.automation_enabled(), 'tasks': visible,
                'attention': {'needs_match': sum(t['stage'] == 'needs_match' for t in tasks), 'failed': sum(t['stage'] == 'failed' for t in tasks)}}

    def media_add_search(self, query, kind='auto'):
        from management import Problem
        query = str(query or '').strip()[:80]
        if not query: raise Problem('请输入片名')
        config = self._media_add_config()
        try: return {'results': self._tmdb_search(config, query, kind)[:12]}
        except NativeError as exc: raise Problem('影片资料库暂时无法搜索：' + str(exc), 502) from None

    def _tmdb_search(self, config, query, kind='auto'):
        payload = self._native(config, 'seerr', '/api/v1/search?' + urlencode({'query': query, 'page': 1, 'language': 'zh-CN'}, quote_via=quote), timeout=15) or {}
        want = {'movie': 'movie', 'series': 'tv'}.get(kind)
        out = []
        for raw in payload.get('results') or []:
            c = candidate(raw) if isinstance(raw, dict) else None
            if c and (not want or c['media_type'] == want): out.append(c)
        return out

    def media_add_poster(self, path):
        """TMDB poster thumbnails for the picker, fetched by the server (the owner's browser may not reach TMDB)."""
        from management import Problem
        if not POSTER.fullmatch(str(path or '')): raise Problem('海报地址无效', 404)
        cache = self.data / 'tmdb-posters'; cache.mkdir(exist_ok=True)
        dest = cache / (hashlib.sha256(path.encode()).hexdigest()[:32] + PurePosixPath(path).suffix)
        kind = 'image/png' if dest.suffix == '.png' else 'image/jpeg'
        try: return dest.read_bytes(), kind
        except OSError: pass
        try: status, headers, body, _ = request('https://image.tmdb.org/t/p/w185' + path, timeout=10, limit=2 * 1024 * 1024)
        except NetError: raise Problem('海报暂时无法加载', 502) from None
        if status != 200 or headers.get('content-type', '').split(';')[0] not in ('image/jpeg', 'image/png'): raise Problem('海报暂时无法加载', 502)
        try:
            files = sorted((f for f in cache.iterdir() if not f.name.startswith('.')), key=lambda p: p.stat().st_mtime)
            for old in files[:max(0, len(files) - 3000)]: old.unlink(missing_ok=True)
        except OSError: pass   # a concurrent request evicted the same file
        temp = cache / ('.part-' + uuid.uuid4().hex); temp.write_bytes(body); temp.replace(dest)
        return body, headers['content-type'].split(';')[0]

    # ---------- owner actions (admin only) ----------
    def _media_add_task_lock(self, task_id):
        locks = self.__dict__.setdefault('_media_add_task_locks', {})
        return locks.setdefault(task_id, threading.RLock())

    def media_add_action(self, body, actor):
        from management import Problem
        if body.get('action') == 'add':
            # qB and our database form one idempotent submission boundary.
            with self._media_add_task_lock('__new__'): return self._media_add_action(body, actor)
        # Revision checks protect database writes, but cannot undo a native import or qB file deletion.
        # Serialize those calls with owner edits for this task only; other tasks can still make progress.
        lock = self._media_add_task_lock(str(body.get('id') or ''))
        if not lock.acquire(blocking=False): raise Problem('这个任务正在入库，请稍后再操作', 409)
        try: return self._media_add_action(body, actor)
        finally: lock.release()

    def _media_add_action(self, body, actor):
        from management import Problem
        action = body.get('action')
        if action not in ('add', 'match', 'retry', 'remove', 'select_files', 'number_episodes'): raise Problem('操作无效')
        config = self._media_add_config()
        if action == 'add': return self._media_add_new(config, body, actor)
        task = self._media_add_get(str(body.get('id') or ''))
        if not task: raise Problem('加片任务不存在', 404)
        if action in ('select_files', 'number_episodes'):
            return self._media_add_select(task, body, actor)
        # Network first, outside any database transaction. The owner's change is then applied to the latest row,
        # so download progress saved meanwhile never makes the owner repeat the action.
        try:
            if action == 'match': change, message = self._media_add_match(config, task, body)
            elif action == 'retry':
                if task['stage'] != 'failed': raise Problem('只有失败的任务可以重试')
                if (task.get('download') or {}).get('state') == 'missing':
                    raise Problem('下载器已无此任务，请重新贴原链接或种子；重试不会伪报开始下载')
                change, message = None, '已重新排队'
            else: change, message = None, self._media_add_remove(config, task)
        except NativeError as exc:
            raise Problem(str(exc), 502) from None
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM media_add WHERE id=?', (task['id'],)).fetchone()
            if not row: raise Problem('加片任务不存在', 404)
            latest = json.loads(row[0])
            if action == 'match':
                if latest['stage'] in ('importing', 'imported', 'listed', 'removed', 'downloaded'): raise Problem('这个任务刚刚已入库或被移除，请刷新后再看')
                latest.update(change)
            elif action == 'retry':
                if latest['stage'] != 'failed': raise Problem('只有失败的任务可以重试')
                latest['stage'] = 'file' if latest.get('kind') == 'file' else ('matched' if latest.get('match') else ('identifying' if latest.get('name') else 'waiting_name'))
                latest.pop('file_candidates', None)
                latest.update(error='', identify_after=0, import_started=None, episodes_since=None, wait_since=None, refreshed=False, arr={})
            else:
                latest['files_kept'] = latest.get('files_kept', False) or latest['stage'] in ('downloaded', 'imported', 'listed')
                latest['stage'] = 'removed'
            self._media_add_save(db, latest)
            self.audit(db, actor, {'match': '确认加片片名', 'retry': '重试加片', 'remove': '移除加片任务'}[action], latest['name'][:120])
        return {'message': message, **self.media_add_list()}

    def _media_add_remove(self, config, task):
        from management import Problem
        if task['stage'] == 'removed' or task.get('files_kept'):
            return '已从列表移除；本次没有删除文件'
        if task['stage'] == 'importing': raise Problem('正在入库，请稍后再移除')
        if task['stage'] in ('imported', 'listed'):
            return '已从列表移除；影片仍在片库中，下载器继续做种'   # list only
        if task['stage'] == 'downloaded':
            return '已从列表移除；下载的文件仍保留在服务器上'
        engine = self._media_add_engine(config)
        try:
            engine._ensure()
            info = engine._info(task['hash'])
            if not info:
                return '已从列表移除；下载器中已无此任务，未删除文件，请在文件中心核对残留目录'
            if info:
                engine._require_owned(info)
                if '%s-%s' % (QB_TAG, task['id']) not in engine._tags(info):
                    return '已从列表移除；这个下载现在属于别的任务，没有删除它的文件'
                # Never imported: nothing in the library uses these files; they live in this task's own folder.
                engine._call('/api/v2/torrents/delete', {'hashes': task['hash'], 'deleteFiles': 'true'})
        except NetError as exc:
            raise Problem('qBittorrent 暂时无法操作，任务未移除：' + str(exc)[:80], 502) from None
        return '已移除，并删除了还没入库的下载文件'

    def _media_add_new(self, config, body, actor):
        from management import Problem, now_ms
        kind = body.get('kind', 'auto')
        if kind not in ('auto', 'movie', 'series', 'file'): raise Problem('类型无效')
        if body.get('torrent_b64'):
            try: data = base64.b64decode(str(body['torrent_b64']), validate=True)
            except ValueError: raise Problem('种子文件内容无效') from None
            if len(data) > 700 * 1024: raise Problem('种子文件过大')
            try: info = torrent_info(data)
            except Exception: raise Problem('不是有效的种子文件') from None   # malformed bencode raises many kinds of errors
            source = {'kind': 'torrent', 'infohash': info['infohash'], 'data': data}; name = info['name']
            if kind == 'auto' and not any(PurePosixPath(f['path']).suffix.lower() in VIDEO for f in info['files']): kind = 'file'
        else:
            link = str(body.get('link') or '').strip()
            if link.lower().startswith('ed2k://'): raise Problem('只支持磁力链接和种子文件；ed2k 链接需要电驴下载器，本系统不支持')
            if not link.startswith('magnet:?'): raise Problem('只支持磁力链接（magnet: 开头）和种子文件')
            if re.search(r'[\r\n]', link) or link.lower().count('magnet:') != 1 or link.lower().count('xt=urn:btih:') != 1:
                raise Problem('一次只能贴一个磁力链接；有多个请分开添加')
            link = re.sub(r'[ \t]', '%20', link)   # some sites show the name part decoded
            h = magnet_hash(link)
            if not h: raise Problem('磁力链接缺少有效的 btih')
            source = {'kind': 'magnet', 'uri': link[:4000], 'infohash': h}
            name = (parse_qs(urlsplit(link).query).get('dn') or [''])[0]
        name = re.sub(r'[\x00-\x1f]+', ' ', str(name)).strip()[:300]
        with self.db() as db:
            existing = next((t for t in self._media_add_tasks(db) if t['hash'] == source['infohash'] and t['stage'] != 'removed'), None)
        if existing and existing['stage'] != 'failed': return {'message': '这个资源已在加片列表中', **self.media_add_list()}
        if existing:   # pasted again after it failed (e.g. deleted in qB): start the same task over
            try: self._media_add_engine(config).add(existing['id'], source, existing['dir'])
            except NetError as exc: raise Problem('qBittorrent 未接受任务：' + str(exc), 502) from None
            with self.db() as db:
                latest = json.loads(db.execute('SELECT body FROM media_add WHERE id=?', (existing['id'],)).fetchone()[0])
                latest.update(stage='file' if latest['kind'] == 'file' else ('matched' if latest.get('match') else ('identifying' if latest.get('name') else 'waiting_name')),
                              error='', identify_after=0, arr={}, wait_since=None, episodes_since=None, download={'state': 'queued', 'progress': 0})
                self._media_add_save(db, latest)
                self.audit(db, actor, '重新开始加片', (latest['name'] or source['infohash'])[:120])
            return {'message': '已重新开始下载', **self.media_add_list()}
        self._media_add_space(config, sum(f['size'] for f in info['files']) if source['kind']=='torrent' else 0)
        task_id = 'add-' + uuid.uuid4().hex[:12]
        directory = str(Path(config['storage_root']) / 'downloads' / 'manual' / task_id)
        task = {'id': task_id, 'hash': source['infohash'], 'name': name, 'kind': kind, 'dir': directory,
                'stage': 'submitting', 'match': None, 'candidates': [], 'download': {'state': 'queued', 'progress': 0},
                'error': '', 'notes': [], 'created_at': now_ms(), 'actor': actor, 'identify_after': 0, 'arr': {}}
        with self.db() as db: self._media_add_save(db, task)
        try: self._media_add_engine(config).add(task_id, source, directory)
        except NetError as exc:
            task.update(stage='failed',error='下载器未接受资源：'+str(exc)[:200])
            with self.db() as db: self._media_add_save(db, task)
            if exc.kind == 'conflict': raise Problem('这个资源已经在 qBittorrent 里（由自动搜片或其他任务下载），不用重复添加', 409) from None
            raise Problem('qBittorrent 未接受任务：' + str(exc), 502) from None
        task['stage'] = 'file' if kind == 'file' else ('identifying' if name else 'waiting_name')
        with self.db() as db:
            self._media_add_save(db, task)
            self.audit(db, actor, '贴链接加片', (name or source['infohash'])[:120])
        if name and kind != 'file':
            try: self._media_add_identify(config, task['id'])   # a quick answer for the owner; the scheduler retries
            except Exception as exc: print('Media add identification will retry:', type(exc).__name__, flush=True)
        message = '已开始下载' + ('：' + name[:60] if name else '，正在获取资源信息') + ('（普通文件，只下载、不入库）' if kind == 'file' else '')
        return {'message': message, **self.media_add_list()}

    def _media_add_match(self, config, task, body):
        from management import Problem
        if task['stage'] in ('importing', 'imported', 'listed'): raise Problem('已经入库，请到待上架里点“改片名”')
        if task['stage'] in ('removed', 'downloaded'): raise Problem('任务已结束')
        media_type = body.get('media_type')
        try: tmdb_id = int(body.get('tmdb_id'))
        except (TypeError, ValueError): raise Problem('请选择一部影片') from None
        if media_type not in ('movie', 'tv') or tmdb_id <= 0: raise Problem('请选择一部影片')
        detail = self._native(config, 'seerr', '/api/v1/%s/%d?language=zh-CN' % (media_type, tmdb_id), timeout=15) or {}
        c = candidate({**detail, 'mediaType': media_type, 'id': tmdb_id}) or {}
        change = {'match': {**c, 'how': 'owner', 'tvdb_id': (detail.get('externalIds') or {}).get('tvdbId')}, 'candidates': [], 'stage': 'matched',
                  'kind': 'series' if media_type == 'tv' else 'movie', 'error': '', 'arr': {}, 'import_started': None, 'episodes_since': None, 'wait_since': None}
        return change, '已确认：' + (c.get('title') or str(tmdb_id)) + ('，下载完成后自动入库' if not task['download'].get('complete') else '，正在入库')

    # ---------- identification ----------
    def _media_add_identify(self, config, task_id):
        from management import now_ms
        task = self._media_add_get(task_id)
        if not task: return
        rev = task.get('rev')
        if task['stage'] != 'identifying' or task.get('identify_after', 0) > now_ms(): return
        g = guess(task['name'], task['kind'])
        found, pool = None, []
        try:
            for q in g['queries']:
                results = self._tmdb_search(config, q, g['kind'])
                if results and not pool: pool = results
                key = normalize_title(q)
                exact = [c for c in results if key in (normalize_title(c['title']), normalize_title(c['original_title']))]
                if g['year']:
                    close = [c for c in exact if c['year'] and abs(c['year'] - g['year']) <= 1]
                    same = [c for c in close if c['year'] == g['year']]
                    # Series packs often carry a later season's year; movies must agree within a year.
                    exact = same if len(same) == 1 else (close if close else ([c for c in exact if c['media_type'] == 'tv'] if g['kind'] == 'series' else []))
                if len(exact) == 1: found = exact[0]; break
                if exact and not pool: pool = exact
        except NativeError as exc:
            task.update(error='暂时连不上影片资料库，5 分钟后自动重试（' + str(exc)[:80] + '）', identify_after=now_ms() + IDENTIFY_RETRY_MS)
            with self.db() as db: self._media_add_save(db, task, rev)
            return
        if found:
            task.update(match={**found, 'how': 'auto'}, stage='matched', candidates=[], error='')
        else:
            task.update(stage='needs_match', candidates=pool[:8], error='没有把握认出这部片，请选择正确的片名' if g['queries'] else '名称里没有可识别的片名，请搜索选择')
        task['guess'] = g
        with self.db() as db: self._media_add_save(db, task, rev)

    # ---------- background (own thread, started with the automation scheduler) ----------
    def run_media_add_scheduler(self):
        def loop():
            while True:
                try: self.media_add_step()
                except Exception as exc: print('Media add will retry:', type(exc).__name__, flush=True)
                time.sleep(STEP_SECONDS)
        threading.Thread(target=loop, name='media-add', daemon=True).start()

    def media_add_step(self):
        if not self.automation_enabled(): return
        try: config = self.media_center_config()
        except NativeError: return
        if not config: return
        if config.get('storage_guard'): self._native_storage_guard(config)
        with self.db() as db: tasks = self._media_add_tasks(db, active_only=True)
        if not tasks: return
        from management import Problem
        engine = None
        for task in reversed(tasks):   # oldest first
            try:
                if task['stage'] == 'submitting':
                    engine = engine or self._media_add_engine(config)
                    if engine._info(task['hash']):
                        task['stage']='file' if task['kind']=='file' else ('identifying' if task.get('name') else 'waiting_name')
                        with self.db() as db: self._media_add_save(db,task)
                    elif __import__('management').now_ms()-task['created_at']>300000:
                        task.update(stage='failed',error='提交中断且下载器没有任务，请重新贴链接')
                        with self.db() as db: self._media_add_save(db,task)
                    continue
                if task['stage'] == 'imported':   # archived: only waits for the hotel sync, qBittorrent is not needed
                    self._media_add_listing(task); continue
                engine = engine or self._media_add_engine(config)
                self._media_add_advance(config, engine, task)
            except Problem as exc:   # qBittorrent not usable (no or refused password): say so on the waiting tasks
                for t in tasks:
                    if t['stage'] not in ('importing', 'failed', 'imported') and t.get('error') != str(exc):
                        t['error'] = str(exc)
                        with self.db() as db: self._media_add_save(db, t, t.get('rev'))
                return
            except Exception as exc:
                print('Media add will retry:', type(exc).__name__, flush=True)

    def _media_add_advance(self, config, engine, task):
        from management import now_ms
        rev = task.get('rev')
        before = json.dumps([task.get(k) for k in ('stage', 'name', 'download', 'error', 'kind', 'progress_at', 'last_downloaded')], sort_keys=True)
        try:
            status = engine.status(task['hash'])
            info = engine._info(task['hash']) or {} if status['state'] != 'missing' else {}
        except NetError as exc:
            if exc.kind == 'auth':   # repeated wrong logins get the server's address banned by qBittorrent
                self.__dict__['_media_add_bad_qb'] = (engine.password, time.monotonic() + QB_BAD_PASSWORD_SECONDS)
                self.__dict__.pop('_media_add_qb', None)
            if task['stage'] not in ('importing', 'failed'):
                task['error'] = '暂时连不上内置 qBittorrent：' + str(exc)[:80]
                with self.db() as db: self._media_add_save(db, task, rev)
            return
        download = {'state': status['state'], 'complete': status['state'] == 'complete',
                    'progress': 1 if status['state'] == 'complete' else (round(status['downloaded'] / status['total'], 4) if status.get('total') else 0),
                    'size': status.get('total'), 'speed': status.get('speed', 0), 'eta': status.get('eta'), 'seeds': status.get('seeds')}
        if task.get('kind') != 'file' and not download['complete'] and status['state'] not in ('error','missing','metadata','checking','moving'):
            try:
                entries=json.loads(engine._call('/api/v2/torrents/files?' + urlencode({'hash':task['hash']})) or b'[]')
                videos=[f for f in entries if PurePosixPath(str(f.get('name',''))).suffix.lower() in VIDEO]
                if videos and all(f.get('progress',0)>=1 for f in videos):
                    download.update(complete=True, progress=1)
            except (NetError, ValueError): pass
        task['download'] = download
        progress=int(status.get('downloaded') or 0)
        if progress>task.get('last_downloaded',0):
            task.update(last_downloaded=progress,progress_at=now_ms())
        elif not task.get('progress_at'):task['progress_at']=task.get('created_at',now_ms())
        if task.get('error', '').startswith(('暂时连不上内置 qBittorrent', '内置 qBittorrent 拒绝')): task['error'] = ''
        if status['state'] == 'missing' and task['stage'] not in ('importing', 'failed', 'imported'):
            task.update(stage='failed', error='qBittorrent 里已找不到这个任务（可能在 qB 中被删除），请重新贴链接')
        elif status['state'] == 'error' and task['stage'] != 'failed':
            task['error'] = 'qBittorrent 报告下载错误，常见原因是磁盘空间不足或保存目录不可写'
        if task['stage'] == 'waiting_name' and info.get('name') and status['state'] != 'metadata' and info['name'].lower() != task['hash']:
            task.update(name=re.sub(r'[\x00-\x1f]+', ' ', str(info['name'])).strip()[:300], stage='identifying')
        if not task.get('files_checked') and task['kind'] == 'auto' and task['stage'] in ('identifying', 'needs_match', 'matched') and status['state'] not in ('metadata', 'missing'):
            # 普通文件只下载: a torrent without any video (software, documents) is never identified or imported.
            try: files = json.loads(engine._call('/api/v2/torrents/files?' + urlencode({'hash': task['hash']})) or b'[]')
            except (NetError, ValueError): files = None
            if files:
                task['files_checked'] = True
                if not any(PurePosixPath(str(f.get('name', ''))).suffix.lower() in VIDEO for f in files):
                    task.update(kind='file', stage='file', candidates=[], match=None, error='')
        if task['stage'] == 'file' and download['complete']:
            task.update(stage='downloaded', notes=['已下载完成；不是影视文件，只下载、不入库'])
        if json.dumps([task.get(k) for k in ('stage', 'name', 'download', 'error', 'kind', 'progress_at', 'last_downloaded')], sort_keys=True) != before:
            with self.db() as db:
                if not self._media_add_save(db, task, rev): return
        if task['stage'] == 'identifying': return self._media_add_identify(config, task['id'])
        if task['stage'] == 'matched' and download['complete']: return self._media_add_run(self._media_add_import, config, task)
        if task['stage'] == 'importing': return self._media_add_run(self._media_add_check, config, task)

    def _media_add_run(self, step, config, task):
        lock = self._media_add_task_lock(task['id'])
        if not lock.acquire(blocking=False): return
        try:
            title=(task.get('match') or {})
            with self._media_add_task_lock('title:'+str(title.get('media_type'))+':'+str(title.get('tmdb_id'))):
                if self._media_add_current(task): self._media_add_run_locked(step, config, task)
        finally: lock.release()

    def _media_add_run_locked(self, step, config, task):
        """Runs an import step; an unreachable app is retried for a while instead of failing the task."""
        from management import now_ms
        rev = task.get('rev')
        try:
            step(config, task)
            task['wait_since'] = None
            rev=task.get('rev')
        except Retry as wait:
            started = task.get('wait_since') or now_ms()
            if now_ms() - started > IMPORT_TIMEOUT_MS:
                task.update(stage='failed', error='入库失败：' + (str(wait) or '等待超时')[:200], wait_since=None)
            else: task.update(error=str(wait)[:200], wait_since=started)
        except NativeError as exc:
            if '无法连接' in str(exc) or re.search(r'返回 5\d\d', str(exc)):   # unreachable or still starting
                started = task.get('wait_since') or now_ms()
                if now_ms() - started > IMPORT_TIMEOUT_MS: task.update(stage='failed', error='入库失败：' + str(exc)[:200], wait_since=None)
                else: task.update(error=str(exc)[:120] + '，稍后自动重试', wait_since=started)
            else: task.update(stage='failed', error='入库失败：' + str(exc)[:200], wait_since=None)
        except OSError as exc:   # the download folder is not readable by the hotel service, or the disk failed
            task.update(stage='failed', error='读取或链接下载文件失败（%s），请检查数据盘权限' % type(exc).__name__, wait_since=None)
        rev=task.get('rev',rev)
        with self.db() as db: saved = self._media_add_save(db, task, rev)
        if saved and task['stage'] == 'imported':
            for delay in (30, 90): threading.Timer(delay, self.sync_wakeup.set).start()

    def _native_storage_guard(self, config):
        """One bounded check for both native search and manual downloads; never touches other torrents."""
        from management import Problem, now_ms
        import shutil
        root=Path(config['storage_root'])
        with self.db() as db:
            settings=self.download_settings(db)
            row=db.execute("SELECT value FROM kv WHERE key='media_storage_guard'").fetchone()
            previous=json.loads(row[0]) if row else {}
        reserve=max(5,settings.get('min_free_gb',5))*1024**3
        try:
            usable=root.is_dir() and (not config.get('require_storage_marker') or (root/'.hotel-media-install.json').is_file()) and shutil.disk_usage(root).free>=reserve
            engine=self._media_add_engine(config);engine._ensure()
            torrents=json.loads(engine._call('/api/v2/torrents/info') or b'[]')
            paused=set(previous.get('paused',[]));live=set();misplaced_count=0
            for torrent in torrents:
                ident=torrent.get('hash','')
                if not re.fullmatch(r'[a-f0-9]{40}',ident):continue
                directory=Path(torrent.get('save_path','/nonexistent')).resolve()
                misplaced=root.resolve() not in (directory,*directory.parents)
                if torrent.get('category') not in ('radarr','sonarr','hoteltv') and QB_TAG not in engine._tags(torrent):continue
                misplaced_count+=int(misplaced)
                live.add(ident)
                if (not usable or misplaced) and torrent.get('state') in ('downloading','forcedDL','stalledDL','metaDL','queuedDL'):
                    engine._call('/api/v2/torrents/'+('stop' if engine.api and engine.api>=(2,11) else 'pause'),{'hashes':ident});paused.add(ident)
                elif usable and not misplaced and ident in paused:
                    engine._call('/api/v2/torrents/'+('start' if engine.api and engine.api>=(2,11) else 'resume'),{'hashes':ident});paused.remove(ident)
            state={'low':not usable,'paused':sorted(paused&live),'at':now_ms(),'reserve_gb':reserve//1024**3,'misplaced':misplaced_count}
            if misplaced_count:state['error']='部分下载目录不在配置的数据盘内，已暂停；请在原生下载器纠正保存目录后再检查'
        except NetError as exc:
            if exc.kind=='auth':
                self.__dict__['_media_add_bad_qb']=(engine.password,time.monotonic()+QB_BAD_PASSWORD_SECONDS)
            state={**previous,'error':'下载器连接未完成，请在密码管理核对 qBittorrent 连接','at':now_ms()}
        except (OSError,Problem):
            state={**previous,'low':True,'error':'数据盘未就绪或下载器凭据不可用，请检查挂载和权限','at':now_ms()}
        with self.db() as db:self.put_kv(db,'media_storage_guard',state)

    def _media_add_space(self, config, size=0):
        from management import Problem
        import shutil
        root = Path(config['storage_root'])
        if config.get('require_storage_marker') and not (root/'.hotel-media-install.json').is_file():
            raise Problem('数据盘挂载标记缺失，已阻止下载；请核对数据盘后重试',409)
        probe = root
        while not probe.exists() and probe.parent != probe: probe = probe.parent
        free = shutil.disk_usage(probe).free
        with self.db() as db:
            settings = self.download_settings(db)
        reserve = max(1, settings.get('min_free_gb', 5)) * 1024 ** 3
        if free < max(0, size) + reserve:
            raise Problem('数据盘剩余空间不足；保留至少 %s GB，请先清理或更换数据盘' % int(reserve/1024**3), 409)

    def _media_add_select(self, task, body, actor):
        from management import Problem
        choices = task.get('file_candidates') or []
        if task['stage'] not in ('needs_file', 'needs_episodes') or not choices:
            raise Problem('文件列表已变化，请刷新后再选', 409)
        if body['action'] == 'select_files':
            selected = body.get('file')
            if selected not in {c['path'] for c in choices}: raise Problem('请选择列表中的正片文件')
            task['selected_file'] = selected
        else:
            season, start = body.get('season'), body.get('start')
            if type(season) is not int or type(start) is not int or not 1<=season<=999 or not 1<=start<=9999:
                raise Problem('请输入有效的季号和起始集号')
            task['episode_mapping'] = {c['path']:[season,start+i] for i,c in enumerate(choices)}
        task.update(stage='matched', error='', wait_since=None)
        with self.db() as db:
            self._media_add_save(db, task)
            self.audit(db, actor, '确认加片文件/集号', task['id'])
        return {'message':'已确认，按所选文件继续入库', **self.media_add_list()}

    # ---------- Radarr/Sonarr helpers ----------
    def _arr_tag(self, config, app):
        tags = self._native(config, app, '/api/v3/tag') or []
        tag = next((t for t in tags if t.get('label') == TAG), None)
        return (tag or self._native(config, app, '/api/v3/tag', {'label': TAG}, 'POST'))['id']

    def _library_root(self, config, kind):
        return Path(config['storage_root']) / 'media' / ('电影' if kind == 'movie' else '电视剧')

    def _arr_root(self, config, app):
        wanted = str(self._library_root(config, 'movie' if app == 'radarr' else 'tv'))
        roots = [r.get('path', '').rstrip('/') for r in self._native(config, app, '/api/v3/rootfolder') or []]
        if wanted in roots: return wanted
        if roots: return roots[0]
        raise NativeError(app + ' 没有片库根目录')

    def _arr_profile(self, config, app):
        if (config.get(app) or {}).get('profile'): return int(config[app]['profile'])
        profiles = self._native(config, app, '/api/v3/qualityprofile') or []
        if not profiles: raise NativeError(app + ' 没有质量配置')
        return next((p['id'] for p in profiles if p.get('name') == 'HD-1080p'), profiles[0]['id'])

    def _arr_movie(self, config, match):
        """Radarr's movie for the title: reused when it exists, else added unmonitored and without any search."""
        existing = self._native(config, 'radarr', '/api/v3/movie?tmdbId=%d' % match['tmdb_id']) or []
        if existing: return existing[0]
        movie = self._native(config, 'radarr', '/api/v3/movie/lookup/tmdb?tmdbId=%d' % match['tmdb_id'], timeout=20)
        movie.update(qualityProfileId=self._arr_profile(config, 'radarr'), rootFolderPath=self._arr_root(config, 'radarr'), monitored=False,
                     minimumAvailability='released', addOptions={'searchForMovie': False}, tags=[self._arr_tag(config, 'radarr')])
        return self._native(config, 'radarr', '/api/v3/movie', movie, 'POST', timeout=30)

    def _tvdb_id(self, config, match):
        if match.get('tvdb_id'): return int(match['tvdb_id'])
        detail = self._native(config, 'seerr', '/api/v1/tv/%d' % match['tmdb_id'], timeout=15) or {}
        tvdb = (detail.get('externalIds') or {}).get('tvdbId')
        return int(tvdb) if tvdb else None

    def _arr_series(self, config, tvdb):
        existing = self._native(config, 'sonarr', '/api/v3/series?tvdbId=%d' % tvdb) or []
        if existing: return existing[0]
        found = self._native(config, 'sonarr', '/api/v3/series/lookup?term=' + quote('tvdb:%d' % tvdb), timeout=20) or []
        if not found: raise NativeError('Sonarr 查不到这部剧')
        series = found[0]
        for season in series.get('seasons') or []: season['monitored'] = False
        series.update(qualityProfileId=self._arr_profile(config, 'sonarr'), rootFolderPath=self._arr_root(config, 'sonarr'), monitored=False, seasonFolder=True,
                      addOptions={'monitor': 'none', 'searchForMissingEpisodes': False, 'searchForCutoffUnmetEpisodes': False}, tags=[self._arr_tag(config, 'sonarr')])
        return self._native(config, 'sonarr', '/api/v3/series', series, 'POST', timeout=30)

    def _confirmed_non_video(self, config, task):
        try:
            entries=json.loads(self._media_add_engine(config)._call('/api/v2/torrents/files?' + urlencode({'hash':task['hash']})) or b'[]')
            return bool(entries) and not any(PurePosixPath(str(f.get('name',''))).suffix.lower() in VIDEO for f in entries)
        except (NetError,ValueError): return False

    def _videos(self, config, app, task):
        """Video files of this task's folder as Radarr/Sonarr see them. Folder mode only: with a movie or series id
        both apps would list that title's library folder instead."""
        items = self._native(config, app, '/api/v3/manualimport?' + urlencode({'folder': task['dir'], 'filterExistingFiles': 'true'}), timeout=120) or []
        return [i for i in items if PurePosixPath(i.get('path', '')).suffix.lower() in VIDEO and Path(i['path']).parent.is_relative_to(Path(task['dir']))]

    @staticmethod
    def _only_files(task):
        task.update(stage='downloaded', error='', notes=['下载内容里没有视频文件；按普通文件处理，只下载、不入库'])

    def _send_import(self, config, app, task, files):
        if not self._media_add_current(task): raise Retry('任务刚刚被修改，按最新设置重新入库')
        return self._native(config, app, '/api/v3/command', {'name': 'ManualImport', 'importMode': 'copy', 'files': files}, 'POST', timeout=30)['id']

    # ---------- import ----------
    def _media_add_import(self, config, task):
        from management import now_ms
        match = task['match']
        if match['media_type'] == 'movie':
            movie = self._arr_movie(config, match)
            if movie.get('hasFile'):
                task.update(stage='failed', error='片库里已经有《%s》的文件，为避免覆盖没有自动入库；需要替换时请在 Radarr 中手动导入' % (match.get('title') or movie.get('title', ''))[:60])
                return
            found = self._videos(config, 'radarr', task)
            if not found:
                if self._confirmed_non_video(config,task):return self._only_files(task)
                raise Retry('下载目录暂不可读或原生扫描未找到文件；请检查数据盘挂载、权限后重试')
            videos = [i for i in found if not is_extra(i['path'])] or found   # a film whose own name ends in "Interview"
            selection = task.get('selected_file')
            relative = lambda i: str(Path(i['path']).relative_to(task['dir']))
            if selection:
                best = next((i for i in videos if relative(i)==selection), None)
                if best is None: raise NativeError('已选文件不在当前下载中，请重新选择')
            elif len(videos) != 1:
                task.update(stage='needs_file', file_candidates=[{'path':relative(i),'size':i.get('size',0)} for i in sorted(videos,key=lambda i:i['path'])], error='包含多个视频，请确认正片版本；系统不会按体积猜选')
                return
            else: best = videos[0]
            if not best.get('quality'):
                raise NativeError('原生归档器未能解析所选视频质量，请使用 Radarr 交互导入确认，未自动导入')
            with self.db() as db:
                other=next((t for t in self._media_add_tasks(db) if t['id']!=task['id'] and t['stage']=='importing' and (t.get('arr') or {}).get('app')=='radarr' and (t.get('arr') or {}).get('id')==movie['id']),None)
            if other: raise Retry('同一影片的另一个版本正在入库；本任务等待核验，避免覆盖')
            files = [{'path': best['path'], 'movieId': movie['id'], 'quality': best.get('quality'), 'languages': best.get('languages') or [], 'releaseGroup': best.get('releaseGroup') or ''}]
            task['arr']={'app':'radarr','id':movie['id'],'path':movie.get('path')}
            with self.db() as db: self._media_add_save(db,task)
            command = self._send_import(config, 'radarr', task, files)
            task.update(stage='importing', arr={'app': 'radarr', 'id': movie['id'], 'command': command, 'path': movie.get('path')}, unmapped=[], import_started=now_ms(), error='')
            return
        tvdb = self._tvdb_id(config, match)
        if not tvdb: return self._media_add_link_series(config, task)
        series = self._arr_series(config, tvdb)
        episodes = self._native(config, 'sonarr', '/api/v3/episode?seriesId=%d' % series['id'], timeout=20) or []
        if not episodes:   # a series Sonarr has just added gets its episodes from the refresh it queued
            task['episodes_since'] = task.get('episodes_since') or now_ms()
            if now_ms() - task['episodes_since'] > EPISODES_WAIT_MS: raise NativeError('Sonarr 一直没有这部剧的分集信息')
            task['error'] = '等待 Sonarr 获取分集信息'
            return
        by_number = {(e.get('seasonNumber'), e.get('episodeNumber')): e for e in episodes}
        default_season = (task.get('guess') or {}).get('season') or 1
        targets, claims, unmapped, existing = {}, {}, [], []
        found = self._videos(config, 'sonarr', task)
        if not found: raise Retry('下载目录暂不可读或原生扫描未找到分集；请检查数据盘挂载、权限后重试')
        for item in (i for i in found if not is_extra(i['path'])):
            # Sonarr may have matched the file to another series in folder mode: only its numbers are used.
            numbers = [(e.get('seasonNumber'), e.get('episodeNumber')) for e in item.get('episodes') or [] if isinstance(e, dict)]
            mapped=(task.get('episode_mapping') or {}).get(str(Path(item['path']).relative_to(task['dir'])))
            if mapped: numbers=[tuple(mapped)]
            if not numbers:
                guessed = episode_of(item['path'], default_season)
                numbers = [guessed] if guessed else []
            found = [by_number[n] for n in numbers if n in by_number]
            name = PurePosixPath(item['path']).name[:160]
            if not found or len(found) != len(numbers): unmapped.append(name); continue
            if any(e.get('hasFile') for e in found): existing.append(name); continue
            targets[item['path']] = (item, found)
            for e in found: claims.setdefault(e['id'], []).append(item['path'])
        files = []
        for path, (item, found) in targets.items():
            # Two files for one episode (a dubbed version, a mislabeled extra): report, never let one replace the other.
            if any(len(claims[e['id']]) > 1 for e in found): unmapped.append(PurePosixPath(path).name[:160]); continue
            files.append({'path': path, 'seriesId': series['id'], 'seasonNumber': min(e['seasonNumber'] for e in found), 'episodeIds': [e['id'] for e in found],
                          'quality': item.get('quality'), 'languages': item.get('languages') or [], 'releaseGroup': item.get('releaseGroup') or ''})
        if not files and unmapped and not task.get('refreshed'):
            # An existing series may not know a newly aired season yet: refresh it once and try again.
            self._native(config, 'sonarr', '/api/v3/command', {'name': 'RefreshSeries', 'seriesId': series['id']}, 'POST', timeout=20)
            task.update(refreshed=True, error='等待 Sonarr 更新分集信息')
            return
        if not files and unmapped and not task.get('episode_mapping'):
            task.update(stage='needs_episodes',file_candidates=[{'path':str(Path(i['path']).relative_to(task['dir'])),'size':i.get('size',0)} for i in sorted(self._videos(config,'sonarr',task),key=lambda i:i['path']) if not is_extra(i['path'])], error='部分文件没有可靠集号；请核对文件顺序后确认季号与起始集号')
            return
        if not files:
            if existing and not unmapped: raise NativeError('这些分集片库里都已经有了，没有覆盖')
            raise NativeError('下载内容里没有找到视频文件' if not unmapped else '认不出集数（%d 个文件）' % len(unmapped))
        command = self._send_import(config, 'sonarr', task, files)
        task.update(stage='importing', arr={'app': 'sonarr', 'id': series['id'], 'command': command, 'path': series.get('path'), 'episodes': sorted({i for f in files for i in f['episodeIds']})},
                    unmapped=sorted(set(unmapped)), existing=existing, import_started=now_ms(), error='')

    def _media_add_check(self, config, task):
        from management import now_ms
        arr = task.get('arr') or {}
        if arr.get('app') == 'linked': return
        command = self._native(config, arr['app'], '/api/v3/command/%d' % int(arr['command'])) or {}
        state = str(command.get('status') or '').lower()
        if state in ('queued', 'started', ''):
            if now_ms() - (task.get('import_started') or now_ms()) > IMPORT_TIMEOUT_MS:
                task.update(stage='failed', error='入库超过 30 分钟仍未完成，请在 %s 中查看' % arr['app'].capitalize())
            return
        if state != 'completed':
            task.update(stage='failed', error='%s 入库失败：%s' % (arr['app'].capitalize(), str(command.get('message') or state)[:160])); return
        if arr['app'] == 'radarr':
            movie = self._native(config, 'radarr', '/api/v3/movie/%d' % int(arr['id'])) or {}
            arr['path'] = movie.get('path') or arr.get('path')
            if not movie.get('hasFile'):   # there was no file before the import (checked), so a file now is ours
                task.update(stage='failed', error='Radarr 没有导入这个文件（可能判断为样片或质量不符），请在 Radarr 中查看'); return
            imported, missing = 1, 0
        else:
            episodes = {e['id']: e for e in self._native(config, 'sonarr', '/api/v3/episode?seriesId=%d' % int(arr['id']), timeout=20) or []}
            imported = sum(1 for i in arr.get('episodes', []) if episodes.get(i, {}).get('hasFile'))
            missing = len(arr.get('episodes', [])) - imported
            if not imported:
                task.update(stage='failed', error='Sonarr 没有导入任何分集，请在 Sonarr 中查看'); return
        self._media_add_done(task, missing)

    def _media_add_done(self, task, missing=0):
        from management import now_ms
        task.update(stage='imported', error='', imported_at=now_ms())
        notes = ['已归档到片库，正在同步到“待上架”']
        if missing: notes.append('%d 集没有导入，请在 Sonarr 中查看' % missing)
        if task.get('unmapped'): notes.append('%d 个文件认不出集数或有重复，未入库' % len(task['unmapped']))
        if task.get('existing'): notes.append('%d 个分集片库里已有，没有覆盖' % len(task['existing']))
        if (task.get('arr') or {}).get('app') == 'linked': notes.append('这部剧没有 TVDB 编号，不经 Sonarr 管理，不会自动下载在线字幕')
        task['notes'] = notes
        self._jellyfin_notice((task.get('arr') or {}).get('path'))

    def _jellyfin_notice(self, path):
        """Tell Jellyfin about the new folder (Radarr/Sonarr notify it too when their connection is configured)."""
        from management import Problem
        if not path: return
        try:
            if self.jellyfin_config(): self.jellyfin_request('/Library/Media/Updated', {'Updates': [{'Path': str(path), 'UpdateType': 'Created'}]})
        except Problem: pass   # realtime monitoring and the scheduled scan still find it

    def _media_route_prefix(self, absolute):
        """The hotel route prefix ('/media/…/') under which files of a library folder appear in the catalog."""
        try: path = Path(absolute).resolve()
        except (OSError, TypeError): return None
        with self.db() as db: sources = [json.loads(r[0]) for r in db.execute('SELECT body FROM sources')]
        for source in sources:
            root = Path(source['path']).resolve()
            if path == root or root in path.parents:
                rel = path.relative_to(root).as_posix()
                base = '/media/' if source['id'] == 'main' else '/media/@sources/' + source['id'] + '/'
                return base + ('' if rel == '.' else quote(rel, safe='/') + '/')
        return None

    def _media_add_listing(self, task):
        """'imported' (archived in the library) becomes 'listed' only when the hotel catalog has the film in 待上架."""
        from management import now_ms
        rev = task.get('rev')
        prefix = self._media_route_prefix((task.get('arr') or {}).get('path'))
        hit = None
        if prefix:
            with self.db() as db:
                for row in db.execute('SELECT id,body,published FROM media WHERE available=1'):
                    body = json.loads(row['body'])
                    if body.get('_archived') or not body.get('jellyfin_id'): continue
                    paths = [body['path']] if body.get('path') else [e.get('path', '') for e in body.get('episodes') or []]
                    if any(str(x).startswith(prefix) for x in paths): hit = (row['id'], body.get('title', ''), row['published']); break
        if hit:
            task.update(stage='listed', media_id=hit[0], error='')
            task['notes'] = ['已进入待上架（未上架）：《%s》' % hit[1][:60] if not hit[2] else '已在片库中，并且已经上架：《%s》' % hit[1][:60]] + task.get('notes', [])[1:]
        elif now_ms() - (task.get('imported_at') or now_ms()) > LIST_WAIT_MS:
            message = '已归档到片库，但 20 分钟内没有出现在“待上架”；请在“NAS 与应用”检查 Jellyfin 同步'
            if task.get('error') == message: return
            task['error'] = message
        else: return
        with self.db() as db: self._media_add_save(db, task, rev)

    def _media_add_link_series(self, config, task):
        """No TVDB id, so Sonarr cannot manage it: hardlink the episodes into the TV library, named for Jellyfin."""
        if not self._media_add_current(task): raise Retry('任务刚刚被修改，按最新设置重新入库')
        match = task['match']
        root = Path(task['dir'])
        library = self._library_root(config, 'tv')
        show = library / safe_name('%s (%s) [tmdbid-%d]' % (match.get('title') or 'Series', match.get('year') or '', match['tmdb_id'])).replace(' () ', ' ')
        default_season = (task.get('guess') or {}).get('season') or 1
        claims, unmapped = {}, []
        videos = sorted(p for p in root.rglob('*') if p.is_file() and p.suffix.lower() in VIDEO)
        if not videos: return self._only_files(task)
        for path in (p for p in videos if not is_extra(p.relative_to(root).as_posix())):
            numbers = episode_of(path.name, default_season)
            if numbers: claims.setdefault(numbers, []).append(path)
            else: unmapped.append(path.name[:160])
        linked, existing = 0, []
        for (season, number), paths in sorted(claims.items()):
            if len(paths) > 1: unmapped.extend(p.name[:160] for p in paths); continue
            dest = show / ('Season %02d' % season) / ('%s S%02dE%02d%s' % (safe_name(match.get('title') or 'Series'), season, number, paths[0].suffix.lower()))
            if dest.exists(): existing.append(paths[0].name[:160]); continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            try: os.link(paths[0], dest)
            except OSError as exc:
                if exc.errno in (errno.EXDEV, errno.EPERM, errno.EACCES): raise NativeError('无法在片库中建立硬链接（%s），请检查数据盘权限' % errno.errorcode.get(exc.errno, exc.errno))
                raise
            linked += 1
        task.update(unmapped=sorted(set(unmapped)), existing=existing, arr={'app': 'linked', 'path': str(show)})
        if not linked:
            raise NativeError('认不出集数（%d 个文件）' % len(unmapped) if unmapped else ('这些分集片库里都已经有了' if existing else '只找到花絮或预告，没有正片'))
        self._media_add_done(task)

    # ---------- 改片名 (any Jellyfin-synced item, writable users) ----------
    def media_rename(self, body, actor):
        from management import Problem, encoded, now_ms
        media_id = str(body.get('media_id') or '')
        try: tmdb_id = int(body.get('tmdb_id'))
        except (TypeError, ValueError): raise Problem('请选择一部影片') from None
        with self.db() as db:
            row = db.execute('SELECT body FROM media WHERE id=?', (media_id,)).fetchone()
        if not row: raise Problem('影片不存在', 404)
        item = json.loads(row[0])
        jid = item.get('jellyfin_id')
        if not jid or not re.fullmatch(r'[a-fA-F0-9]{32}', jid):
            raise Problem('这部影片不是从 Jellyfin 片库同步来的；请点“编辑”直接修改片名和海报')
        series = item.get('kind') == 'series'
        # TMDB numbers movies and TV shows separately: a TV id applied as a movie is a different film.
        if body.get('media_type') != ('tv' if series else 'movie'): raise Problem('请选择%s；电影和剧集不能互换' % ('剧集' if series else '电影'))
        results = self.jellyfin_request('/Items/RemoteSearch/' + ('Series' if series else 'Movie'),
                                        {'SearchInfo': {'ProviderIds': {'Tmdb': str(tmdb_id)}}, 'ItemId': jid, 'IncludeDisabledProviders': False})
        pick = next((r for r in results if isinstance(r, dict) and str((r.get('ProviderIds') or {}).get('Tmdb')) == str(tmdb_id)), None) if isinstance(results, list) else None
        if not pick: raise Problem('影片资料库没有返回这部片，请稍后再试或换一个结果', 502)
        title = str(pick.get('Name') or '')[:200]
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM media WHERE id=?', (media_id,)).fetchone()
            item = json.loads(row[0])
            meta = item.get('_jellyfin_meta') or {}
            # Values the hotel had overridden would otherwise keep winning over the corrected metadata.
            item['_renaming'] = {'title': title, 'tmdb_id': tmdb_id, 'at': now_ms()}
            db.execute('UPDATE media SET body=? WHERE id=?', (encoded(item), media_id))
            self.audit(db, actor, '改片名', '%s → %s' % (str(meta.get('title') or item.get('title'))[:80], title[:80]))
        def apply():
            # Jellyfin refreshes the whole item (every episode of a series) before answering; that can take minutes.
            try:
                self.jellyfin_request('/Items/RemoteSearch/Apply/' + jid + '?ReplaceAllImages=true', pick, timeout=900)
                with self.db() as db:
                    row=db.execute('SELECT body FROM media WHERE id=?',(media_id,)).fetchone()
                    current=json.loads(row[0]);meta=current.get('_jellyfin_meta') or {}
                    for k in ('title','year','description','poster'):
                        if k in meta: current[k]=meta[k]
                    current['_renaming']={'title':title,'tmdb_id':tmdb_id,'status':'awaiting_sync'}
                    db.execute('UPDATE media SET body=? WHERE id=?',(encoded(current),media_id))
            except Exception as exc:
                with self.db() as db:
                    row=db.execute('SELECT body FROM media WHERE id=?',(media_id,)).fetchone()
                    current=json.loads(row[0]);current['_renaming']={'title':title,'error':'资料更新失败，原资料保留；请重试'}
                    db.execute('UPDATE media SET body=? WHERE id=?',(encoded(current),media_id))
                    self.audit(db, actor, '改片名未完成', '%s：%s' % (title[:60], type(exc).__name__))
            finally:
                for delay in (5, 60): threading.Timer(delay, self.sync_wakeup.set).start()
        threading.Thread(target=apply, name='media-rename', daemon=True).start()
        return {'message': '正在改为《%s》，片名、海报和简介约 2 分钟内更新' % title[:60]}

    # ---------- media center summary (no network) ----------
    def media_add_issues(self):
        with self.db() as db: tasks = self._media_add_tasks(db, active_only=True)
        issues = []
        waiting = [t for t in tasks if t['stage'] == 'needs_match']
        unlisted = [t for t in tasks if t['stage'] == 'imported' and t.get('error')]
        failed = [t for t in tasks if t['stage'] == 'failed']
        stalled = [t for t in tasks if not (t.get('download') or {}).get('complete') and t['stage'] not in ('failed','imported','importing','needs_file','needs_episodes') and (t.get('download') or {}).get('state') not in ('paused','checking','moving') and __import__('management').now_ms()-t.get('progress_at',t['created_at'])>2*3600000]
        if stalled: issues.append({'level':'warning','title':'%d 个下载长时间没有进展' % len(stalled),'detail':'可能无人做种、资源信息未取得或数据盘异常；可在加片列表或下载器查看','action':'view:add'})
        selections=[t for t in tasks if t['stage'] in ('needs_file','needs_episodes')]
        if selections: issues.append({'level':'warning','title':'%d 个下载需要确认文件或集号' % len(selections),'detail':'请核对正片版本或分集顺序后继续','action':'view:add'})
        if failed: issues.append({'level': 'error', 'title': '%d 个加片任务失败' % len(failed), 'detail': '；'.join((t['name'] or t['hash'])[:40] + '：' + t['error'] for t in failed[:2])[:300], 'action': 'view:add'})
        if unlisted: issues.append({'level': 'warning', 'title': '%d 部影片已归档但没进入待上架' % len(unlisted), 'detail': unlisted[0]['error'], 'action': 'view:add'})
        if waiting: issues.append({'level': 'warning', 'title': '%d 个下载需要确认片名' % len(waiting), 'detail': '系统没有把握认出：' + '、'.join((t['name'] or '')[:40] for t in waiting[:3]), 'action': 'view:add'})
        return issues
