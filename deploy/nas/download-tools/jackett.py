#!/usr/bin/env python3
"""Install the upstream-recommended LinuxServer Jackett image, independent of download engines."""
import argparse
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import pwd
import secrets
import subprocess
import sys
import time
from urllib.request import build_opener, ProxyHandler, HTTPCookieProcessor

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from docker_images import copy_helper, pull_image

IMAGE = 'linuxserver/jackett@sha256:2c015f0978bb15d006937a0e6926d8a8c4ca955eed5f3372c6ec09a3a2de3ddd'
ROOT = Path('/srv/nas/appdata/jackett')
STACK = Path('/opt/stacks/hotel-jackett')
PLUGIN_URL = 'https://raw.githubusercontent.com/qbittorrent/search-plugins/2beb5c67b0edbf2387a031a02d1881fab4bec3c3/nova3/engines/jackett.py'
PLUGIN_SHA256 = '650b6d24b68e75a2b1a17db5b620f949809a5201e6823b388f7069fe6ca68d66'


def compose(uid, gid, timezone='UTC'):
    return '''services:
  jackett:
    image: IMAGE
    container_name: hotel-jackett
    restart: unless-stopped
    environment:
      PUID: "__UID__"
      PGID: "__GID__"
      TZ: __TZ__
      AUTO_UPDATE: "false"
      UMASK: "077"
    ports: ["127.0.0.1:9117:9117"]
    volumes: ["/srv/nas/appdata/jackett:/config"]
    networks: [default, qb-native]
    deploy:
      resources:
        limits: {cpus: "0.75", memory: 768m}
networks:
  qb-native:
    external: true
    name: hotel-qbittorrent_default
'''.replace('IMAGE', IMAGE).replace('__UID__', str(uid)).replace('__GID__', str(gid)).replace('__TZ__', timezone)


def verify():
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
    for attempt in range(90):
        try:
            with opener.open('http://127.0.0.1:9117/UI/Dashboard', timeout=2) as response:
                assert response.status == 200
            break
        except (OSError, AssertionError):
            if attempt == 89: raise
            time.sleep(.5)
    container = json.loads(subprocess.check_output(['docker', 'inspect', 'hotel-jackett']))[0]
    assert container['Config']['Image'] == IMAGE
    assert container['HostConfig']['PortBindings']['9117/tcp'][0]['HostIp'] == '127.0.0.1'
    assert json.loads((ROOT/'Jackett/ServerConfig.json').read_text())['UpdateDisabled']
    lock = {'image': IMAGE, 'upstream_version': '0.24.2949', 'plugin_url': PLUGIN_URL, 'plugin_sha256': PLUGIN_SHA256,
            'listen': '127.0.0.1:9117', 'memory_limit_mb': 768, 'auto_update': False}
    (STACK / 'upstream.lock.json').write_text(json.dumps(lock, indent=2))
    (STACK / 'jackett.py').write_text(Path(__file__).read_text())
    print('JACKETT_RUNNING_ON_LOOPBACK_EXISTING_QB_NETWORK_UNCHANGED')


def install(timezone='UTC'):
    assert os.geteuid() == 0
    assert not ROOT.exists() and not STACK.exists(), 'Existing Jackett data: inspect rather than overwrite'
    assert subprocess.run(['docker', 'inspect', 'hotel-jackett'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0
    identity = pwd.getpwnam('hotel-tv')
    uid, gid = identity.pw_uid, identity.pw_gid
    config = ROOT / 'Jackett'; config.mkdir(parents=True, mode=0o750)
    ROOT.chmod(0o750)
    for path in (ROOT, config): os.chown(path, uid, gid)
    file = config / 'ServerConfig.json'
    file.write_text(json.dumps({'Port': 9117, 'AllowExternal': True, 'APIKey': secrets.token_hex(16), 'UpdateDisabled': True}, indent=2))
    file.chmod(0o600); os.chown(file, uid, gid)
    STACK.mkdir(parents=True)
    (STACK / 'compose.yaml').write_text(compose(uid, gid, timezone))
    (STACK / 'jackett.py').write_text(Path(__file__).read_text())
    copy_helper(STACK)
    pull_image(IMAGE)
    command = ['docker', 'compose', '-f', str(STACK / 'compose.yaml')]
    subprocess.run(command + ['config', '--quiet'], check=True)
    subprocess.run(command + ['up', '-d'], check=True)
    verify()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--verify',action='store_true');parser.add_argument('--timezone',default='UTC');args=parser.parse_args()
    from zoneinfo import ZoneInfo
    ZoneInfo(args.timezone)
    verify() if args.verify else install(args.timezone)
