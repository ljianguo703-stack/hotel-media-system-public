"""Discover mounted media volumes at scan time, then reuse the existing source/job workflow."""
import json
import os
from pathlib import Path
import secrets
import threading
import time
from physical_storage import snapshot as physical_snapshot

_cache_lock = threading.Lock()
_cache = (0, {})


def mounted_volumes(refresh=False):
    global _cache
    with _cache_lock:
        if not refresh and 'volumes' in _cache[1] and time.monotonic() - _cache[0] < 2:
            return _cache[1]
        inventory = physical_snapshot()
        rows, seen = [], set()
        for disk in inventory.get('devices', []):
            for part in [disk] + disk.get('partitions', []):
                for mount in part.get('mounts', []):
                    path = Path(mount)
                    if not path.is_absolute() or mount == '/' or any(mount == p or mount.startswith(p + '/') for p in ('/boot','/proc','/sys','/dev','/etc','/usr','/var/lib/docker','/var/lib/containerd')):
                        continue
                    if mount.startswith('/run/') and not mount.startswith('/run/media/'):
                        continue
                    if not part.get('fstype') or part.get('fstype') == 'swap':
                        continue
                    try:
                        if path.resolve() != path: continue
                        readable = path.is_dir() and os.access(path, os.R_OK | os.X_OK)
                        device_number = path.stat().st_dev if readable else None
                    except (OSError, RuntimeError):
                        readable, device_number = False, None
                    ident = part.get('uuid') or part.get('path') or mount
                    if ident in seen:
                        continue
                    seen.add(ident)
                    rows.append({'path': str(path), 'name': part.get('label') or part.get('name') or mount,
                                 'uuid': part.get('uuid', ''), 'device': part.get('path', ''), 'readable': readable,
                                 'readonly': bool(part.get('readonly')), 'size': part.get('size', 0), 'device_number': device_number})
        if inventory['status'] == 'unavailable':
            # Keep already approved playback working through a transient inventory failure.
            for previous in _cache[1].get('volumes', []):
                row = dict(previous)
                try:
                    p = Path(row['path'])
                    row['readable'] = p.is_dir() and os.access(p, os.R_OK | os.X_OK) and p.stat().st_dev == row['device_number']
                except OSError:
                    row['readable'] = False
                rows.append(row)
        unmounted = sum(not any(part.get('mounts') for part in [disk] + disk.get('partitions', [])) for disk in inventory.get('devices', []))
        result = {'status': inventory['status'], 'volumes': rows, 'unmounted_count': unmounted, 'message': inventory.get('message', '')}
        _cache = (time.monotonic(), result)
        return result


def mounted_roots():
    return [Path(r['path']) for r in mounted_volumes()['volumes'] if r['readable']]


def source_mount_available(source):
    if not source.get('_mount_uuid'):
        return True
    root = Path(source['path']).resolve()
    for volume in mounted_volumes()['volumes']:
        base = Path(volume['path'])
        if volume['uuid'] == source['_mount_uuid'] and (base == root or base in root.parents) and volume['readable']:
            try:
                return base.stat().st_dev == volume['device_number']
            except OSError:
                pass
    return False


class LibraryScanOperations:
    def library_scan_plan(self, user):
        import management as m
        if user['role'] != 'admin':
            raise m.Problem('发现新挂载盘需要管理员权限', 403)
        disks = mounted_volumes(refresh=True)
        with self.db() as db:
            sources = [json.loads(r['body']) for r in db.execute('SELECT body FROM sources')]
        choices = {}
        for s in sources:
            if s.get('_archived') or not s.get('enabled', True):
                continue
            path = Path(s['path']).resolve()
            allowed = any(path == p or p in path.parents for p in self.allowed_roots)
            mounted = source_mount_available(s)
            readable = allowed and mounted and path.is_dir() and os.access(path, os.R_OK | os.X_OK)
            choices[str(path)] = {'path': str(path), 'name': s['name'], 'kind': 'source', 'source_id': s['id'],
                                  'readable': readable, 'reason': '' if readable else '未挂载、已移出范围或没有读取权限'}
        for volume in disks['volumes']:
            path = volume['path']
            disabled = next((s for s in sources if str(Path(s['path']).resolve()) == path and (s.get('_archived') or not s.get('enabled', True))), None)
            existing_source = next((s for s in sources if str(Path(s['path']).resolve()) == path and (not s.get('_mount_uuid') or s['_mount_uuid'] == volume['uuid'])), None)
            existing = choices.get(path, {}) if existing_source else {}
            choices[path] = {**volume, **existing, 'kind': 'disk', 'readable': volume['readable'] and not disabled,
                             'reason': '此来源已停用或归档，请先在来源管理中恢复' if disabled else '' if volume['readable'] else '没有读取权限'}
        return {'choices': list(choices.values()), 'inventory_status': disks['status'], 'unmounted_count': disks['unmounted_count'], 'message': disks['message'],
                'notice': '未挂载盘不参与，已过滤系统、虚拟及容器挂载。同一卷只列一次；整盘包含已接入的启用子目录，停用来源不会自动恢复。新内容保持未上架，文件不移动。'}

    def start_mounted_library_scan(self, paths, user):
        import management as m
        if not isinstance(paths, list) or not paths or len(paths) > 64 or any(not isinstance(p, str) for p in paths):
            raise m.Problem('请选择要扫描的盘或已接入目录')
        plan = self.library_scan_plan(user)
        choices = {r['path']: r for r in plan['choices'] if r['readable']}
        if any(p not in choices for p in paths):
            raise m.Problem('挂载或权限已变化，请重新检测并确认扫描范围', 409)
        if plan['inventory_status'] != 'ok' and any(choices[p]['kind'] == 'disk' for p in paths):
            raise m.Problem('暂时无法确认当前挂载盘，请重新检测；已有片库保持可用',503)
        ids = set()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            sources = [json.loads(r['body']) for r in db.execute('SELECT body FROM sources')]
            for path in dict.fromkeys(paths):
                choice = choices[path]
                if choice['kind'] == 'source':
                    ids.add(choice['source_id']); continue
                source = next((s for s in sources if str(Path(s['path']).resolve()) == path and (not s.get('_mount_uuid') or s['_mount_uuid'] == choice.get('uuid'))), None)
                if source is None and choice.get('uuid'):
                    source = next((s for s in sources if s.get('_auto_mount') and s.get('_mount_uuid') == choice['uuid'] and not s.get('_archived') and s.get('enabled', True)), None)
                if source is None:
                    source = {'id': 'lib-' + secrets.token_hex(6), 'name': choice['name'], 'path': path, 'last_scan': 0}
                    sources.append(source)
                previous = dict(source)
                source.update(path=path, _auto_mount=True, _mount_uuid=choice.get('uuid', ''))
                db.execute('INSERT OR REPLACE INTO sources VALUES(?,?)', (source['id'], m.encoded(source)))
                if source != previous:
                    self.changed(db, user['name'], '确认挂载盘扫描范围', path)
                ids.add(source['id'])
                parent = Path(path)
                for child in sources:
                    if child.get('enabled', True) and not child.get('_archived') and parent in Path(child['path']).resolve().parents:
                        ids.add(child['id'])
        return self.start_library_update(user['name'], source_ids=sorted(ids), sync_jellyfin=False)
