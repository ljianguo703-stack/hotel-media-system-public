#!/usr/bin/env python3
"""Build the current Go installer with pinned runtime files and the candidate's own APKs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_files(root, files):
    actual = {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()}
    if actual != set(files):
        raise ValueError('Runtime dependency inventory differs from the reviewed lock')
    for name, expected in files.items():
        rel = Path(name)
        if rel.is_absolute() or '..' in rel.parts or (root / rel).is_symlink() or sha(root / rel) != expected:
            raise ValueError('Invalid runtime dependency: ' + name)


def build(runtime, payload, output, jdk=None, sdk=None, helper_platform="android-34"):
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT):
        raise ValueError('Commit reviewed source before building the installer')
    runtime, payload, output = Path(runtime).resolve(), Path(payload).resolve(), Path(output).resolve()
    lock = json.loads((ROOT / 'third-party/tv-runtime.lock.json').read_text())
    verify_files(runtime, lock['files'])
    provenance = json.loads((payload / 'client-provenance.json').read_text())
    if {p.name for p in payload.iterdir()} != {'hotel-standard.apk','hotel-guard.apk','client-provenance.json','upgrade-policy.json'}:
        raise ValueError('Unexpected file in payload')
    for name, digest in provenance['source_snapshot'].items():
        if sha(ROOT / name) != digest:
            raise ValueError('Client source differs from current integrated source: ' + name)
    policy = json.loads((payload / 'upgrade-policy.json').read_text())
    for name in ('hotel-standard.apk', 'hotel-guard.apk'):
        if provenance['payload'][name] != sha(payload / name) or sha(payload / name) not in policy['allowed_apk_sha256']:
            raise ValueError('APK provenance or upgrade policy mismatch')
        with zipfile.ZipFile(payload / name) as archive:
            defaults = json.loads(archive.read('assets/hotel-defaults.json'))
            if defaults != json.loads((ROOT / 'deploy/android/hotel-defaults.example.json').read_text()):
                raise ValueError('Site configuration must never enter a TV bundle')
    if output.exists():
        raise ValueError('Output already exists; preserve the prior candidate')
    version = re.search(r'const version = "([0-9.]+)"', (ROOT / 'tv-installer/config.go').read_text())[1]
    jdk = jdk or os.environ.get('JAVA_HOME')
    sdk = sdk or os.environ.get('ANDROID_HOME') or os.environ.get('ANDROID_SDK_ROOT')
    if not jdk or not sdk:
        raise ValueError('Building the power helper requires --jdk and --sdk (build machine only)')
    output.mkdir(parents=True)
    helper = output / 'helper-build' / 'power-mode.jar'
    subprocess.run([sys.executable, str(ROOT / 'tools/tv_power_mode/build.py'),
                    '--jdk', str(jdk), '--sdk', str(sdk), '--platform', helper_platform, '--output', str(helper)], check=True)
    helper_sources = ['tools/tv_power_mode/PowerModeTool.java', 'tools/tv_power_mode/build.py']
    helper_provenance = {'schema': 1, 'profile': 'xiaomi-mulan-pstaatv-104-pw01', 'min_api': 28,
                         'compile_platform': helper_platform, 'source_snapshot': {n: sha(ROOT / n) for n in helper_sources},
                         'jar_sha256': sha(helper), 'manufacturer_binaries_included': False}
    release = output / 'release'
    release.mkdir()
    packages = []
    for system, arch in [('darwin', 'arm64'), ('darwin', 'amd64'), ('linux', 'amd64'), ('windows', 'amd64')]:
        name = f'hotel-tv-installer-{version}-{system}-{arch}'
        folder = release / name
        shutil.copytree(runtime / system, folder)
        exe = 'hotel-tv-installer.exe' if system == 'windows' else 'hotel-tv-installer'
        subprocess.run(['go', 'build', '-trimpath', '-ldflags=-s -w', '-o', str(folder / exe), '.'],
                       cwd=ROOT / 'tv-installer', env={**os.environ, 'GOOS': system, 'GOARCH': arch, 'CGO_ENABLED': '0'}, check=True)
        shutil.copytree(payload, folder / 'payload')
        (folder / 'helpers').mkdir()
        shutil.copy2(helper, folder / 'helpers/power-mode.jar')
        (folder / 'helpers/provenance.json').write_text(json.dumps(helper_provenance, indent=2) + '\n')
        for n in ['catalog.json', 'profile-provenance.json', '机型支持清单.csv', '使用功能详解.txt', '开源研究与支持边界.md', '手机安装使用说明.md', '设备连接开启方式.md', '开源借鉴与版本差异.md', 'connection-guides.json']:
            shutil.copy2(ROOT / 'tv-installer' / n, folder / n)
        shutil.copy2(ROOT / 'tv-installer/先看这里.txt', folder / '先看这里.txt')
        (folder / 'docs').mkdir()
        for n in ['BUILD.md','INSTALL.md','USAGE.md','CHANGELOG-IN20.md']:
            shutil.copy2(ROOT / 'docs' / n, folder / 'docs' / n)
        for n in ['upstream-2026-11-03.json', 'community-index.json']:
            shutil.copy2(ROOT / 'tv-installer/source' / n, folder / 'docs' / n)
        if system == 'windows':
            (folder / '启动电视安装工具.bat').write_bytes(b'@echo off\r\ncd /d "%~dp0"\r\nhotel-tv-installer.exe\r\nif errorlevel 1 pause\r\n')
        else:
            launcher = folder / ('启动电视安装工具.command' if system == 'darwin' else '启动电视安装工具.sh')
            launcher.write_text('#!/bin/sh\ncd -- "$(dirname -- "$0")" || exit 1\nchmod +x ./hotel-tv-installer ./platform-tools/adb\nexec ./hotel-tv-installer\n')
            launcher.chmod(0o755)
            (folder / 'platform-tools/adb').chmod(0o755)
            if system == 'linux':
                mobile = folder / '启动手机局域网安装.sh'
                mobile.write_text('#!/bin/sh\nset -eu\ncd -- "$(dirname -- "$0")"\nchmod +x ./hotel-tv-installer ./platform-tools/adb\nexec ./hotel-tv-installer --lan --no-browser --state "${HOTEL_INSTALLER_STATE:-$PWD/data}" "$@"\n')
                mobile.chmod(0o755)
                background = folder / '后台启动手机局域网安装.sh'
                shutil.copy2(ROOT / 'tv-installer/tools/start_mobile_background.sh', background)
                background.chmod(0o755)
        files = {str(p.relative_to(folder)): sha(p) for p in sorted(folder.rglob('*')) if p.is_file()}
        source_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        manifest = {'version': version, 'source_commit': source_commit, 'status': 'candidate-awaiting-physical-acceptance', 'files': files}
        (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
        target = release / (name + '.zip')
        with zipfile.ZipFile(target, 'x', zipfile.ZIP_DEFLATED) as archive:
            for p in sorted(folder.rglob('*')):
                if p.is_file(): archive.write(p, str(p.relative_to(release)))
        with zipfile.ZipFile(target) as archive:
            if archive.testzip(): raise ValueError('Invalid ZIP: ' + target.name)
        packages.append({'file': target.name, 'sha256': sha(target), 'files': len(files)})
    (release / 'SHA256SUMS.txt').write_text(''.join(x['sha256'] + '  ' + x['file'] + '\n' for x in packages))
    (output / 'verification.json').write_text(json.dumps({'version': version, 'packages': packages}, indent=2) + '\n')
    return packages


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--payload', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--jdk', type=Path)
    parser.add_argument('--sdk', type=Path)
    parser.add_argument('--helper-platform', default='android-34')
    args = parser.parse_args()
    print(json.dumps(build(args.runtime, args.payload, args.output, args.jdk, args.sdk, args.helper_platform), indent=2))
