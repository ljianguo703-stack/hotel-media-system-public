#!/usr/bin/env python3
"""Publish the exact upstream source commits listed with the IN19 dependencies."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from urllib.request import Request, urlopen

REPOSITORY = 'ljianguo703-stack/hotel-media-system-public'
RELEASE = 'IN19-dependencies-20261107'


def main():
    with tempfile.TemporaryDirectory(prefix='hotel-upstream-sources-') as directory:
        root = Path(directory)
        subprocess.run(['gh', 'release', 'download', RELEASE, '--repo', REPOSITORY,
                        '--pattern', 'SOURCE-INDEX.json', '--dir', root], check=True)
        records = json.loads((root / 'SOURCE-INDEX.json').read_text())
        sources = root / 'sources'
        sources.mkdir()

        def fetch(record):
            name = record['archive']
            if Path(name).name != name:
                raise ValueError('Invalid source archive filename')
            request = Request(f"https://api.github.com/repos/{record['repo']}/tarball/{record['commit']}",
                              headers={'User-Agent': 'Hotel-IN19-Source-Publication'})
            path = sources / name
            with urlopen(request, timeout=120) as response, path.open('wb') as stream:
                shutil.copyfileobj(response, stream, 1024 * 1024)
            with tarfile.open(path) as archive:
                record['license_files'] = [entry for entry in archive.getnames()
                                           if Path(entry).name.lower() in ('license', 'license.txt', 'license.md', 'copying', 'copyright')]
            record['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            record['bytes'] = path.stat().st_size
            print('Verified source commit', record['repo'], record['commit'], flush=True)
            return record

        with ThreadPoolExecutor(max_workers=4) as pool:
            records = list(pool.map(fetch, records))
        index = sources / 'SOURCE-INDEX.json'
        index.write_text(json.dumps(records, ensure_ascii=False, indent=2) + '\n')
        bundle = root / 'upstream-application-sources-IN19.tar.gz'
        with tarfile.open(bundle, 'w:gz') as archive:
            for path in sorted(sources.iterdir()):
                archive.add(path, arcname=path.name)
        print('Source bundle SHA256', hashlib.sha256(bundle.read_bytes()).hexdigest(), flush=True)
        subprocess.run(['gh', 'release', 'upload', RELEASE, '--repo', REPOSITORY,
                        str(bundle), str(index), '--clobber'], check=True)


if __name__ == '__main__':
    main()
