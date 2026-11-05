#!/usr/bin/env python3
"""Collect both locally built signed clients, with safe defaults and exact source provenance."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SIGNER = 'bc56fecb0d3c1f6026143f55ae84a8ff7d64d36045bb298eaadf765454d65dca'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(apks, build_tools, output):
    if output.exists():
        raise ValueError('Output exists; preserve the previous payload')
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT):
        raise ValueError('Commit reviewed source before collecting the client payload')
    expected = json.loads((ROOT / 'deploy/android/hotel-defaults.example.json').read_text())
    files = [p for p in (ROOT / 'app/src').rglob('*') if p.is_file() and p.name != 'hotel-defaults.json']
    files += [p for p in (ROOT / 'gradle').rglob('*') if p.is_file()]
    files += [ROOT / n for n in ['build.gradle.kts', 'settings.gradle.kts', 'gradle.properties',
                                 'app/build.gradle.kts', 'app/proguard-rules.pro', 'deploy/android/hotel-defaults.example.json']]
    payload, versions = {}, {}
    for flavor in ['standard', 'guard']:
        apk = apks / flavor / 'release' / f'app-{flavor}-release.apk'
        result = subprocess.check_output([str(build_tools / 'apksigner'), 'verify', '--print-certs', str(apk)], text=True)
        if re.search(r'Signer #1 certificate SHA-256 digest: (\w+)', result)[1] != SIGNER:
            raise ValueError('Client must use the existing release signer')
        with zipfile.ZipFile(apk) as z:
            if json.loads(z.read('assets/hotel-defaults.json')) != expected:
                raise ValueError('Client contains nonportable defaults')
        badging = subprocess.check_output([str(build_tools / 'aapt'), 'dump', 'badging', str(apk)], text=True)
        name, code, version = re.search(r"package: name='([^']+)' versionCode='([^']+)' versionName='([^']+)'", badging).groups()
        if name != 'com.xjg.hoteltv':
            raise ValueError('Unexpected application identity')
        versions[flavor] = {'version_code': int(code), 'version_name': version}
        payload[f'hotel-{flavor}.apk'] = sha(apk)
    if versions['standard']['version_code'] != versions['guard']['version_code']:
        raise ValueError('Client flavor versions differ')
    output.mkdir(parents=True)
    for flavor in versions:
        shutil.copy2(apks / flavor / 'release' / f'app-{flavor}-release.apk', output / f'hotel-{flavor}.apk')
    provenance = {'schema': 1, 'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                  'source_snapshot': {str(p.relative_to(ROOT)): sha(p) for p in sorted(files)}, 'payload': payload,
                  'signer_sha256': SIGNER, 'versions': versions, 'physical_acceptance': 'pending'}
    (output / 'client-provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
    known = json.loads((ROOT / 'third-party/known-tv-apks.json').read_text())
    known['allowed_apk_sha256'] = sorted(set(known['allowed_apk_sha256']) | set(payload.values()))
    (output / 'upgrade-policy.json').write_text(json.dumps(known, indent=2) + '\n')
    return provenance


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apks', type=Path, default=ROOT / 'app/build/outputs/apk')
    parser.add_argument('--build-tools', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    a = parser.parse_args()
    print(json.dumps(build(a.apks.resolve(), a.build_tools.resolve(), a.output.resolve())))
