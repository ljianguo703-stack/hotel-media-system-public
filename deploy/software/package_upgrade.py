#!/usr/bin/env python3
"""Build one legacy-compatible DEB for automatic, transactional program upgrades."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile


def build(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('Preserve the existing package')
    spec = importlib.util.spec_from_file_location('unified_upgrade', Path(__file__).with_name('unified_upgrade.py'))
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    spec = importlib.util.spec_from_file_location('program_package', Path(__file__).with_name('package.py'))
    packager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(packager)
    with tempfile.TemporaryDirectory(prefix='hotel-unified-deb-') as temporary:
        root = Path(temporary) / 'root'
        target = root / 'usr/lib/hotel-tv-system-upgrade'
        target.mkdir(parents=True)
        program = target / 'program'
        archive_path = Path(temporary) / 'payload.zip'
        packager.build(source, archive_path, controller.TARGET, controller.BASES[0])
        with zipfile.ZipFile(archive_path) as archive:
            manifest = json.loads(archive.read('hotel-software.json'))
            for name in manifest['files']:
                path = program / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.read(name))
        info = {'format': 1, 'target': controller.TARGET, 'supported_bases': list(controller.BASES), 'files': manifest['files']}
        (target / 'manifest.json').write_text(json.dumps(info, ensure_ascii=False, indent=2) + '\n')
        shutil.copyfile(Path(__file__).with_name('unified_upgrade.py'), target / 'unified_upgrade.py')
        controller.verify_payload(target)
        control = root / 'DEBIAN'
        control.mkdir()
        version = '1.0.0~in20u2'
        (control / 'control').write_text('Package: hotel-tv-system-upgrade\nVersion: ' + version + '\nArchitecture: all\nMaintainer: Hotel Media System\nDepends: python3 (>= 3.9)\nDescription: Hotel cinema automatic program upgrade to IN20u2\n Explicit service-readable program permissions, snapshots and rollback.\n')
        (control / 'postinst').write_text('#!/bin/sh\nset -eu\ncase "${1:-}" in configure) ;; *) exit 0 ;; esac\nexec /usr/bin/python3 -I /usr/lib/hotel-tv-system-upgrade/unified_upgrade.py\n')
        for path in root.rglob('*'):
            path.chmod(0o755 if path.is_dir() else 0o644)
        (control / 'postinst').chmod(0o755)
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['dpkg-deb', '-Zgzip', '--build', '--root-owner-group', str(root), str(output)], check=True, stdout=subprocess.DEVNULL)
    return {'file': output.name, 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(), 'bytes': output.stat().st_size,
            'target': controller.TARGET, 'deb_version': version, 'runtime_files': len(info['files']), 'supported_bases': list(controller.BASES)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output), ensure_ascii=False))
