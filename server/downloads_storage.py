"""Download location browser and frozen host/container mappings. No block-device mutations."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

from physical_storage import snapshot


def core():
    import management
    return management


class DownloadLocations:
    def _location_roots(self):
        with self.db() as db: settings = self.download_settings(db)
        config = Path(getattr(self, 'download_roots_file', os.environ.get('HOTEL_DOWNLOAD_ROOTS_FILE', '/etc/hotel-tv/download-roots.json')))
        try: registered = json.loads(config.read_text()).get('roots', [])
        except (OSError, ValueError, AttributeError): registered = []
        devices = snapshot().get('devices', []) if registered else []
        roots = []
        for r in registered:
            if not isinstance(r, dict) or not r.get('id') or not Path(r.get('path', '')).is_absolute(): continue
            roots.append({'id': str(r['id']), 'name': str(r.get('name') or r['id']), 'path': str(Path(r['path']).resolve()),
                          'engine_paths': r.get('engine_paths') or {}, 'available': Path(r['path']).is_dir()})
            if r.get('mount_uuid'):
                matches = [part for disk in devices if not disk['system'] for part in (disk['partitions'] or [disk])
                           if part.get('uuid') == r['mount_uuid'] and str(Path(r['path'])) in part.get('mounts', [])]
                roots[-1].update(mount_uuid=r['mount_uuid'], available=bool(matches) and not matches[0]['readonly'],
                                 message='' if matches and not matches[0]['readonly'] else '原硬盘未挂载到此处、已更换或只读')
        if not roots:
            path = settings['staging_dir']
            roots = [{'id': 'default', 'name': '默认下载文件夹', 'path': str(Path(path).resolve()), 'fallback': True, 'available': True,
                      'engine_paths': {k: v.get('directory') or path for k, v in settings['engines'].items()}}]
        # Expose individual mounted data disks as friendly choices, backed by already mapped roots.
        if registered:
            for disk in devices:
                if disk['system']: continue
                volumes = disk['partitions'] or [disk]
                mounted = [p for p in volumes if p['mounts']]
                if not mounted:
                    roots.append({'id': 'disk-' + (disk['serial'] or disk['name']), 'name': (next((p.get('label') for p in volumes if p.get('label')), '') or disk['name']) + ' · 未挂载',
                                  'path': '', 'available': False, 'message': '请先在硬盘管理中挂载', 'engine_paths': {}})
                for part in mounted:
                    for mount in part['mounts']:
                        ident = 'volume-' + (part['uuid'] or part['name']) + '-' + hashlib.sha1(mount.encode()).hexdigest()[:10]
                        label = (part['label'] or disk['name']) + ' · ' + mount
                        if any(r['path'] == mount and r.get('mount_uuid') == part['uuid'] for r in roots): continue
                        try: path = Path(mount).resolve()
                        except (OSError, RuntimeError):
                            roots.append({'id': ident, 'name': label, 'path': mount, 'available': False,
                                          'engine_paths': {}, 'message': '没有读取此挂载位置的权限'})
                            continue
                        parent = next((r for r in sorted(roots, key=lambda x: -len(x['path'])) if r['path'] and r['available'] and (path == Path(r['path']) or Path(r['path']) in path.parents)), None)
                        mapped = {k: str(Path(v) / path.relative_to(parent['path'])) for k, v in parent['engine_paths'].items()} if parent else {}
                        roots.append({'id': ident, 'name': label,
                                      'path': str(path), 'available': bool(parent) and not part['readonly'], 'engine_paths': mapped,
                                      'message': '' if parent and not part['readonly'] else '只读或未开放为下载位置，请挂载到 /mnt 或 /media 下'})
        return roots

    def _location(self, value, root_id=None, writable=False):
        m = core(); raw = Path(str(value or ''))
        if not raw.is_absolute() or '..' in raw.parts: raise m.Problem('请选择有效的下载文件夹')
        roots = self._location_roots()
        candidates = [r for r in roots if r['path'] and r['available'] and (not root_id or r['id'] == root_id)]
        try: path = raw.resolve()
        except (OSError, RuntimeError): raise m.Problem('无法读取所选文件夹，请刷新后重试')
        root = next((r for r in sorted(candidates, key=lambda r: -len(r['path'])) if path == Path(r['path']) or Path(r['path']) in path.parents), None)
        if not root: raise m.Problem('该文件夹未开放下载，或对应硬盘尚未挂载')
        # Refuse symlinks even when they currently resolve within the allowed root.
        base = Path(root['path'])
        if raw != path: raise m.Problem('下载位置不能经过符号链接')
        if path.exists() and not path.is_dir(): raise m.Problem('所选位置不是文件夹')
        if not path.exists() and not (root.get('fallback') and path == base): raise m.Problem('文件夹不存在，请先新建')
        probe = path
        while not probe.exists(): probe = probe.parent
        if writable and not os.access(probe, os.W_OK | os.X_OK): raise m.Problem('此文件夹没有写入权限，请选择其他位置或在文件管理中调整权限')
        return root, path

    def download_locations(self, args):
        roots = self._location_roots()
        public = [{k: v for k, v in r.items() if k not in ('engine_paths', 'fallback', 'mount_uuid')} for r in roots]
        requested = args.get('path', [''])[0]
        root_id = args.get('root', [''])[0]
        if not requested:
            root = next((r for r in roots if r['available'] and (not root_id or r['id'] == root_id)), None)
            if not root: return {'roots': public, 'folders': [], 'path': '', 'writable': False}
            requested, root_id = root['path'], root['id']
        root, path = self._location(requested, root_id)
        folders = []
        if path.exists():
            try:
                with os.scandir(path) as entries:
                    for entry in entries:
                        if entry.is_dir(follow_symlinks=False):
                            accessible = os.access(entry.path, os.R_OK | os.X_OK)
                            try: task_folder = (Path(entry.path) / '.hotel-download-task').exists()
                            except PermissionError: task_folder = False
                            if not task_folder:
                                folders.append({'name': entry.name, 'path': str(Path(entry.path)), 'hidden': entry.name.startswith('.'), 'accessible': accessible})
                        if len(folders) >= 1000: break
            except OSError: raise core().Problem('无法浏览此文件夹，请检查读取权限')
        folders.sort(key=lambda r: (r['hidden'], r['name'].casefold()))
        probe = path
        while not probe.exists(): probe = probe.parent
        try: free = shutil.disk_usage(probe).free
        except OSError: free = None
        return {'roots': public, 'root': root['id'], 'path': str(path), 'parent': str(path.parent) if path != Path(root['path']) else '',
                'folders': folders, 'writable': os.access(probe, os.W_OK | os.X_OK), 'free_bytes': free}

    def make_download_folder(self, body, user, revision):
        m = core(); name = m.text(body.get('name'), 100, True)
        if name in ('.', '..') or re.search(r'[/\\\x00-\x1f]', name): raise m.Problem('文件夹名称无效')
        if len(name.encode('utf-8')) > 240: raise m.Problem('文件夹名称过长')
        with self.db() as db: self.check_revision(db, revision)
        root, parent = self._location(body.get('path'), body.get('root'), writable=True)
        # Walk from an approved root using dir_fd and NOFOLLOW; a symlink swap must never escape it.
        fd = self._location_fd(root, parent)
        try:
            try:
                os.mkdir(name, mode=0o770, dir_fd=fd)
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try: os.fchmod(child, 0o770)
                finally: os.close(child)
            except FileExistsError: raise m.Problem('已有同名文件或文件夹')
        except PermissionError: raise m.Problem('没有新建文件夹的权限')
        finally: os.close(fd)
        with self.db() as db: self.changed(db, user['name'], '新建下载文件夹', str(parent / name))
        return self.download_locations({'root': [root['id']], 'path': [str(parent / name)]})

    def _location_fd(self, root, path):
        base = Path(root['path'])
        if root.get('fallback') and not base.exists(): base.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(str(base), flags)
        try:
            for part in path.relative_to(base).parts:
                nxt = os.open(part, flags, dir_fd=fd); os.close(fd); fd = nxt
            return fd
        except Exception:
            os.close(fd); raise

    def freeze_download_location(self, value=None):
        with self.db() as db: settings = self.download_settings(db)
        root, path = self._location(value or settings.get('save_dir') or settings['staging_dir'], writable=True)
        probe = path
        while not probe.exists(): probe = probe.parent
        relative = path.relative_to(root['path'])
        return {'root': root['id'], 'path': str(path), 'device': probe.stat().st_dev,
                'engine_paths': {k: str(Path(v) / relative) for k, v in root['engine_paths'].items()}}

    def guard_download_location(self, storage):
        root, path = self._location(storage['path'], storage['root'], writable=True)
        probe = path
        while not probe.exists(): probe = probe.parent
        if probe.stat().st_dev != storage['device']: raise core().Problem('目标硬盘已移除或挂载已改变，请恢复原挂载后再继续')
        return root, path

    def prepare_download_directory(self, task):
        root, path = self.guard_download_location(task['storage'])
        fd = self._location_fd(root, path)
        try:
            if os.fstat(fd).st_dev != task['storage']['device']: raise core().Problem('下载位置的硬盘已改变')
            created = True
            try: os.mkdir(task['folder_name'], mode=0o1770, dir_fd=fd)
            except FileExistsError: created = False
            child = os.open(task['folder_name'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                if created:
                    # Engines retain this service's primary group. Do not request SGID:
                    # the production unit deliberately enables RestrictSUIDSGID.
                    os.fchmod(child, 0o1770)
                    f = os.open('.hotel-download-task', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640, dir_fd=child)
                    with os.fdopen(f, 'w') as stream: stream.write(task['id'])
                else:
                    try: f = os.open('.hotel-download-task', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=child)
                    except FileNotFoundError: raise core().Problem('下载目录已存在且不属于此任务')
                    with os.fdopen(f) as stream:
                        if stream.read(100) != task['id']: raise core().Problem('下载目录已被其他任务占用')
                f = os.open('.ignore', os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o640, dir_fd=child); os.close(f)
            finally: os.close(child)
        finally: os.close(fd)
        return str(path / task['folder_name'])
