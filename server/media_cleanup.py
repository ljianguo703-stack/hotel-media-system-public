"""On-demand local popularity and explicitly confirmed, journalled media-file cleanup.
No background deletion, recursive removal, symlink traversal or new TV telemetry.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
from urllib.parse import unquote

DAY = 86400000
BASIS = '近30天点播启动会话；按设备、客户端会话和电影/分集去重，插播恢复不重复计数。不是海报曝光点击率或有效观看时长。0仅表示当前已收到的记录为0；离线未上报及历史未采集不计。'


def core():
    import management
    return management


def routes(item):
    return list(dict.fromkeys(unquote(p) for p in [item.get('path'), *[e.get('path') for e in item.get('episodes', [])]] if isinstance(p, str) and p))


def strings(value):
    if isinstance(value, str): yield unquote(value)
    elif isinstance(value, dict):
        for v in value.values(): yield from strings(v)
    elif isinstance(value, list):
        for v in value: yield from strings(v)


def identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink]


@contextlib.contextmanager
def parent_fd(root, relative):
    """Pin each parent using directory FDs; never follow a replaceable link."""
    parts = relative.split('/')
    if any(p in ('', '.', '..') for p in parts) or '\\' in relative or '\x00' in relative:
        raise core().Problem('文件路径不安全')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = new
        yield fd, parts[-1]
    finally: os.close(fd)


class MediaCleanupOperations:
    def cleanup_files(self, db, item):
        """Only explicit bound media files, not the containing folder or neighbouring files."""
        sources = {r['id']: json.loads(r['body']) for r in db.execute('SELECT id,body FROM sources')}
        result = []
        for route in routes(item):
            if not route.startswith('/media/'): raise core().Problem('非本机媒体不能清理')
            relative = route[7:]; source = sources.get('main')
            if relative.startswith('@sources/'):
                parts = relative.split('/', 2)
                if len(parts) != 3: raise core().Problem('来源路径无效')
                source = sources.get(parts[1]); relative = parts[2]
            if not source or not source.get('enabled', True) or source.get('_archived'):
                raise core().Problem('来源已停用或不存在')
            root = Path(source['path'])
            if root.is_symlink() or root.resolve() != root or not any(root == a or a in root.parents for a in self.allowed_roots):
                raise core().Problem('来源路径已改变或不在允许范围')
            with parent_fd(str(root), relative) as (fd, name):
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode): raise core().Problem('不是普通文件或是符号链接')
                if not os.access(root / relative, os.R_OK): raise core().Problem('服务器没有读取该文件的权限')
                result.append({'root': str(root), 'relative': relative, 'route': route, 'identity': identity(info),
                               'writable': os.access((root / relative).parent, os.W_OK | os.X_OK),
                               'bytes': info.st_size, 'reclaimable_bytes': info.st_blocks * 512 if info.st_nlink == 1 else 0})
        if not result: raise core().Problem('没有绑定本地视频文件')
        if len(result) > 500: raise core().Problem('单次最多清理500个文件，请先按分集拆分')
        return result

    def media_insights(self):
        with self.db() as db: return self._media_insights(db)

    def _media_insights(self, db):
        now = core().now_ms(); begin = now - 30 * DAY
        rows = [dict(r) for r in db.execute('SELECT id,body,source,published,available FROM media')]
        items = {r['id']: json.loads(r['body']) for r in rows}; aliases = {}; counts = {}; latest = {}; seen = set()
        for ident, item in items.items():
            aliases[ident] = ident
            for e in item.get('episodes', []): aliases[str(e['id'])] = ident
        for r in db.execute("SELECT id,device,received,body FROM events WHERE json_extract(body,'$.type')='content_started'"):
            event = json.loads(r['body'])
            if event.get('exclude_from_ad_reports'): continue
            cid = str(event.get('content_id', '')); ident = aliases.get(cid)
            if not ident: continue
            stamp = event.get('at_ms')
            if type(stamp) is not int or stamp <= 0 or stamp > r['received'] + 300000: stamp = r['received']
            if stamp > now: continue
            latest[ident] = max(latest.get(ident, 0), stamp)
            key = (r['device'], event.get('client_session_id') or r['id'], cid)
            if stamp >= begin and key not in seen:
                seen.add(key); counts[ident] = counts.get(ident, 0) + 1
        # Index known references, including disabled/archived objects: deleting their file is still irreversible.
        refs = {ident: [] for ident in items}; path_owners = {}
        for ident, item in items.items():
            for p in routes(item): path_owners.setdefault(p, set()).add(ident)
        objects = [(r['kind'], json.loads(r['body'])) for r in db.execute("SELECT kind,body FROM managed_objects WHERE kind IN ('topic','channel','asset')")]
        objects += [('campaign', json.loads(r[0])) for r in db.execute('SELECT body FROM campaigns')]
        home = db.execute("SELECT value FROM kv WHERE key='home_screen'").fetchone()
        if home: objects.append(('home', json.loads(home[0])))
        jobs = [json.loads(r[0]) for r in db.execute('SELECT body FROM jobs')]
        objects += [('job', j) for j in jobs if j.get('status') in ('queued', 'running', 'paused')]
        objects += [('compat', j) for j in self.objects(db, 'compat') if j.get('job_status') in ('queued', 'running', 'paused')]
        for kind, obj in objects:
            values = set(strings(obj))
            for ident, item in items.items():
                if ident in values or values.intersection(routes(item)) or any(str(e['id']) in values for e in item.get('episodes', [])):
                    refs[ident].append(kind + '：' + str(obj.get('name') or obj.get('id') or '引用'))
        # Compare canonical paths as well, even when another source is unreadable/noncanonical.
        source_roots = {r['id']: Path(json.loads(r['body'])['path']) for r in db.execute('SELECT id,body FROM sources')}
        canonical_owners = {}
        for mid, media in items.items():
            for route in routes(media):
                if not route.startswith('/media/'): continue
                relative = route[7:]; root = source_roots.get('main')
                if relative.startswith('@sources/'):
                    parts = relative.split('/', 2)
                    if len(parts) != 3: continue
                    root = source_roots.get(parts[1]); relative = parts[2]
                if root is not None:
                    try: canonical_owners.setdefault(str((root / relative).resolve()), set()).add(mid)
                    except (OSError, RuntimeError): pass
        cached = {}; physical_owners = {}
        for ident, item in items.items():
            try:
                cached[ident] = self.cleanup_files(db, item)
                for f in cached[ident]: physical_owners.setdefault(tuple(f['identity'][:2]), set()).add(ident)
            except (OSError, core().Problem) as exc: cached[ident] = exc
        output = []
        for record in rows:
            ident = record['id']; item = items[ident]; reasons = []; files = []
            created = item.get('_created_at'); plays = counts.get(ident, 0); last = latest.get(ident, 0)
            if item.get('_archived'): reasons.append('已归档，请使用普通回收站管理')
            if not record['available']: reasons.append('源文件缺失')
            if type(created) is not int or created <= 0: reasons.append('入库时间未知，暂不列入冷门清理')
            elif now - created < 7 * DAY: reasons.append('新内容保护：入库不足7天')
            if item.get('_pinned'): reasons.append('置顶保护')
            if last and now - last < DAY: reasons.append('近期播放保护：24小时内启动过')
            reasons += refs[ident]
            if any(path_owners[p] - {ident} for p in routes(item)): reasons.append('其他作品共用原文件')
            value = cached[ident]
            if isinstance(value, Exception): reasons.append(str(value) if isinstance(value, core().Problem) else '文件不可访问、挂载改变或路径包含符号链接')
            else:
                files = value
                if any(not f['writable'] for f in files): reasons.append('服务器没有原文件删除权限')
                if any(canonical_owners.get(str((Path(f['root'])/f['relative']).resolve()), set()) - {ident} for f in files): reasons.append('其他作品通过另一来源共用原文件')
                if any(physical_owners[tuple(f['identity'][:2])] - {ident} for f in files): reasons.append('其他作品共用同一物理文件')
            output.append({'id': ident, 'title': item.get('title', ident), 'plays_30': plays, 'last_played_at': last,
                           'size_bytes': sum(f['bytes'] for f in files), 'reclaimable_bytes': sum(f['reclaimable_bytes'] for f in files),
                           'file_count': len(files), 'eligible': not reasons, 'protected_reasons': reasons})
        history = self.objects(db, 'media_cleanup_plan')
        history = sorted((p for p in history if p['status'] != 'preview'), key=lambda p: -p['created_at'])[:5]
        return {'days': 30, 'new_days': 7, 'checked_at': now, 'revision': self.kv(db, 'revision'), 'basis': BASIS,
                'rows': output, 'history': [{k: p.get(k) for k in ('id','status','created_at','result')} for p in history]}

    def cleanup_preview(self, body, actor, revision):
        m = core(); ids = body.get('ids')
        if not isinstance(ids, list) or not 1 <= len(ids) <= 50 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
            raise m.Problem('每次请选择1–50部不同作品')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE'); self.check_revision(db, revision)
            insights = {r['id']: r for r in self._media_insights(db)['rows']}; allowed = []; blocked = []; total_files = 0
            for ident in ids:
                row = insights.get(ident)
                if not row or not row['eligible']:
                    blocked.append({'id': ident, 'title': row['title'] if row else ident, 'reason': '；'.join(row['protected_reasons']) if row else '作品已不存在'}); continue
                item = self.record(db, 'media', ident); files = self.cleanup_files(db, item); total_files += len(files)
                allowed.append({**row, 'files': files})
            if total_files > 500: raise m.Problem('合计最多清理500个文件，请减少所选作品')
            now = m.now_ms(); plan = {'id': 'cleanup-' + secrets.token_hex(18), 'actor': actor, 'created_at': now, 'expires_at': now + 600000,
                                     'revision': revision, 'status': 'preview', 'rows': allowed, 'blocked': blocked}
            # Preview metadata is not a business edit; prune only expired, never-executed previews.
            for p in self.objects(db, 'media_cleanup_plan'):
                if p['status'] == 'preview' and p['expires_at'] < now: db.execute("DELETE FROM managed_objects WHERE kind='media_cleanup_plan' AND id=?", (p['id'],))
            self.put_object(db, 'media_cleanup_plan', plan)
            return {**plan, 'size_bytes': sum(r['size_bytes'] for r in allowed), 'reclaimable_bytes': sum(r['reclaimable_bytes'] for r in allowed)}

    def cleanup_execute(self, body, actor, revision):
        m = core()
        if body.get('confirm') is not True: raise m.Problem('请勾选确认永久删除原视频，普通回收站无法恢复')
        ident = m.text(body.get('plan_id'), 100, True)
        if not self.scan_lock.acquire(blocking=False): raise m.Problem('扫描、同步或清理正在进行，请稍后重试', 409)
        try:
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE'); self.check_revision(db, revision)
                plan = self.record(db, 'media_cleanup_plan', ident)
                if plan['actor'] != actor: raise m.Problem('此预览属于另一管理员', 403)
                if plan['status'] != 'preview' or plan['expires_at'] < m.now_ms() or plan['revision'] != revision:
                    raise m.Problem('清理预览过期、已使用或片库已改变，请重新预览', 409)
                if not plan['rows']: raise m.Problem('没有可清理的作品')
                current = {r['id']: r for r in self._media_insights(db)['rows']}
                for row in plan['rows']:
                    item = self.record(db, 'media', row['id']); fresh = current.get(row['id'])
                    if not fresh or not fresh['eligible'] or self.cleanup_files(db, item) != row['files']:
                        raise m.Problem('影片状态、播放活动或文件发生变化，请重新预览', 409)
                # Durable intent precedes any unlink. On interruption, this plan and archived records remain inspectable.
                for row in plan['rows']:
                    for f in row['files']: f['claim'] = '.hotel-cleanup-' + ident + '-' + hashlib.sha256(f['route'].encode()).hexdigest()[:16]
                plan['status'] = 'running'; plan['result'] = {'ok': [], 'failed': [], 'deleted_files': 0, 'deleted_bytes': 0}
                for row in plan['rows']:
                    item = self.record(db, 'media', row['id']); item.update(_archived=True, _archived_at=m.now_ms(), _cleanup_plan=ident)
                    self.write_record(db, 'media', item); db.execute('UPDATE media SET published=0 WHERE id=?', (item['id'],))
                    self.put_object(db, 'media_tombstone', {'id': item['id'], 'name': item.get('title', item['id']), 'source_id': '', 'keys': self.media_keys(item), 'purged_at': m.now_ms()})
                self.put_object(db, 'media_cleanup_plan', plan); self.changed(db, actor, '确认永久清理原视频', ident)
            # Serialize edits with a SQLite write transaction too. No scanner can re-add files between steps.
            for row in plan['rows']:
                failure = ''
                for f in row['files']:
                    try:
                        with self.db() as db:
                            db.execute('BEGIN IMMEDIATE')
                            with parent_fd(f['root'], f['relative']) as (fd, name):
                                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                                if identity(info) != f['identity'] or not stat.S_ISREG(info.st_mode):
                                    raise m.Problem('文件已改变，未删除')
                                # Claim the name before unlink, then recheck the claimed inode.
                                # A concurrent replacement must never be mistaken for the previewed file.
                                claim = f['claim']
                                try: os.stat(claim, dir_fd=fd, follow_symlinks=False)
                                except FileNotFoundError: pass
                                else: raise m.Problem('清理暂存名称已占用，未覆盖现有文件')
                                os.rename(name, claim, src_dir_fd=fd, dst_dir_fd=fd)
                                try:
                                    claimed = os.stat(claim, dir_fd=fd, follow_symlinks=False)
                                    if identity(claimed) != f['identity'] or not stat.S_ISREG(claimed.st_mode):
                                        raise m.Problem('文件被并发替换，未删除，请重新核对')
                                    os.unlink(claim, dir_fd=fd)
                                except (OSError, m.Problem):
                                    # Restore only when the original name is vacant; never overwrite a new file.
                                    try:
                                        os.link(claim, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                                        os.unlink(claim, dir_fd=fd)
                                    except OSError: pass
                                    raise
                            plan['result']['deleted_files'] += 1; plan['result']['deleted_bytes'] += f['bytes']
                            f['deleted'] = True; self.put_object(db, 'media_cleanup_plan', plan)
                    except (OSError, m.Problem) as exc:
                        failure = str(exc) if isinstance(exc, m.Problem) else '文件删除失败；原记录保留在已归档，请检查权限和挂载'
                        break
                with self.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    if failure: plan['result']['failed'].append({'id': row['id'], 'title': row['title'], 'reason': failure})
                    else:
                        db.execute('DELETE FROM media WHERE id=?', (row['id'],)); plan['result']['ok'].append(row['id'])
                    self.put_object(db, 'media_cleanup_plan', plan)
            with self.db() as db:
                plan['status'] = 'partial' if plan['result']['failed'] else 'completed'
                self.put_object(db, 'media_cleanup_plan', plan)
                self.changed(db, actor, '原视频清理完成' if plan['status']=='completed' else '原视频清理部分完成', ident)
            return {'id': ident, 'status': plan['status'], **plan['result'], 'message': '已删除字节是文件大小合计；硬链接、仍打开的文件、快照或压缩可能影响实际可用空间。'}
        finally: self.scan_lock.release()
