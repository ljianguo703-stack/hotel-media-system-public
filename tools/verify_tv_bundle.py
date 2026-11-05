#!/usr/bin/env python3
"""Verify four candidate packages byte for byte, including dependencies, APK signer and defaults."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import zipfile
from build_tv_payload import SIGNER

ROOT = Path(__file__).resolve().parents[1]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def verify(output, build_tools):
    expected = json.loads((ROOT / 'deploy/android/hotel-defaults.example.json').read_text())
    lock = json.loads((ROOT / 'third-party/tv-runtime.lock.json').read_text())['files']
    results = []
    for system, arch in [('darwin', 'arm64'), ('darwin', 'amd64'), ('linux', 'amd64'), ('windows', 'amd64')]:
        archives = list((output / 'release').glob(f'*-{system}-{arch}.zip'))
        if len(archives) != 1:
            raise ValueError('Expected one package per platform')
        archive = archives[0]; folder = archive.with_suffix('')
        files = json.loads((folder / 'manifest.json').read_text())['files']
        if 'helpers/power-mode.jar' not in files or 'helpers/provenance.json' not in files:
            raise ValueError('Missing native power helper')
        helper = json.loads((folder / 'helpers/provenance.json').read_text())
        if sha((folder / 'helpers/power-mode.jar').read_bytes()) != helper['jar_sha256']:
            raise ValueError('Power helper bytes differ')
        for name, digest in helper['source_snapshot'].items():
            if sha((ROOT / name).read_bytes()) != digest:
                raise ValueError('Power helper source differs')
        with zipfile.ZipFile(folder / 'helpers/power-mode.jar') as dex:
            if dex.namelist() != ['classes.dex'] or not dex.read('classes.dex').startswith(b'dex\n'):
                raise ValueError('Unexpected native helper contents')
        if system == 'linux' and not (folder / '启动手机局域网安装.sh').is_file():
            raise ValueError('Missing Ubuntu mobile launcher')
        if system == 'linux' and not (folder / '后台启动手机局域网安装.sh').is_file():
            raise ValueError('Missing Ubuntu background mobile launcher')
        actual = {str(p.relative_to(folder)) for p in folder.rglob('*') if p.is_file() and p.name != 'manifest.json'}
        if actual != set(files):
            raise ValueError('Package inventory mismatch')
        with zipfile.ZipFile(archive) as z:
            if z.testzip() or set(z.namelist()) != {folder.name + '/' + n for n in [*files, 'manifest.json']}:
                raise ValueError('ZIP inventory mismatch')
            for name in [*files, 'manifest.json']:
                p = folder / name
                if p.is_symlink() or set(Path(name).parts) & {'local', 'data', 'production'} or p.suffix in {'.key', '.keystore', '.jks', '.p12', '.pfx'}:
                    raise ValueError('Private file in bundle')
                data = p.read_bytes()
                if z.read(folder.name + '/' + name) != data or (name in files and sha(data) != files[name]):
                    raise ValueError('Package bytes mismatch: ' + name)
        for name, digest in lock.items():
            if name.startswith(system + '/') and sha((folder / name.split('/', 1)[1]).read_bytes()) != digest:
                raise ValueError('Pinned dependency differs')
        provenance = json.loads((folder / 'payload/client-provenance.json').read_text())
        for name, digest in provenance['source_snapshot'].items():
            if sha((ROOT / name).read_bytes()) != digest:
                raise ValueError('Payload source differs from the integrated source')
        policy = json.loads((folder / 'payload/upgrade-policy.json').read_text())
        for flavor in ['standard', 'guard']:
            p = folder / f'payload/hotel-{flavor}.apk'
            if sha(p.read_bytes()) != provenance['payload'][p.name] or sha(p.read_bytes()) not in policy['allowed_apk_sha256']:
                raise ValueError('Payload policy/provenance mismatch')
            with zipfile.ZipFile(p) as z:
                if json.loads(z.read('assets/hotel-defaults.json')) != expected:
                    raise ValueError('Nonportable APK defaults')
            cert = subprocess.check_output([str(build_tools / 'apksigner'), 'verify', '--print-certs', str(p)], text=True)
            if re.search(r'Signer #1 certificate SHA-256 digest: (\w+)', cert)[1] != SIGNER:
                raise ValueError('Wrong client signer')
            xml = subprocess.check_output([str(build_tools / 'aapt'), 'dump', 'xmltree', str(p), 'AndroidManifest.xml'], text=True)
            if ('android.intent.category.HOME' in xml) != (flavor == 'standard'):
                raise ValueError('Wrong client HOME role')
        executable = folder / ('hotel-tv-installer.exe' if system == 'windows' else 'hotel-tv-installer')
        build_info = subprocess.check_output(['go','version','-m',str(executable)], text=True)
        if 'vcs.revision='+provenance['source_commit'] not in build_info or 'vcs.modified=false' not in build_info:
            raise ValueError('Installer was not built from the clean candidate commit')
        binary = executable.read_bytes()
        if system == 'windows':
            offset = struct.unpack_from('<I', binary, 0x3c)[0]
            if binary[:2] != b'MZ' or binary[offset:offset+4] != b'PE\0\0' or struct.unpack_from('<H', binary, offset+4)[0] != 0x8664:
                raise ValueError('Wrong Windows binary')
        elif system == 'linux':
            if binary[:4] != b'\x7fELF' or struct.unpack_from('<H', binary, 18)[0] != 62:
                raise ValueError('Wrong Linux binary')
        elif binary[:4] != b'\xcf\xfa\xed\xfe' or struct.unpack_from('<I', binary, 4)[0] != (0x100000c if arch == 'arm64' else 0x1000007):
            raise ValueError('Wrong Mac binary')
        results.append({'file': archive.name, 'sha256': sha(archive.read_bytes()), 'files_verified': len(files)})
    report = {'status': 'local-package-verification-passed', 'physical_acceptance': 'pending', 'packages': results}
    (output / 'package-verification.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--build-tools', required=True, type=Path)
    a = parser.parse_args()
    print(json.dumps(verify(a.output.resolve(), a.build_tools.resolve())))
