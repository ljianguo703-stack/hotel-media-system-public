"""D05: real download engines behind one small contract. Open-source engines only (owner decision 2026-10-28:
no Xunlei): qBittorrent for BT/magnet (keeps seeding), aria2 for http/https direct links.

    test() -> {status, message, version, capabilities}
    add(task_key, source, directory, limit_kbps) -> engine reference (BT info-hash or aria2 GID)
    status(ref) -> {state: queued|metadata|stalled|checking|allocating|moving|unknown|downloading|paused|complete|error|missing, downloaded, total (None = unknown),
                    speed, eta, files: [{path, size, done}], directory, error}
    pause(ref), resume(ref), cancel(ref)     # cancel never deletes downloaded files

Only items carrying this system's category/tag/GID/task id are ever touched, so other jobs in the same client are safe."""
import hashlib
import json
import secrets
import threading
import uuid
from urllib.parse import urlencode, urlsplit

from downloads_net import NetError, request

TAG = 'hoteltv'


def _trusted(url):
    p = urlsplit(url); return {((p.hostname or '').lower(), p.port)}


class QBittorrent:
    """qBittorrent WebUI API v2 (4.x and 5.x: 5.x renamed pause/resume to stop/start, detected by API version)."""
    name = 'qbittorrent'
    capabilities = {'magnet': True, 'torrent': True, 'http': False, 'seeding': True}

    def __init__(self, config, password):
        self.base = (config.get('url') or '').rstrip('/'); self.user = config.get('username') or ''
        self.password = password or ''; self.sid = None; self.api = None; self.lock = threading.Lock()

    def _call(self, path, form=None, files=None, retry=True):
        headers = {'Referer': self.base + '/', 'Origin': self.base}
        if self.sid: headers['Cookie'] = self.sid
        data = None
        if files:
            boundary = uuid.uuid4().hex; parts = []
            for k, v in (form or {}).items(): parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, k, v)).encode())
            for k, (fname, blob) in files.items():
                parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\nContent-Type: application/x-bittorrent\r\n\r\n' % (boundary, k, fname)).encode() + blob + b'\r\n')
            data = b''.join(parts) + ('--%s--\r\n' % boundary).encode(); headers['Content-Type'] = 'multipart/form-data; boundary=' + boundary
        elif form is not None:
            data = urlencode(form).encode(); headers['Content-Type'] = 'application/x-www-form-urlencoded'
        try:
            status, h, body, _ = request(self.base + path, data=data, headers=headers, timeout=15, trusted_hosts=_trusted(self.base))
        except NetError as exc:
            if exc.status == 403 and retry and path != '/api/v2/auth/login': self.sid = None; self.login(); return self._call(path, form, files, False)
            raise
        return body

    def login(self):
        if not self.base: raise NetError('unconfigured', '未配置 qBittorrent 地址')
        headers = {'Referer': self.base + '/', 'Origin': self.base, 'Content-Type': 'application/x-www-form-urlencoded'}
        status, h, body, _ = request(self.base + '/api/v2/auth/login', data=urlencode({'username': self.user, 'password': self.password}).encode(),
                                     headers=headers, timeout=10, trusted_hosts=_trusted(self.base))
        # 4.x answers 200 "Ok." with cookie SID; 5.x answers 204 with cookie QBT_SID_<port>. Failures: "Fails." or 401/403.
        pairs = [part.strip() for part in h.get('set-cookie', '').replace(',', ';').split(';') if '=' in part]
        cookie = next((p for p in pairs if 'SID' in p.split('=', 1)[0].strip().upper()), None)
        if body.strip() == b'Fails.' or status not in (200, 204) or not cookie: raise NetError('auth', 'qBittorrent 用户名或密码错误')
        self.sid = cookie
        self.api = tuple(int(x) for x in self._call('/api/v2/app/webapiVersion').decode().strip().split('.')[:2])

    def _ensure(self):
        with self.lock:
            if not self.sid: self.login()

    def test(self):
        if not self.base: return {'status': 'unconfigured', 'message': '未配置地址'}
        self.sid = None; self.login()
        version = self._call('/api/v2/app/version').decode().strip()
        return {'status': 'ok', 'message': '连接成功', 'version': '%s (WebAPI %s)' % (version, '.'.join(map(str, self.api))), 'capabilities': self.capabilities}

    def add(self, task_key, source, directory, limit_kbps=0):
        self._ensure()
        ref = source['infohash']
        existing = self._info(ref)
        task_tag = '%s-%s' % (TAG, task_key)
        if existing:
            self._require_owned(existing)
            if task_tag not in self._tags(existing): raise NetError('conflict', '相同种子已属于另一个下载任务，拒绝接管')
            if existing.get('state') in ('error', 'missingFiles', 'pausedDL', 'stoppedDL'): self.resume(ref)
            return ref  # idempotent only for the same task, including a lost reply
        self._call('/api/v2/torrents/createCategory', {'category': TAG, 'savePath': ''}, retry=True) if not self._has_category() else None
        form = {'savepath': directory, 'category': TAG, 'tags': '%s,%s-%s' % (TAG, TAG, task_key), 'paused': 'false', 'stopped': 'false',
                'autoTMM': 'false', 'useDownloadPath': 'false',
                'dlLimit': str(int(limit_kbps) * 1024 if limit_kbps else 0)}
        try:
            if source['kind'] == 'torrent': response = self._call('/api/v2/torrents/add', form, files={'torrents': ('task.torrent', source['data'])})
            else:
                form['urls'] = source['uri']; response = self._call('/api/v2/torrents/add', form)
            if response.strip().lower() not in (b'ok.', b'ok', b''):
                raise NetError('rejected', '下载器拒绝了种子或磁力链接，尚未确认开始下载')
        except NetError as exc:
            existing = self._info(ref) if exc.status == 409 else None
            if not existing: raise
            self._require_owned(existing)
            if task_tag not in self._tags(existing): raise NetError('conflict', '相同种子已属于另一个下载任务，拒绝接管')
        return ref

    @staticmethod
    def _tags(info): return {t.strip() for t in (info.get('tags') or '').split(',') if t.strip()}

    def _require_owned(self, info):
        if TAG not in self._tags(info): raise NetError('conflict', '任务不属于本系统，拒绝接管')

    def _has_category(self):
        try: return TAG in json.loads(self._call('/api/v2/torrents/categories') or b'{}')
        except (NetError, ValueError): return False

    def _info(self, ref):
        rows = json.loads(self._call('/api/v2/torrents/info?' + urlencode({'hashes': ref})) or b'[]')
        return rows[0] if rows else None

    def status(self, ref):
        self._ensure()
        info = self._info(ref)
        if not info: return {'state': 'missing', 'downloaded': 0, 'total': None, 'speed': 0, 'eta': None, 'files': [], 'directory': '', 'error': '下载器中找不到该任务'}
        self._require_owned(info)
        s = info.get('state', '')
        done = s not in ('error', 'missingFiles', 'checkingDL', 'checkingUP', 'checkingResumeData', 'moving') and (info.get('progress', 0) >= 1 or s in ('uploading', 'stalledUP', 'queuedUP', 'forcedUP', 'pausedUP', 'stoppedUP'))
        phases = {'error': 'error', 'missingFiles': 'error', 'pausedDL': 'paused', 'stoppedDL': 'paused',
                  'queuedDL': 'queued', 'metaDL': 'metadata', 'forcedMetaDL': 'metadata', 'stalledDL': 'stalled',
                  'checkingDL': 'checking', 'checkingResumeData': 'checking', 'allocating': 'allocating',
                  'moving': 'moving', 'downloading': 'downloading', 'forcedDL': 'downloading'}
        state = 'complete' if done else phases.get(s, 'unknown')
        files = []
        if done:
            for f in json.loads(self._call('/api/v2/torrents/files?' + urlencode({'hash': ref})) or b'[]'):
                files.append({'path': f['name'], 'size': f['size'], 'done': f.get('progress', 0) >= 1})
        total = info.get('size') or info.get('total_size') or 0  # -1 until the metadata of a magnet has arrived
        # completed is verified payload; downloaded can include discarded/network bytes.
        completed = info.get('completed')
        return {'state': state, 'downloaded': max(0, int(completed if completed is not None else info.get('downloaded') or 0)), 'total': int(total) if total and total > 0 and state != 'metadata' else None,
                'speed': int(info.get('dlspeed') or 0), 'eta': None if info.get('eta', 8640000) >= 8640000 else int(info['eta']), 'files': files,
                'directory': info.get('save_path', ''), 'error': 'qBittorrent 报告错误：' + s if state == 'error' else '',
                'seeds': max(0, int(info['num_seeds'])) if info.get('num_seeds') is not None else None,
                'peers': max(0, int(info['num_seeds'])) + max(0, int(info['num_leechs'])) if info.get('num_seeds') is not None and info.get('num_leechs') is not None else None}

    def reannounce(self, ref):
        self._ensure()
        info = self._info(ref)
        if not info: raise NetError('missing', '下载器中找不到该任务')
        self._require_owned(info)
        self._call('/api/v2/torrents/reannounce', {'hashes': ref})

    def _control(self, ref, new, old):
        self._ensure()
        info = self._info(ref)
        if not info: raise NetError('missing', '下载器中找不到该任务')
        self._require_owned(info)
        self._call('/api/v2/torrents/' + (new if self.api and self.api >= (2, 11) else old), {'hashes': ref})

    def pause(self, ref): self._control(ref, 'stop', 'pause')
    def resume(self, ref): self._control(ref, 'start', 'resume')

    def cancel(self, ref):
        self._ensure()
        info = self._info(ref)
        if info:
            self._require_owned(info)
            self._call('/api/v2/torrents/delete', {'hashes': ref, 'deleteFiles': 'false'})

    def set_limit(self, ref, limit_kbps):
        self._ensure()
        info = self._info(ref)
        if not info: raise NetError('missing', '下载器中找不到该任务')
        self._require_owned(info)
        self._call('/api/v2/torrents/setDownloadLimit', {'hashes': ref, 'limit': str(int(limit_kbps) * 1024 if limit_kbps else 0)})


