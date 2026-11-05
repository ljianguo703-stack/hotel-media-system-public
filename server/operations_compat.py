"""TV01 / I04: media compatibility pre-check and compatible copies.

ffprobe reads every playable file; rules for the hotel TVs decide whether it plays directly, needs only a container
change, an audio re-encode or a full video transcode. Copies are served from the virtual route /media/_compat and stored
in the compat root (media/_compat by default; a fresh install points it at the media data disk), never over the source;
one job runs at a time at low priority and can be paused, resumed, cancelled and retried. A job that would leave the
disk below its reserve fails as "space" and is retried automatically once space is available."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time
import tempfile
from urllib.parse import quote, unquote


def core():
    import management
    return management


VIDEO_OK = {'h264', 'hevc', 'vp9'}
AUDIO_OK = {'aac', 'mp3', 'opus', 'vorbis', 'flac', 'pcm_s16le', 'pcm_s24le', 'mp2'}
CONTAINER_OK = {'mov,mp4,m4a,3gp,3g2,mj2', 'matroska,webm', 'mpegts'}
IMAGE_SUBS = {'hdmv_pgs_subtitle', 'dvd_subtitle', 'dvb_subtitle', 'xsub'}
VERDICTS = {'ok': '可直接播放', 'remux': '需换封装', 'audio': '需转音轨', 'video': '需转码视频', 'error': '无法读取'}
ACTIVE = ('queued', 'running', 'paused')
SPACE_RESERVE_MIN = 5 * 1024 ** 3   # never fill a disk: keep 2 % of it, at least 5 GiB …
SPACE_RESERVE_MAX = 50 * 1024 ** 3  # … and at most 50 GiB, so big data disks are not left idle
SPACE_RESERVE_RATIO = 0.02
SPACE_RETRY_MS = 10 * 60 * 1000     # automatic re-queue of a space failure at most every 10 minutes


class SpaceShortage(RuntimeError):
    """Not enough free space for a compatible copy; retried automatically when space returns."""


def judge(info, max_height=2160):
    """Classify one ffprobe result for the hotel TVs. Returns (verdict, reasons, warnings)."""
    streams = info.get('streams', []); fmt = info.get('format', {})
    video = [s for s in streams if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')]
    audio = [s for s in streams if s.get('codec_type') == 'audio']
    subs = [s for s in streams if s.get('codec_type') == 'subtitle']
    rank = {'ok': 0, 'remux': 1, 'audio': 2, 'video': 3}
    state = {'v': 'ok'}; reasons, warnings = [], []
    def bump(level, reason):
        if rank[level] > rank[state['v']]: state['v'] = level
        reasons.append(reason)
    if not video: return 'error', ['没有可播放的视频流'], []
    v = video[0]; codec = v.get('codec_name', '')
    if codec not in VIDEO_OK: bump('video', '视频编码 %s 电视不能直接解码' % (codec or '未知'))
    elif codec == 'h264' and str(v.get('profile', '')).lower() in ('high 10', 'high 4:2:2', 'high 4:4:4 predictive'): bump('video', 'H.264 %s 档次电视不支持' % v.get('profile'))
    if int(v.get('height') or 0) > max_height: bump('video', '分辨率 %sx%s 超过电视上限' % (v.get('width'), v.get('height')))
    if fmt.get('format_name') not in CONTAINER_OK: bump('remux', '封装格式 %s 建议换成 MKV' % (fmt.get('format_name') or '未知'))
    if not audio: warnings.append('没有音轨')
    elif audio[0].get('codec_name') not in AUDIO_OK: bump('audio', '默认音轨 %s 需生成 AAC 双声道兼容版，避免旧电视无声' % audio[0].get('codec_name'))
    elif int(audio[0].get('channels') or 0) > 2: bump('audio', '默认音轨为 %s 声道，生成 AAC 双声道兼容版' % audio[0]['channels'])
    if any(s.get('codec_name') in IMAGE_SUBS for s in subs): warnings.append('含图片字幕（PGS/DVD），电视无法显示，兼容版也不会转换')
    return state['v'], reasons, warnings


def summary(info):
    streams = info.get('streams', []); fmt = info.get('format', {})
    v = next((s for s in streams if s.get('codec_type') == 'video'), {})
    return {'container': fmt.get('format_name', ''), 'duration': float(fmt.get('duration') or 0), 'bitrate': int(fmt.get('bit_rate') or 0),
            'video': ' '.join(str(x) for x in (v.get('codec_name', ''), v.get('profile', ''), '%sx%s' % (v['width'], v['height']) if v.get('width') else '') if x),
            'audio': ['%s %sch %s' % (s.get('codec_name', ''), s.get('channels', ''), s.get('tags', {}).get('language', '')) for s in streams if s.get('codec_type') == 'audio'],
            'subtitles': ['%s %s' % (s.get('codec_name', ''), s.get('tags', {}).get('language', '')) for s in streams if s.get('codec_type') == 'subtitle']}


def fingerprint(path: Path):
    """Cheap but change-sensitive identity of a source file: size, mtime and hashes of its first and last MiB."""
    st = path.stat(); h = hashlib.sha256(('%d:%d' % (st.st_size, st.st_mtime_ns)).encode())
    with path.open('rb') as f:
        h.update(f.read(1 << 20))
        if st.st_size > 2 << 20: f.seek(-(1 << 20), os.SEEK_END); h.update(f.read(1 << 20))
    return h.hexdigest()[:24], st.st_size, st.st_mtime_ns


class CompatOperations:
    def init_compat_runtime(self):
        self._compat_lock = threading.RLock()
        self._compat_worker = None
        self._compat_proc = None
        self._compat_running = None

    def compat_tools(self):
        found = {}
        for name in ('ffprobe', 'ffmpeg'):
            found[name] = shutil.which(name) or next((p for p in ('/usr/bin/' + name, '/opt/homebrew/bin/' + name, '/usr/local/bin/' + name) if os.access(p, os.X_OK)), '')
        return found

    def compat_root(self):
        """Where copies live. Stored as kv 'compat_root' by the installer; cached briefly (media_path is hot)."""
        cached = self.__dict__.get('_compat_root_cache')
        if cached and time.monotonic() - cached[0] < 30: return cached[1]
        root = self.media / '_compat'
        try:
            with self.db() as db:
                row = db.execute("SELECT value FROM kv WHERE key='compat_root'").fetchone()
            value = json.loads(row[0]) if row else None
            if isinstance(value, str) and value.startswith('/') and '\x00' not in value and '..' not in Path(value).parts:
                root = Path(value)
        except (OSError, ValueError): pass
        self.__dict__['_compat_root_cache'] = (time.monotonic(), root)
        return root

    def compat_space(self, size):
        """(enough, free_bytes, reserve_bytes) for writing a copy of about `size` bytes into the compat root."""
        root = self.compat_root()
        probe = root if root.exists() else root.parent
        usage = shutil.disk_usage(probe)
        reserve = min(SPACE_RESERVE_MAX, max(SPACE_RESERVE_MIN, int(usage.total * SPACE_RESERVE_RATIO)))
        return usage.free - int(size) >= reserve, usage.free, reserve

    @staticmethod
    def compat_id(route):
        return 'c-' + hashlib.sha256(route.encode()).hexdigest()[:20]

    def compat_record(self, db, ident):
        row = db.execute("SELECT body FROM managed_objects WHERE kind='compat' AND id=?", (ident,)).fetchone()
        return json.loads(row['body']) if row else None

    def compat_files(self, db):
        """Every playable file in the index: (route, media id, title)."""
        rows = []
        for r in db.execute('SELECT id,body FROM media'):
            m = json.loads(r['body'])
            if m.get('_archived'): continue
            if m.get('kind') == 'series':
                for e in m.get('episodes', []): rows.append((e['path'], m['id'], '%s · %s' % (m.get('title', ''), e.get('title', ''))))
            elif m.get('path'): rows.append((m['path'], m['id'], m.get('title', '')))
        return rows

    def probe_file(self, route):
        m = core(); tool = self.compat_tools()['ffprobe']
        if not tool: raise m.Problem('服务器未安装 ffprobe（ffmpeg 套件），无法预检', 409)
        path = self.media_path(unquote(route))
        out = subprocess.run([tool, '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', str(path)], capture_output=True, timeout=120, check=False)
        if out.returncode != 0:
            message = (out.stderr.decode('utf-8', 'replace').strip().splitlines() or ['ffprobe 失败'])[-1].replace(str(path), path.name)
            return path, None, message.replace(str(path.resolve()), path.name)[:200]
        return path, json.loads(out.stdout.decode('utf-8', 'replace') or '{}'), ''

    def compat_probe(self, ids, actor, revision):
        """Probe the files of the given media ids (every episode of a series)."""
        m = core()
        if not isinstance(ids, list) or not 1 <= len(ids) <= 500: raise m.Problem('每次请选择 1–500 部影片')
        with self.db() as db:
            self.check_revision(db, revision)
            files = [f for f in self.compat_files(db) if f[1] in set(ids)]
        result = {'ok': [], 'failed': []}
        for route, media_id, title in files:
            ident = self.compat_id(route)
            try:
                path, info, error = self.probe_file(route)
                fp, size, mtime = fingerprint(path)
                verdict, reasons, warnings = ('error', [error], []) if info is None else judge(info)
                with self.db() as db:
                    old = self.compat_record(db, ident) or {}
                    record = {**old, 'id': ident, 'route': route, 'media_id': media_id, 'title': title, 'fingerprint': fp, 'size': size, 'mtime': mtime,
                              'verdict': verdict, 'reasons': reasons, 'warnings': warnings, 'info': summary(info or {}), 'probed_at': m.now_ms()}
                    if old.get('copy_fingerprint') and old['copy_fingerprint'] != fp: record.update(copy_status='stale')
                    # A replaced file (e.g. re-downloaded to the same name) starts over instead of inheriting a failure.
                    if old.get('fingerprint') and old['fingerprint'] != fp and old.get('job_status') in ('failed', 'cancelled'):
                        record.update(job_status='', job_error='', job_error_kind='', job_progress=0)
                    self.put_object(db, 'compat', record)
                result['ok'].append(ident)
            except m.Problem as exc: result['failed'].append({'id': ident, 'title': title, 'reason': str(exc)})
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                result['failed'].append({'id': ident, 'title': title, 'reason': type(exc).__name__})
                # Terminal until the file changes: a hanging or unreadable file must not be re-probed forever.
                try:
                    st = self.media_path(unquote(route)).stat()
                    with self.db() as db:
                        old = self.compat_record(db, ident) or {}
                        self.put_object(db, 'compat', {**old, 'id': ident, 'route': route, 'media_id': media_id, 'title': title, 'fingerprint': '',
                                                        'size': st.st_size, 'mtime': st.st_mtime_ns, 'verdict': 'error',
                                                        'reasons': ['预检未完成：' + type(exc).__name__], 'warnings': [], 'info': {}, 'probed_at': m.now_ms()})
                except (OSError, m.Problem): pass
        with self.db() as db: self.audit(db, actor, '兼容预检', '%d 个文件（失败 %d）' % (len(result['ok']), len(result['failed'])))
        return result

    def compat_enqueue(self, ids, actor, revision):
        m = core()
        if not self.compat_tools()['ffmpeg']: raise m.Problem('服务器未安装 ffmpeg，无法生成兼容版', 409)
        if not isinstance(ids, list) or not 1 <= len(ids) <= 500: raise m.Problem('每次请选择 1–500 个文件')
        result = {'ok': [], 'failed': []}
        with self.db() as db:
            self.check_revision(db, revision)
            for ident in ids:
                r = self.compat_record(db, ident)
                if not r: result['failed'].append({'id': ident, 'title': ident, 'reason': '请先预检'}); continue
                if r['verdict'] in ('ok', 'error'): result['failed'].append({'id': ident, 'title': r['title'], 'reason': '无需或无法生成兼容版（%s）' % VERDICTS[r['verdict']]}); continue
                if r.get('job_status') in ACTIVE: result['failed'].append({'id': ident, 'title': r['title'], 'reason': '已在队列中'}); continue
                r.update(job_status='queued', job_progress=0, job_error='', job_error_kind='', job_queued_at=m.now_ms(), job_actor=actor)
                self.put_object(db, 'compat', r); result['ok'].append(ident)
            if result['ok']: self.audit(db, actor, '加入兼容版队列', '%d 个文件' % len(result['ok']))
        self.start_compat_worker()
        return result

    def compat_control(self, ident, action, actor, revision=None):
        m = core()
        labels = {'pause': '暂停', 'resume': '继续', 'cancel': '取消', 'retry': '重试', 'use_original': '改用原文件', 'use_copy': '改用兼容版'}
        if action not in labels: raise m.Problem('操作无效')
        with self._compat_lock, self.db() as db:
            if action in ('use_original', 'use_copy'): self.check_revision(db, revision)
            r = self.compat_record(db, ident)
            if not r: raise m.Problem('记录不存在', 404)
            status, mine = r.get('job_status', ''), self._compat_proc is not None and self._compat_running == ident and self._compat_proc.poll() is None
            if status == 'running' and action in ('pause', 'cancel') and not mine: raise m.Problem('任务正在收尾，请刷新后重试', 409)
            if action == 'pause' and status in ('queued', 'running'):
                if status == 'running' and mine:
                    self._compat_proc.terminate()
                r.update(job_status='paused', job_paused_from='queued', job_progress=0, job_generation=r.get('job_generation', 0)+1)
            elif action == 'resume' and status == 'paused':
                r.update(job_status='queued', job_progress=0, job_queued_at=m.now_ms())
            elif action == 'cancel' and status in ACTIVE:
                r['job_status'] = 'cancelled'
                r['job_generation'] = r.get('job_generation', 0)+1
                if mine: self._compat_proc.terminate()
            elif action == 'retry' and status in ('failed', 'cancelled'): r.update(job_status='queued', job_progress=0, job_error='', job_error_kind='', job_queued_at=m.now_ms())
            elif action == 'use_original' and r.get('copy_route'): r['use_copy'] = False
            elif action == 'use_copy' and r.get('copy_route') and r.get('copy_status') == 'ready': r['use_copy'] = True
            else: raise m.Problem('当前状态不能执行此操作')
            self.put_object(db, 'compat', r)
            if action in ('use_original', 'use_copy'): self.changed(db, actor, labels[action], r['title'])
            else: self.audit(db, actor, labels[action] + '兼容版任务', r['title'])
        if action in ('resume', 'retry'): self.start_compat_worker()
        return r

    def recover_compat_jobs(self):
        """After a restart a half-written copy is useless: running jobs go back to the queue and temp files are removed."""
        with self.db() as db:
            for r in self.objects(db, 'compat'):
                if r.get('job_status') == 'running': r.update(job_status='queued', job_progress=0); self.put_object(db, 'compat', r)
                elif r.get('job_status') == 'paused' and r.get('job_paused_from') == 'running': r['job_paused_from'] = 'queued'; self.put_object(db, 'compat', r)
        root = self.compat_root()
        for temp in (root.glob('*/*.part.mkv') if root.exists() else []):
            try: temp.unlink()
            except OSError: pass
        self.start_compat_worker()

    def start_compat_worker(self):
        with self._compat_lock:
            if self._compat_worker and self._compat_worker.is_alive(): return
            self._compat_worker = threading.Thread(target=self._compat_loop, name='compat-worker', daemon=True)
            self._compat_worker.start()

    def _compat_loop(self):
        try:
            while True:
                # The empty check and unregister share the starter's lock. A concurrent enqueue either
                # reaches this query, or sees no registered worker after acquiring the lock and starts one.
                with self._compat_lock:
                    with self.db() as db:
                        queue = sorted([o for o in self.objects(db, 'compat') if o.get('job_status') == 'queued'], key=lambda o: o.get('job_queued_at', 0))
                    if not queue:
                        self._compat_worker = None
                        return
                try: self._run_compat(queue[0]['id'])
                except Exception as exc:
                    with self.db() as db:
                        r = self.compat_record(db, queue[0]['id'])
                        if r and r.get('job_status') not in ('cancelled', 'paused'):
                            r.update(job_status='failed', job_error='任务异常：' + type(exc).__name__)
                            self.put_object(db, 'compat', r)
        finally:
            with self._compat_lock:
                if self._compat_worker is threading.current_thread(): self._compat_worker = None

    def ffmpeg_args(self, record, source, target, threads):
        verdict = record['verdict']
        args = [self.compat_tools()['ffmpeg'], '-nostdin', '-hide_banner', '-v', 'error', '-y', '-i', str(source), '-map', '0:v:0', '-map', '0:a?', '-map', '0:s?', '-dn', '-map_metadata', '0']
        video = ['-c:v', 'copy'] if verdict in ('remux', 'audio') else ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '21', '-profile:v', 'high', '-level', '4.1', '-pix_fmt', 'yuv420p', '-vf', "scale='min(1920,iw)':-2"]
        audio = ['-c:a', 'copy'] if verdict == 'remux' else ['-c:a', 'aac', '-b:a', '192k', '-ac', '2']
        return args + video + audio + ['-c:s', 'copy', '-threads', str(threads), '-progress', 'pipe:1', '-f', 'matroska', str(target)]

    def _set_progress(self, ident, percent):
        with self.db() as db:
            cur = self.compat_record(db, ident)
            if cur and cur.get('job_status') == 'running': cur['job_progress'] = percent; self.put_object(db, 'compat', cur)

    def _run_compat(self, ident):
        m = core()
        with self.db() as db:
            r = self.compat_record(db, ident)
            if not r or r.get('job_status') != 'queued': return
            row = db.execute("SELECT value FROM kv WHERE key='compat_settings'").fetchone()
            threads = int(json.loads(row[0]).get('threads', 2)) if row else 2
        folder = self.compat_root() / ident; folder.mkdir(parents=True, exist_ok=True)
        temp, error, error_kind = folder / ('job.%d.part.mkv' % r.get('job_generation', 0)), '', ''
        automatic = getattr(self, 'automation_enabled', lambda: False)()
        full_disk = '磁盘已满，兼容副本未完成；' + ('腾出空间后自动继续' if automatic else '腾出空间后在兼容检查中重试')
        proc = None
        try:
            source = self.media_path(unquote(r['route']))
            before = fingerprint(source)[0]
            if before != r['fingerprint']: raise RuntimeError('源文件在预检后已变化，请重新预检')
            # Automation installs only (existing servers keep their behaviour): the copy is about the source size.
            if automatic:
                enough, free, reserve = self.compat_space(source.stat().st_size)
                if not enough:
                    raise SpaceShortage('磁盘空间不足：兼容副本约需 %.1f GB，当前可用 %.1f GB，需保留 %.1f GB；腾出空间后自动继续'
                                        % (source.stat().st_size / 1024 ** 3, free / 1024 ** 3, reserve / 1024 ** 3))
            duration = float(r.get('info', {}).get('duration') or 0)
            with tempfile.TemporaryFile() as errors:
                with self._compat_lock, self.db() as db:
                    latest = self.compat_record(db, ident)
                    if not latest or latest.get('job_status') != 'queued': return
                    if latest.get('fingerprint') != before: raise RuntimeError('预检在任务启动前发生变化，请重试')
                    r = latest
                    temp = folder / ('job.%d.part.mkv' % r.get('job_generation', 0))
                    # No running-without-process window: controls use the same lock.
                    args = self.ffmpeg_args(r, source, temp, threads)
                    nice = shutil.which('nice')
                    if nice: args = [nice, '-n', '10'] + args
                    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=errors)
                    self._compat_proc, self._compat_running = proc, ident
                    latest.update(job_status='running', job_started_at=m.now_ms(), job_progress=0)
                    self.put_object(db, 'compat', latest)
                last = 0.0
                for line in proc.stdout:
                    text = line.decode('utf-8', 'replace').strip()
                    if text.startswith('out_time_ms=') and duration and time.monotonic() - last > 1:
                        try: done = int(text.split('=', 1)[1]) / 1e6
                        except ValueError: continue
                        last = time.monotonic(); self._set_progress(ident, max(0, min(99, int(done * 100 / duration))))
                code = proc.wait()
                size = errors.seek(0, os.SEEK_END); errors.seek(max(0, size-4096))
                stderr = errors.read().decode('utf-8', 'replace').strip()
            if code != 0:
                error = (stderr.splitlines() or ['ffmpeg 退出码 %d' % code])[-1][:300]
                if 'No space left' in stderr: error, error_kind = full_disk, 'space'   # only the target is written
            elif fingerprint(source)[0] != before: error = '转码期间源文件发生变化，已丢弃副本'
        except (OSError, RuntimeError, m.Problem) as exc:
            error = str(exc)[:300]
            if isinstance(exc, SpaceShortage): error_kind = 'space'
            elif isinstance(exc, OSError) and exc.errno == 28 and exc.filename and Path(exc.filename).resolve().is_relative_to(self.compat_root().resolve()):
                error, error_kind = full_disk, 'space'   # a full /tmp or system disk is a different problem; not retried as space
        finally:
            if proc is not None:
                if proc.poll() is None:
                    try: os.kill(proc.pid, signal.SIGCONT); proc.terminate(); proc.wait(timeout=5)
                    except ProcessLookupError: pass
                    except subprocess.TimeoutExpired: proc.kill(); proc.wait()
                if proc.stdout: proc.stdout.close()
            with self._compat_lock:
                if self._compat_running == ident: self._compat_proc = None; self._compat_running = None
        with self._compat_lock, self.db() as db:
            cur = self.compat_record(db, ident) or {}
            if cur.get('job_generation', 0) != r.get('job_generation', 0) or cur.get('job_status') in ('cancelled', 'paused') or error:
                try: temp.unlink()
                except OSError: pass
                if cur.get('job_generation', 0) == r.get('job_generation', 0) and cur.get('job_status') not in ('cancelled', 'paused'):
                    cur.update(job_status='failed', job_error=error or '未知错误', job_error_kind=error_kind, job_failed_at=m.now_ms()); self.put_object(db, 'compat', cur)
                return
            target = folder / (r['fingerprint'][:12] + '.mkv'); temp.replace(target)
            for stale in folder.glob('*.mkv'):
                if stale != target and not stale.name.endswith('.part.mkv'):
                    try: stale.unlink()
                    except OSError: pass
            cur.update(job_status='done', job_progress=100, job_finished_at=m.now_ms(), copy_route='/media/_compat/%s/%s' % (ident, quote(target.name)),
                       copy_fingerprint=r['fingerprint'], copy_status='ready', use_copy=True, copy_bytes=target.stat().st_size)
            self.put_object(db, 'compat', cur)
            self.changed(db, 'system:兼容版', '兼容版完成', cur.get('title', ident))

    def compat_routes(self, db):
        """Source route -> compatible copy route, only while the source is still exactly what was converted."""
        routes = {}
        for r in self.objects(db, 'compat'):
            if not (r.get('copy_route') and r.get('use_copy', True) and r.get('copy_status') == 'ready'): continue
            try:
                st = self.media_path(unquote(r['route'])).stat()
                self.media_path(unquote(r['copy_route']))
            except (OSError, core().Problem): continue
            if (st.st_size, st.st_mtime_ns) == (r['size'], r['mtime']): routes[r['route']] = r['copy_route']
        return routes

    def compat_rows(self, args):
        arg = lambda k: args.get(k, [''])[0]
        with self.db() as db:
            records = {o['route']: o for o in self.objects(db, 'compat')}
            rows = []
            for route, media_id, title in self.compat_files(db):
                r = records.get(route) or {'id': self.compat_id(route), 'route': route, 'verdict': ''}
                rows.append({**r, 'media_id': media_id, 'title': title, 'verdict_label': VERDICTS.get(r.get('verdict'), '未预检')})
        counts = {}
        for r in rows: counts[r.get('verdict') or 'pending'] = counts.get(r.get('verdict') or 'pending', 0) + 1
        if arg('verdict'): rows = [r for r in rows if (r.get('verdict') or 'pending') == arg('verdict')]
        if arg('q'): rows = [r for r in rows if arg('q').casefold() in r['title'].casefold()]
        tools = self.compat_tools()
        return {'rows': rows[:500], 'total': len(rows), 'counts': counts, 'tools': {k: bool(v) for k, v in tools.items()}, 'running': self._compat_running or ''}
