#!/usr/bin/env python3
"""Prepare the pinned IN19 dependencies from public GitHub Release assets, then install."""
import argparse
import fcntl
import hashlib
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPOSITORY = 'ljianguo703-stack/hotel-media-system-public'
RELEASE = 'IN19-dependencies-20261107'
# Filled from the uploaded assets and verified local OCI archives when publishing.
DEPENDENCIES = json.loads(r'''{
  "schema": 1,
  "release": "IN19-dependencies-20261107",
  "backend_release": "v1-0-0-rc5-pl04",
  "platform": "linux/amd64",
  "docker_requirement": "29+ with containerd image store",
  "images": [
    {
      "name": "jellyfin",
      "file": "jellyfin-IN19.oci.tar",
      "reference": "jellyfin/jellyfin@sha256:fea863d0cd9ec445985d9c75fc6ed64e38a1d2766cf0c250a33d553f4fc1b771",
      "image_id": "sha256:7da5184caa38c553f219f6772476540081f392a68049e2b68c923f6daf877f8e",
      "root_digest": "sha256:fea863d0cd9ec445985d9c75fc6ed64e38a1d2766cf0c250a33d553f4fc1b771",
      "sha256": "9b4ad26c6693b1f94868047596bbc35a06fc8665651ebf2e9eddd43f67dc079f",
      "bytes": 1171423232,
      "asset_id": 718656689
    },
    {
      "name": "dockge",
      "file": "dockge-IN19.oci.tar",
      "reference": "louislam/dockge@sha256:335c6368b880ecc203236ed89e6e5232e0d6578e8ef5920e4a502390451502bf",
      "image_id": "sha256:edee39b74f238378ebfe0d9d12b3833f9d1e123aea9dfaf5149ad45c9afe2527",
      "root_digest": "sha256:335c6368b880ecc203236ed89e6e5232e0d6578e8ef5920e4a502390451502bf",
      "sha256": "d8686db291f32ea9076794471e32a23299cbc82da88f824cfcfb053de6bee7e0",
      "bytes": 649954816,
      "asset_id": 718656797
    },
    {
      "name": "filebrowser",
      "file": "filebrowser-IN19.oci.tar",
      "reference": "ghcr.io/gtsteffaniak/filebrowser@sha256:de2b4c4d18485ea202ab2cdada5bd25d8f0b8ed071712c8216a67194c31285ae",
      "image_id": "sha256:1db4819e84b841e3e33c7958c54e4a2ad8be07a1d8fc255e0ff2829b64fd9d9c",
      "root_digest": "sha256:de2b4c4d18485ea202ab2cdada5bd25d8f0b8ed071712c8216a67194c31285ae",
      "sha256": "530a4349f780a7ae0ae0a894689fc2cf6040f813062613acf00793b34b1a4f40",
      "bytes": 124551168,
      "asset_id": 718656381
    },
    {
      "name": "qbittorrent",
      "file": "qbittorrent-IN19.oci.tar",
      "reference": "qbittorrentofficial/qbittorrent-nox@sha256:43b2d352cf76330a7ca34c7320b9b74227d6f31e662e0e123d0457206b09a7ea",
      "image_id": "sha256:5c859d65d776fe9e0c8ecdf3d42afaeddc7e3cdcb560a630c14e4256c77b64e5",
      "root_digest": "sha256:43b2d352cf76330a7ca34c7320b9b74227d6f31e662e0e123d0457206b09a7ea",
      "sha256": "0b9e561289f958579d7461ce49b12366dcf9f343f4a4289024143ff63372d0fa",
      "bytes": 851272192,
      "asset_id": 718656704
    },
    {
      "name": "npc",
      "file": "npc-IN19.oci.tar",
      "reference": "ghcr.io/djylb/npc:v0.34.10@sha256:64b05938c189edbdcb90865427f6e28e6aeb3e8e38f4a01495b69a0760944812",
      "image_id": "sha256:d7e7929407b7001f7257b5477b551a067770e144a0482311e6fd5b55aa82d2c9",
      "root_digest": "sha256:64b05938c189edbdcb90865427f6e28e6aeb3e8e38f4a01495b69a0760944812",
      "sha256": "2de2059ce78baa0269d8881edc6eb593d04c27d41f1e5fe387679e870a9d1f12",
      "bytes": 80103424,
      "asset_id": 718658141
    },
    {
      "name": "prowlarr",
      "file": "prowlarr-IN19.oci.tar",
      "reference": "lscr.io/linuxserver/prowlarr@sha256:04be30163fbaf48d6aeee7b5893c9f6614ffbad6d853971df0d6dcdf01c7f104",
      "image_id": "sha256:31cb2def3155509d3e1720a59afbe83ea2f3dbe96121d5b3ef24892385bd11c6",
      "root_digest": "sha256:04be30163fbaf48d6aeee7b5893c9f6614ffbad6d853971df0d6dcdf01c7f104",
      "sha256": "46922126c1b24c22bc2a30c334ccb5e84dc3ce341f8a1355a4675fa5cbda06fc",
      "bytes": 168456192,
      "asset_id": 718659474
    },
    {
      "name": "radarr",
      "file": "radarr-IN19.oci.tar",
      "reference": "lscr.io/linuxserver/radarr:version-6.4.4.10685@sha256:084b3b2ab1d0ef930660727259546a241f2bdf67c9ed085d54bf1486a6887c9b",
      "image_id": "sha256:41fdc1672209c2ef31c5260015ec8b96e3c75513e60a667eb052e1ff54b4cfa9",
      "root_digest": "sha256:084b3b2ab1d0ef930660727259546a241f2bdf67c9ed085d54bf1486a6887c9b",
      "sha256": "55bebfde3ad93ac30ef49075aaa39be9f5550fc02e80f4f338f1c29473d5cff4",
      "bytes": 187802624,
      "asset_id": 718659567
    },
    {
      "name": "sonarr",
      "file": "sonarr-IN19.oci.tar",
      "reference": "lscr.io/linuxserver/sonarr:version-4.0.20.3014@sha256:48e36b608b461273b9dae857725cd0561a127f2c694b687318f6d0c5ce63e5c9",
      "image_id": "sha256:a674ae620ca7ce1246b377bcac13a7500567196cf2207059e68146fa4dc169f7",
      "root_digest": "sha256:48e36b608b461273b9dae857725cd0561a127f2c694b687318f6d0c5ce63e5c9",
      "sha256": "9db9bdc48785496df2443a6a0a496c2c39fa4e296ae76f58be661d0cf16bb099",
      "bytes": 179524096,
      "asset_id": 718659767
    },
    {
      "name": "bazarr",
      "file": "bazarr-IN19.oci.tar",
      "reference": "lscr.io/linuxserver/bazarr:version-v1.6.2@sha256:9a625883f054e722795affe5e3ca71057e6ed817ecdf99f4a99624346fcc509c",
      "image_id": "sha256:7764771b044aa065ffc89c1d2c4512dbd545cce63a5ca0def626535ac1571282",
      "root_digest": "sha256:9a625883f054e722795affe5e3ca71057e6ed817ecdf99f4a99624346fcc509c",
      "sha256": "8cb051d9ef317649c45e62dbaea54d93f2a201e68982d5c67cd495225b270ca6",
      "bytes": 315165696,
      "asset_id": 718660246
    },
    {
      "name": "seerr",
      "file": "seerr-IN19.oci.tar",
      "reference": "ghcr.io/seerr-team/seerr:v3.5.1@sha256:d8fab675b55ad293ded98fd71efcfd46054cbad9746534ae1cc4794a88c0b3be",
      "image_id": "sha256:2636191726a315dbd75228537f7be74b2fa7498998bb22e37b5b5c8756018fba",
      "root_digest": "sha256:d8fab675b55ad293ded98fd71efcfd46054cbad9746534ae1cc4794a88c0b3be",
      "sha256": "53f3ab3f414a833fc1bad4bce592271cc014824abe7018ac05836d09ec6e9442",
      "bytes": 562356224,
      "asset_id": 718661353
    },
    {
      "name": "alpine",
      "file": "alpine-IN19.oci.tar",
      "reference": "alpine@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc",
      "image_id": "sha256:c64c687cbea9300178b30c95835354e34c4e4febc4badfe27102879de0483b5e",
      "root_digest": "sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc",
      "sha256": "5aaec5db2d6fa80cda4c26b3bfc29d322a33c811d8e49ed67b2e660f411dd505",
      "bytes": 28723200,
      "asset_id": 718661006
    },
    {
      "name": "jackett",
      "file": "jackett-IN19.oci.tar",
      "reference": "linuxserver/jackett@sha256:2c015f0978bb15d006937a0e6926d8a8c4ca955eed5f3372c6ec09a3a2de3ddd",
      "image_id": "sha256:8e5ede4cdb8b3e68997398be184ee1c1d1a3901a4f46d6f9a47d5f4f5157ee50",
      "root_digest": "sha256:2c015f0978bb15d006937a0e6926d8a8c4ca955eed5f3372c6ec09a3a2de3ddd",
      "sha256": "9a950f251add1e2d63f5683bb5b64194715034ffff2a20613d46e077fb335af5",
      "bytes": 164130304,
      "asset_id": 718661286
    },
    {
      "name": "nginx",
      "file": "nginx-IN19.oci.tar",
      "reference": "nginx@sha256:47928bf67eb62a1863cb08d9ecb52d3e704304ba614eaee17884204677702951",
      "image_id": "sha256:84ed8c0dd296c188bfc24ed506f46589e9285fb0b11cf3a04d91d524d3067e11",
      "root_digest": "sha256:47928bf67eb62a1863cb08d9ecb52d3e704304ba614eaee17884204677702951",
      "sha256": "83b32430413b40dfba43c3a6f5757bcb045c4c555f5b1b40846be38a01eec1bb",
      "bytes": 203415040,
      "asset_id": 718662555
    },
    {
      "name": "aria2",
      "file": "aria2-IN19.oci.tar",
      "reference": "hotel-aria2:1.37",
      "image_id": "sha256:0c14ad2ac0db9d7d7e77cbcaec06da74064bdbe278483da3ece0c99b8c265036",
      "root_digest": "sha256:0c14ad2ac0db9d7d7e77cbcaec06da74064bdbe278483da3ece0c99b8c265036",
      "sha256": "6a13e22a6ea824e114542a6ba287bcbcc48f611df499effa1d83584a1dc2fd8a",
      "bytes": 9578496,
      "asset_id": 718662332
    }
  ],
  "aria2_image_id": "sha256:0c14ad2ac0db9d7d7e77cbcaec06da74064bdbe278483da3ece0c99b8c265036",
  "docker_key": {
    "file": "docker-official.asc",
    "asset_id": 718577247,
    "sha256": "1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570",
    "bytes": 3817
  },
  "native_files": [
    {
      "file": "AriaNg-1.3.15.zip",
      "asset_id": 718501181,
      "sha256": "d2317e441966ad31f1e342cb6a6cc1c51808fb0a10cf6e39cb30b0517b3e7823",
      "bytes": 1161055,
      "destination": "/var/lib/hotel-tv-setup/downloads/AriaNg.zip"
    },
    {
      "file": "jackett-plugin.py",
      "asset_id": 718501218,
      "sha256": "650b6d24b68e75a2b1a17db5b620f949809a5201e6823b388f7069fe6ca68d66",
      "bytes": 11886,
      "destination": "/var/lib/hotel-tv-setup/downloads/jackett-plugin.py"
    }
  ],
  "installer": {
    "file": "hotel-media-system-v1-0-0-rc5-pl04-IN19-public-r3.run",
    "asset_id": 718605060,
    "sha256": "847d276c53cacbf744c4e72b125bfdaa091ea65a077ea59d1ad6006f912fedba",
    "bytes": 9557353
  }
}''')
CACHE = Path('/var/cache/hotel-tv-dependencies') / RELEASE
STATE = Path('/etc/hotel-tv/install-state.json')


