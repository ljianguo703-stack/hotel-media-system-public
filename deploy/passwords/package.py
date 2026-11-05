#!/usr/bin/env python3
"""Build a data-only native DEB with dpkg-deb. No maintainer scripts or credential changes."""
import argparse
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def build(source, output, version):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not re.fullmatch(r'[0-9][A-Za-z0-9.+~:-]{0,80}', version): raise ValueError('DEB 版本无效')
    if output.exists(): raise ValueError('输出已经存在，未覆盖')
    files = {'deploy/passwords/manage.py': 'usr/local/lib/hotel-passwords/manage.py'}
    for name in ('manifest.json', 'index.html', 'passwords.js', 'passwords.css'):
        files['admin/passwords/' + name] = 'usr/share/cockpit/hotel-passwords/' + name
    with tempfile.TemporaryDirectory(prefix='hotel-passwords-deb-') as directory:
        root = Path(directory)
        root.chmod(0o755)  # The DEB's ./ entry must never inherit tempfile's private 0700 mode.
        for src, dest in files.items():
            target = root / dest; target.parent.mkdir(parents=True, exist_ok=True)
            if not (source / src).is_file() or (source / src).is_symlink(): raise ValueError('组件源文件无效')
            shutil.copyfile(source / src, target); target.chmod(0o644)
        control = root / 'DEBIAN'; control.mkdir()
        (control / 'control').write_text(
            'Package: hotel-tv-password-manager\nVersion: ' + version + '\nArchitecture: all\n'
            'Maintainer: Hotel Media System\nDepends: python3 (>= 3.9), cockpit-bridge\n'
            'Description: Hotel application password management through native Cockpit authorization\n'
            ' Data files only; no installation scripts, password changes or extra privileged daemon.\n')
        (control / 'control').chmod(0o644)
        for path in root.rglob('*'):
            if path.is_dir(): path.chmod(0o755)
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['dpkg-deb', '-Zgzip', '--build', '--root-owner-group', str(root), str(output)], check=True,
                       stdout=subprocess.DEVNULL)
    return {'file': output.name, 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(), 'files': len(files), 'version': version}


if __name__ == '__main__':
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output, args.version)))