class Aria2:
    """aria2 JSON-RPC. The GID is derived from the task, so a lost reply or a restart never creates a second download."""
    name = 'aria2'
    capabilities = {'magnet': False, 'torrent': False, 'http': True, 'seeding': False}

    def __init__(self, config, secret):
        self.url = config.get('url') or ''; self.secret = secret or ''

    def _rpc(self, method, *params):
        body = json.dumps({'jsonrpc': '2.0', 'id': secrets.token_hex(4), 'method': method, 'params': (['token:' + self.secret] if self.secret else []) + list(params)}).encode()
        try:
            status, h, raw, _ = request(self.url, data=body, headers={'Content-Type': 'application/json'}, timeout=15, trusted_hosts=_trusted(self.url))
        except NetError as exc:
            if exc.status != 400: raise
            try: raw = exc.body  # aria2 answers JSON-RPC errors with HTTP 400 and a JSON body
            except AttributeError: raise NetError('http', 'aria2 拒绝请求')
        try: reply = json.loads(raw)
        except ValueError: raise NetError('http', 'aria2 返回无法解析的内容')
        if 'error' in reply:
            message = str(reply['error'].get('message', ''))
            if 'Unauthorized' in message: raise NetError('auth', 'aria2 RPC 密钥错误')
            if 'is not found' in message or 'not found for gid' in message.lower(): raise NetError('missing', message[:120])
            raise NetError('http', 'aria2：' + message[:120])
        return reply['result']

    @staticmethod
    def gid(task_key): return hashlib.sha1(('hoteltv:' + task_key).encode()).hexdigest()[:16]

    def test(self):
        if not self.url: return {'status': 'unconfigured', 'message': '未配置 RPC 地址'}
        v = self._rpc('aria2.getVersion')
        return {'status': 'ok', 'message': '连接成功', 'version': v.get('version', ''), 'capabilities': self.capabilities}

    def add(self, task_key, source, directory, limit_kbps=0):
        if source.get('kind') != 'url': raise NetError('invalid', '本系统的 aria2 适配仅支持 HTTP/HTTPS，BT/磁力请配置 qBittorrent')
        gid = self.gid(task_key)
        try:
            old = self._rpc('aria2.tellStatus', gid, ['status'])
            if old.get('status') in ('error', 'removed'): self._rpc('aria2.removeDownloadResult', gid)
            else: return gid
        except NetError as exc:
            if exc.kind != 'missing': raise
        options = {'dir': directory, 'gid': gid, 'continue': 'true', 'max-download-limit': '%dK' % int(limit_kbps) if limit_kbps else '0', 'allow-overwrite': 'false', 'auto-file-renaming': 'false'}
        if source.get('filename'): options['out'] = source['filename']
        self._rpc('aria2.addUri', [source['uri']], options)
        return gid

    def status(self, ref):
        try: s = self._rpc('aria2.tellStatus', ref, ['status', 'totalLength', 'completedLength', 'downloadSpeed', 'files', 'dir', 'errorMessage', 'errorCode'])
        except NetError as exc:
            if exc.kind == 'missing': return {'state': 'missing', 'downloaded': 0, 'total': None, 'speed': 0, 'eta': None, 'files': [], 'directory': '', 'error': 'aria2 中找不到该任务（会话未保存或已重启）'}
            raise
        total, done, speed = int(s.get('totalLength') or 0), int(s.get('completedLength') or 0), int(s.get('downloadSpeed') or 0)
        state = {'active': 'downloading', 'waiting': 'queued', 'paused': 'paused', 'complete': 'complete', 'error': 'error', 'removed': 'missing'}.get(s.get('status'), 'error')
        files = [{'path': f['path'], 'size': int(f.get('length') or 0), 'done': int(f.get('completedLength') or 0) >= int(f.get('length') or 0) > 0} for f in s.get('files', []) if f.get('path')]
        return {'state': state, 'downloaded': done, 'total': total or None, 'speed': speed, 'eta': (total - done) // speed if speed and total else None,
                'files': files, 'directory': s.get('dir', ''), 'error': ('aria2 错误 %s：%s' % (s.get('errorCode', ''), s.get('errorMessage', ''))) if state == 'error' else ''}

    def pause(self, ref): self._rpc('aria2.forcePause', ref)
    def resume(self, ref): self._rpc('aria2.unpause', ref)

    def cancel(self, ref):
        try: self._rpc('aria2.forceRemove', ref)
        except NetError as exc:
            if exc.kind != 'missing': raise
        try: self._rpc('aria2.removeDownloadResult', ref)
        except NetError as exc:
            if exc.kind != 'missing': raise

    def set_limit(self, ref, limit_kbps): self._rpc('aria2.changeOption', ref, {'max-download-limit': '%dK' % int(limit_kbps) if limit_kbps else '0'})


def build(kind, config, secret):
    return {'qbittorrent': QBittorrent, 'aria2': Aria2}[kind](config, secret)
