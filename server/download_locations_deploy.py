#!/usr/bin/env python3
"""DL10 host/container storage mappings. No mount, partition or format operations.

Run apply only with the hotel service and its two download engines stopped; the caller
owns the release lock, consistent data backup, restart/readiness checks and rollback.
Original compose bytes, registry, systemd drop-in and root directory ACLs are preserved.
"""
import argparse
import copy
import grp
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

ROOTS = [
    ('media', '内置媒体存储', '/srv/hotel-tv/media'),
    ('mnt', '硬盘挂载目录 /mnt', '/mnt'),
    ('external', '硬盘挂载目录 /media', '/media'),
    ('removable', '移动存储 /run/media', '/run/media'),
    ('shared', '共享文件夹', '/srv/nas/shared'),
]
REGISTRY = Path('/etc/hotel-tv/download-roots.json')
DROPIN = Path('/etc/systemd/system/hotel-tv.service.d/20-download-locations.conf')


def mapped_config(config, kind, gid, locations=None):
    """Keep existing images, ports, limits, sessions and legacy /downloads mapping."""
    out = copy.deepcopy(config)
    service = out['services'][kind]
    volumes = service.setdefault('volumes', [])
    for ident, _, path in (ROOTS if locations is None else locations):
        target = '/hotel-storage/' + ident
        existing = next((v for v in volumes if v.get('target') == target), None)
        desired = {'type': 'bind', 'source': path, 'target': target,
                   'bind': {'propagation': 'rslave', 'create_host_path': False}}
        if existing:
            if existing.get('source') != path: raise ValueError('Conflicting storage mapping: ' + target)
            existing.update(desired)
        else: volumes.append(desired)
    # Retain the engine UID (and all owned session files), give it our shared primary group.
    # qB's entrypoint drops supplementary groups, so group_add alone is insufficient.
    if kind == 'qbittorrent':
        env = service.setdefault('environment', {})
        if not str(env.get('PUID', '')).isdigit(): raise ValueError('Expected numeric qB PUID')
        env['PGID'] = str(gid)
    else:
        uid = str(service.get('user', '')).split(':')[0]
        if not uid.isdigit(): raise ValueError('Expected numeric aria2 UID')
        service['user'] = uid + ':' + str(gid)
    return out


def run(args):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def save_original(path, backup, records):
    record = {'path': str(path), 'exists': path.exists()}
    if path.exists():
        if path.is_symlink() or not path.is_file(): raise ValueError('Expected regular file: ' + str(path))
        name = str(len(records)) + '.original'
        shutil.copy2(path, backup / name)
        st = path.stat(); record.update(file=name, uid=st.st_uid, gid=st.st_gid, mode=st.st_mode & 0o7777)
    records.append(record)


def location_specs(registered=(), extra=None, devices=()):
    """Keep custom roots and bind them to an explicitly selected, mounted data volume."""
    specs = {ident: {'id': ident, 'name': name, 'path': value} for ident, name, value in ROOTS}
    for item in list(registered) + ([extra] if extra else []):
        ident, path = str(item.get('id', '')), str(item.get('path', ''))
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', ident): raise ValueError('Invalid storage id')
        if ident in specs and specs[ident]['path'] != path: raise ValueError('Conflicting storage root')
        if ident in {r[0] for r in ROOTS}: continue
        if not Path(path).is_absolute() or '..' in Path(path).parts or any(c in path for c in '\r\n\x00'):
            raise ValueError('Invalid storage path')
        uuid = item.get('mount_uuid')
        matches = [p for d in devices if not d['system'] for p in (d['partitions'] or [d])
                   if p.get('uuid') == uuid and path in p.get('mounts', []) and not p.get('readonly')]
        if not uuid or not matches: raise ValueError('Custom root must be the selected mounted writable data volume')
        specs[ident] = {'id': ident, 'name': str(item.get('name') or ident)[:100], 'path': path, 'mount_uuid': uuid}
    return list(specs.values())


def apply(backup, extra=None):
    if os.geteuid() != 0: raise SystemExit('Run with sudo')
    backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    gid = grp.getgrnam('hotel-tv').gr_gid
    from physical_storage import snapshot
    registered = json.loads(REGISTRY.read_text())['roots'] if REGISTRY.exists() else []
    specs = location_specs(registered, extra, snapshot().get('devices', []))
    locations = [(r['id'], r['name'], r['path']) for r in specs]
    plans, records, created = [], [], []
    for kind in ('qbittorrent', 'aria2'):
        path = Path('/opt/stacks/hotel-' + kind + '/compose.yaml')
        config = json.loads(run(['docker', 'compose', '-f', str(path), 'config', '--format', 'json']))
        if len(config['services']) != 1 or kind not in config['services']: raise ValueError('Unexpected engine stack')
        plans.append((path, mapped_config(config, kind, gid, locations)))
        save_original(path, backup, records)
    for path in (REGISTRY, DROPIN): save_original(path, backup, records)
    for _, _, value in locations:
        path = Path(value)
        if path.is_symlink() or (path.exists() and not path.is_dir()): raise ValueError('Unexpected storage root: ' + value)
        if not path.exists(): path.mkdir(mode=0o755); created.append(value)
    paths = [r[2] for r in locations]
    (backup / 'acl.original').write_bytes(run(['getfacl', '-p', *paths]))
    (backup / 'restore.json').write_text(json.dumps({'files': records, 'created_dirs': created}))
    # Nonrecursive: files and mounted filesystem ownership/permissions remain the owner's choice.
    run(['setfacl', '-m', 'g:%d:rwx,d:g:%d:rwx' % (gid, gid), *paths])
    registry = {'roots': [{**r, 'engine_paths': {k: '/hotel-storage/' + r['id'] for k in ('qbittorrent', 'aria2')}} for r in specs]}
    REGISTRY.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + '\n')
    os.chown(REGISTRY, 0, gid); REGISTRY.chmod(0o640)
    DROPIN.parent.mkdir(mode=0o755, exist_ok=True)
    quoted = ['"' + p.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"' for p in paths]
    DROPIN.write_text('[Service]\nReadWritePaths=' + ' '.join(quoted) + '\n')
    DROPIN.chmod(0o644)
    for path, config in plans:
        path.write_text(json.dumps(config, indent=2) + '\n'); path.chmod(0o600)
        run(['docker', 'compose', '-f', str(path), 'config', '--quiet'])
    print('STORAGE_MAPPINGS_PREPARED roots=%d engines=2; no disks mounted or formatted' % len(specs))


def rollback(backup):
    if os.geteuid() != 0: raise SystemExit('Run with sudo')
    manifest = backup / 'restore.json'
    if not manifest.exists(): return
    spec = json.loads(manifest.read_text())
    for record in spec['files']:
        path = Path(record['path'])
        if record['exists']:
            shutil.copy2(backup / record['file'], path)
            os.chown(path, record['uid'], record['gid']); path.chmod(record['mode'])
        else: path.unlink(missing_ok=True)
    run(['setfacl', '--restore=' + str(backup / 'acl.original')])
    for value in reversed(spec['created_dirs']):
        try: Path(value).rmdir()
        except OSError: pass
    print('STORAGE_MAPPINGS_ROLLED_BACK')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['apply', 'rollback'])
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--extra-root', type=Path, help='Protected JSON file selecting one mounted data volume')
    args = parser.parse_args()
    if args.command == 'apply': apply(args.backup, json.loads(args.extra_root.read_text()) if args.extra_root else None)
    else: rollback(args.backup)
