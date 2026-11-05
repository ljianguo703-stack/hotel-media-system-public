"""Small hotel-side bridge: native apps own downloading; existing AV01 owns compatibility.

When media automation is enabled (full fresh install) every library item, whatever its source, must pass the
compatibility check before it can be published or shown to TVs. The scheduler only spends its bounded batch on items
it can actually advance, so permanently failed files can never starve newly arrived films."""
import json
from pathlib import Path
import re
import shutil
import threading
import time
from urllib.parse import unquote

AUTOMATION_BATCH = 10
GC_INTERVAL = 1800   # seconds between sweeps of orphaned compatible copies


class MediaAutomation:
    def automation_enabled(self, db=None):
        if db is None:
            with self.db() as conn: return self.automation_enabled(conn)
        row = db.execute("SELECT value FROM kv WHERE key='media_automation'").fetchone()
        return bool(row and json.loads(row[0]).get('enabled'))

    def automation_readiness(self, db, item, strong=False):
        """No network dependency. Check each source, including every episode and changed upgrades."""
        if not self.automation_enabled(db): return {'ready': True, 'label': ''}
        from operations_compat import fingerprint
        files = [item['path']] if item.get('path') else [e['path'] for e in item.get('episodes', [])]
        if not files: return {'ready': False, 'label': '没有可播放文件'}
        for route in files:
            record = self.compat_record(db, self.compat_id(route))
            try:
                source = self.media_path(unquote(route)); st = source.stat()
                valid = record and (record.get('size'), record.get('mtime')) == (st.st_size, st.st_mtime_ns)
                if valid and strong: valid = fingerprint(source)[0] == record.get('fingerprint')
                if not valid: return {'ready': False, 'label': '等待兼容预检'}
                if record['verdict'] == 'ok': continue
                if record['verdict'] == 'error': return {'ready': False, 'label': '文件预检失败'}
                if record.get('copy_status') == 'ready' and record.get('copy_fingerprint') == record.get('fingerprint') and record.get('use_copy', True):
                    self.media_path(unquote(record['copy_route']))
                    continue
                if record.get('job_status') == 'failed' and record.get('job_error_kind') == 'space':
                    return {'ready': False, 'label': '磁盘空间不足，腾出空间后自动生成兼容副本'}
                return {'ready': False, 'label': '兼容副本失败，请在兼容预检中重试' if record.get('job_status') in ('failed', 'cancelled') else '正在准备AAC兼容副本'}
            except (OSError, KeyError, ValueError): return {'ready': False, 'label': '源文件或兼容副本不可读'}
            except __import__('management').Problem: return {'ready': False, 'label': '源文件或兼容副本不可读'}
        return {'ready': True, 'label': '兼容检查通过'}

    def automation_publish_errors(self, db, item):
        state = self.automation_readiness(db, item, strong=True)
        return [] if state['ready'] else [state['label'] + '；完成后才能上架']

    def _automation_step(self, db, routes):
        """For one item: (needs_probe, record ids the existing queue can take now). Terminal states yield nothing."""
        problem = __import__('management').Problem
        needs_probe, queue = False, []
        for route in routes:
            record = self.compat_record(db, self.compat_id(route))
            try: st = self.media_path(unquote(route)).stat()
            except (OSError, problem): continue          # unreadable: shown to the owner, not retried blindly
            if not record or (record.get('size'), record.get('mtime')) != (st.st_size, st.st_mtime_ns):
                needs_probe = True; continue
            if record['verdict'] in ('ok', 'error'): continue
            if record.get('copy_status') == 'ready' and record.get('copy_fingerprint') == record.get('fingerprint'): continue
            status = record.get('job_status')
            if not status or (status == 'done' and record.get('copy_status') == 'stale'):
                queue.append(record['id'])
            elif status == 'failed' and record.get('job_error_kind') == 'space':
                from operations_compat import SPACE_RETRY_MS
                if time.time() * 1000 - record.get('job_failed_at', 0) < SPACE_RETRY_MS: continue
                try:
                    if self.compat_space(record.get('size') or st.st_size)[0]: queue.append(record['id'])
                except OSError: pass
        return needs_probe, queue

    def automation_work(self):
        """Items the scheduler can advance right now, oldest first. Failed or unreadable items are excluded."""
        work = []
        with self.db() as db:
            routes = {}
            for route, media_id, _ in self.compat_files(db): routes.setdefault(media_id, []).append(route)
            for row in db.execute('SELECT body FROM media WHERE available=1 ORDER BY rowid'):
                item = json.loads(row['body'])
                if item.get('_archived') or self.automation_readiness(db, item)['ready']: continue
                needs_probe, queue = self._automation_step(db, routes.get(item['id'], []))
                if needs_probe or queue: work.append(item['id'])
        return work

    def prepare_automated_media(self):
        """Bounded batch of actionable items only; serial low-priority existing worker."""
        if not self.automation_enabled(): return
        actor, problem = 'system:片库兼容', __import__('management').Problem
        for item_id in self.automation_work()[:AUTOMATION_BATCH]:
            try:
                with self.db() as db:
                    routes = [r for r, media_id, _ in self.compat_files(db) if media_id == item_id]
                    needs_probe, _ = self._automation_step(db, routes)
                    revision = self.kv(db, 'revision')
                if needs_probe: self.compat_probe([item_id], actor, revision)
                with self.db() as db:
                    _, queue = self._automation_step(db, routes)
                    revision = self.kv(db, 'revision')
                if queue: self.compat_enqueue(queue, actor, revision)
            except problem:
                continue   # e.g. an owner edit changed the revision meanwhile; the next round retries

    def compat_gc(self, min_age_ms=3600 * 1000):
        """Delete copies whose source no longer belongs to any library item (purged, renamed, upgraded by Radarr).
        Archived and temporarily missing items keep theirs. Only <compat root>/c-<hex> folders are ever removed."""
        from management import now_ms
        root = self.compat_root().resolve()
        with self.db() as db:
            live = set()
            for row in db.execute('SELECT body FROM media'):
                item = json.loads(row['body'])
                live.update([item['path']] if item.get('path') else [e.get('path') for e in item.get('episodes', [])])
            stale = [r for r in self.objects(db, 'compat') if r.get('route') not in live and r.get('job_status') not in ('queued', 'running', 'paused')
                     and now_ms() - max(r.get('probed_at') or 0, r.get('job_finished_at') or 0, r.get('job_failed_at') or 0) > min_age_ms]
        removed = 0
        for record in stale:
            if not re.fullmatch(r'c-[0-9a-f]{20}', record.get('id', '')): continue
            folder = root / record['id']
            if folder.is_symlink(): continue
            if folder.is_dir() and folder.resolve().parent == root: shutil.rmtree(folder)
            with self.db() as db: db.execute("DELETE FROM managed_objects WHERE kind='compat' AND id=?", (record['id'],))
            removed += 1
        return removed

    def run_automation_scheduler(self):
        if not self.automation_enabled(): return
        def loop():
            last_gc = 0
            while True:
                try:
                    self.prepare_automated_media()
                    if time.monotonic() - last_gc > GC_INTERVAL: self.compat_gc(); last_gc = time.monotonic()
                except Exception as exc: print('Media compatibility will retry:', type(exc).__name__, flush=True)
                time.sleep(10)
        threading.Thread(target=loop, name='media-compatibility', daemon=True).start()
        self.run_media_add_scheduler()   # its own thread: a slow import never stalls compatibility work
