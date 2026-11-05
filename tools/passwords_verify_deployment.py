#!/usr/bin/env python3
"""Read-only deployed file, permission and service verification for the password page.

Run as the Ubuntu SSH operator. Use the application's native consistent database backups;
this utility does not open the live database or modify its permissions.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def digest(raw): return hashlib.sha256(raw).hexdigest()


def snapshot():
    current = Path('/opt/hotel-tv/current').resolve()
    paths = [Path('/etc/systemd/system/hotel-tv.service'), *Path('/etc/systemd/system/hotel-tv.service.d').glob('*'),
             *Path('/opt/stacks').glob('*/compose.yaml')]
    configs = {}
    for path in paths:
        if not path.is_file(): continue
        try: configs[str(path)] = digest(path.read_bytes())
        except PermissionError: configs[str(path)] = 'not-readable-by-ssh-operator'
    return {'release': current.name, 'configs': configs,
            'maintenance_pin_exists': Path('/srv/hotel-tv/data/maintenance-pin.json').exists(),
            'service': subprocess.check_output(['systemctl', 'is-active', 'hotel-tv'], text=True).strip()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--before', type=Path)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--component-manifest', type=Path)
    args = parser.parse_args()
    result = snapshot()
    if args.before:
        old = json.loads(args.before.read_text())
        for key in ('configs', 'maintenance_pin_exists'): assert result[key] == old[key], 'Preservation check failed: ' + key
        assert result['service'] == 'active'
        result['preserved'] = ['configs', 'maintenance_pin_exists']
    if args.manifest:
        manifest = json.loads(args.manifest.read_text())
        root = Path('/opt/hotel-tv/current').resolve()
        assert root.name == manifest['version']
        for filename, expected in manifest['files'].items(): assert digest((root / filename).read_bytes()) == expected, filename
        result['verified_program_files'] = len(manifest['files'])
    if args.component_manifest:
        component = json.loads(args.component_manifest.read_text())
        for filename, expected in component.items():
            path = Path(filename); stat = path.stat()
            assert stat.st_uid == 0 and stat.st_gid == 0 and stat.st_mode & 0o777 == 0o644
            assert digest(path.read_bytes()) == expected, filename
        result['verified_component_files'] = len(component)
        result['component_package'] = subprocess.check_output(['dpkg-query', '-W', '-f=${Status} ${Version}', 'hotel-tv-password-manager'], text=True).strip()
    args.output.write_text(json.dumps(result, indent=2)); args.output.chmod(0o600)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__': main()