def run(arguments, **kwargs):
    return subprocess.run([str(a) for a in arguments], check=True, **kwargs)


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def download(asset):
    target = CACHE / asset['file']
    if target.is_file() and sha256(target) == asset['sha256']:
        print('已校验缓存', target.name, flush=True)
        return target
    url = f"https://api.github.com/repos/{REPOSITORY}/releases/assets/{asset['asset_id']}"
    partial = target.with_name(target.name + '.partial')
    if partial.exists() and partial.stat().st_size == asset['bytes'] and sha256(partial) == asset['sha256']:
        partial.replace(target)
        return target
    for attempt in range(3):
        try:
            offset = partial.stat().st_size if partial.exists() else 0
            if offset >= asset['bytes']:
                partial.unlink()
                offset = 0
            headers = {'Accept': 'application/octet-stream', 'User-Agent': 'Hotel-IN19-Dependencies'}
            if offset:
                headers['Range'] = f'bytes={offset}-'
            request = Request(url, headers=headers)
            print(f"下载 {target.name}（{offset / 1048576:.1f}/{asset['bytes'] / 1048576:.1f} MiB，第 {attempt + 1}/3 次）", flush=True)
            with urlopen(request, timeout=60) as response:
                append = offset > 0 and response.status == 206
                if append and not response.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
                    raise RuntimeError('下载续传范围不符：' + target.name)
                with partial.open('ab' if append else 'wb') as stream:
                    received = offset if append else 0
                    reported = time.monotonic()
                    for block in iter(lambda: response.read(1024 * 1024), b''):
                        stream.write(block)
                        received += len(block)
                        if time.monotonic() - reported >= 10:
                            print(f'{target.name} {received / 1048576:.1f}/{asset["bytes"] / 1048576:.1f} MiB', flush=True)
                            reported = time.monotonic()
            if partial.stat().st_size < asset['bytes']:
                raise ConnectionError('下载中断')
            if partial.stat().st_size != asset['bytes'] or sha256(partial) != asset['sha256']:
                partial.unlink()
                raise RuntimeError('依赖文件大小或 SHA256 不符，下载停止：' + target.name)
            partial.replace(target)
            return target
        except (HTTPError, URLError, TimeoutError, ConnectionError, IncompleteRead) as error:
            if isinstance(error, HTTPError):
                status = f'HTTP {error.code}'
            elif isinstance(error, URLError):
                status = f'{type(error.reason).__name__} {error.reason}'
            else:
                status = type(error).__name__
            print(f'GitHub API 下载失败 {target.name}，{status}', file=sys.stderr, flush=True)
            if isinstance(error, HTTPError) and error.code == 416:
                partial.unlink(missing_ok=True)
            if attempt == 2:
                raise RuntimeError('下载未完成，修正 GitHub API 网络后重跑同一命令：' + target.name) from None
            time.sleep(3)


