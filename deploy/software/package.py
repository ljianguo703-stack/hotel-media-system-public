#!/usr/bin/env python3
"""Build a program-only hotel update ZIP; never include data, keys, APKs or secrets."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile


def build(source, output, version, base_release):
    if not all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+_~:-]{0,99}', v) for v in (version, base_release)) or version == base_release:
        raise ValueError('版本无效或与当前版本相同')
    source, output = Path(source).resolve(), Path(output).resolve()
    files = {}
    for directory, extensions in [('server', {'.py'}), ('admin', {'.js', '.html', '.css', '.txt', '.svg', '.png', '.jpg', '.webp'})]:
        for path in sorted((source / directory).rglob('*')):
            relative = path.relative_to(source)
            if path.is_file() and not path.is_symlink() and path.suffix in extensions and not any(p.startswith('.') or p in ('tests', 'evidence', '__pycache__') for p in relative.parts) and not path.name.startswith('test_'):
                files[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not {'server/server.py', 'admin/index.html'} <= files.keys():
        raise ValueError('缺少服务器或后台主文件')
    manifest = {'format': 1, 'kind': 'system', 'id': 'hotel-tv-server', 'name': '酒店影院系统', 'version': version, 'base_release': base_release, 'files': files}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('hotel-software.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        for name in files:
            archive.write(source / name, name)
    return {'path': str(output), 'files': len(files), 'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--base-release', required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output, args.version, args.base_release), ensure_ascii=False))