def docker_identity(reference):
    result = subprocess.run(['docker', 'image', 'inspect', '--platform', 'linux/amd64', reference],
                            capture_output=True, text=True)
    if result.returncode:
        return None
    value = json.loads(result.stdout)[0]
    return value if value['Os'] == 'linux' and value['Architecture'] == 'amd64' else None


def prepare_docker():
    if not shutil.which('docker'):
        key = download(DEPENDENCIES['docker_key'])
        run(['apt-get', '-o', 'DPkg::Lock::Timeout=180', 'update'])
        run(['apt-get', '-o', 'DPkg::Lock::Timeout=180', 'install', '-y', 'ca-certificates'])
        source = Path('/etc/apt/sources.list.d/docker.sources')
        content = ('Types: deb\nURIs: https://mirrors.tuna.tsinghua.edu.cn/docker-ce/linux/ubuntu\n'
                   'Suites: noble\nComponents: stable\nArchitectures: amd64\n'
                   'Signed-By: /etc/apt/keyrings/docker.asc\n')
        if source.exists() and source.read_text() != content:
            raise RuntimeError('已有不同的 Docker APT 源，请先核对 /etc/apt/sources.list.d/docker.sources')
        run(['install', '-D', '-m', '0644', key, '/etc/apt/keyrings/docker.asc'])
        source.write_text(content)
        source.chmod(0o644)
        run(['apt-get', '-o', 'DPkg::Lock::Timeout=180', '-o', 'Acquire::Retries=3', 'update'])
        run(['apt-get', '-o', 'DPkg::Lock::Timeout=180', '-o', 'Acquire::Retries=3', 'install', '-y',
             'docker-ce', 'docker-ce-cli', 'containerd.io', 'docker-buildx-plugin', 'docker-compose-plugin'])
        run(['systemctl', 'enable', '--now', 'docker'])
    run(['docker', 'compose', 'version'])
    version = subprocess.check_output(['docker', 'version', '--format', '{{.Server.Version}}'], text=True).strip()
    driver = subprocess.check_output(['docker', 'info', '--format', '{{json .DriverStatus}}'], text=True)
    if int(version.split('.')[0]) < 29 or 'io.containerd.snapshotter.v1' not in driver:
        raise RuntimeError('本 OCI 依赖包要求 Docker 29+ 的 containerd 镜像存储，请先核对 Docker 配置')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-only', action='store_true', help='准备和校验依赖后退出')
    parser.add_argument('--plan', action='store_true', help='查看依赖清单')
    parser.add_argument('--profile', choices=('full',), default='full')
    args, installer_arguments = parser.parse_known_args()
    if args.plan:
        print(json.dumps(DEPENDENCIES, ensure_ascii=False, indent=2))
        return
    if os.geteuid() != 0:
        raise RuntimeError('请使用 sudo python3 install-from-release.py')
    release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    if release.get('ID', '').strip('"') != 'ubuntu' or release.get('VERSION_ID', '').strip('"') != '24.04' or platform.machine() != 'x86_64':
        raise RuntimeError('本依赖入口适用于 Ubuntu 24.04 amd64')
    if STATE.exists():
        state = json.loads(STATE.read_text())
        if state['release'] != DEPENDENCIES['backend_release'] or state['profile'] != args.profile:
            raise RuntimeError('安装状态与本依赖包版本或 full profile 不一致')
    elif Path('/opt/hotel-tv/current').exists() or Path('/srv/hotel-tv/data/events.sqlite3').exists():
        raise RuntimeError('发现已有影院，请按原后台软件更新流程维护')
    CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ['DEBIAN_FRONTEND'] = 'noninteractive'
    with (CACHE / 'install.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('另一个依赖准备任务正在执行') from None
        prepare_docker()
        for image in DEPENDENCIES['images']:
            identity = docker_identity(image['reference'])
            if identity and identity['Id'] == image['image_id']:
                print('已验证本机镜像', image['name'], flush=True)
                continue
            archive = download(image)
            run(['docker', 'load', '--platform', 'linux/amd64', '--input', archive])
            identity = docker_identity(image['reference'])
            if not identity or identity['Id'] != image['image_id']:
                raise RuntimeError('镜像导入后的固定摘要、身份或架构不符：' + image['name'])
        for asset in DEPENDENCIES['native_files']:
            path = download(asset)
            run(['install', '-D', '-m', '0600', path, asset['destination']])
        installer = download(DEPENDENCIES['installer'])
        if args.cache_only:
            print('全部依赖和安装包已校验，缓存位置 ' + str(CACHE), flush=True)
            return
        environment = dict(os.environ, HOTEL_PREBUILT_ARIA2_ID=DEPENDENCIES['aria2_image_id'])
        run(['bash', installer, '--profile', args.profile, '--resume', *installer_arguments], env=environment)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as error:
        message = str(error) if isinstance(error, (RuntimeError, ValueError)) else type(error).__name__
        print('依赖安装未完成，' + message, file=sys.stderr)
        sys.exit(1)
