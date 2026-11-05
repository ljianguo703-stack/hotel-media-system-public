#!/usr/bin/env python3
"""Optional download engines for 智能下载 on the hotel Ubuntu (same host, Docker Compose, loopback-only ports).

    sudo python3 smart_download.py prepare [--uid N --gid N] [--with-prowlarr]   # dirs, compose files, aria2 image, pinned pulls
    sudo python3 smart_download.py start | stop | status
    sudo python3 smart_download.py set-qb-password < password.txt               # replaces qBittorrent's temporary password

Never touches existing stacks, ports, media or the hotel service. Secrets stay in root-only files; nothing is printed.
Paths can be overridden (--stacks/--root/--media) for a dry run on a test machine."""
import argparse
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from docker_images import pull_image

QB_IMAGE = 'qbittorrentofficial/qbittorrent-nox@sha256:43b2d352cf76330a7ca34c7320b9b74227d6f31e662e0e123d0457206b09a7ea'  # 5.3.0, tested DL03
ALPINE = 'alpine@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc'  # 3.20, aria2 1.37.0 tested DL03
PROWLARR = 'lscr.io/linuxserver/prowlarr@sha256:04be30163fbaf48d6aeee7b5893c9f6614ffbad6d853971df0d6dcdf01c7f104'  # pinned to the pulled digest at prepare time (untested in DL03)
QB_PORT, ARIA_PORT, PROWLARR_PORT = 8098, 6800, 9696
STACKS = {'qbittorrent': 'hotel-qbittorrent', 'aria2': 'hotel-aria2', 'prowlarr': 'hotel-prowlarr'}


def run(cmd, **kw): return subprocess.run(cmd, check=True, **kw)


def write(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() != text and '--force' not in sys.argv: raise SystemExit('已存在且内容不同，未覆盖：%s（确认后加 --force）' % path)
    path.write_text(text); os.chmod(path, mode)


def compose_qb(a):
    return ('''services:
  qbittorrent:
    image: %s
    container_name: hotel-qbittorrent
    restart: unless-stopped
    environment: {QBT_LEGAL_NOTICE: confirm, QBT_WEBUI_PORT: "%d", QBT_TORRENTING_PORT: "6881", PUID: "%d", PGID: "%d", UMASK: "002", TZ: __TZ__}   # group-writable: the cinema service may hardlink downloads (protected_hardlinks)
    ports: ["127.0.0.1:%d:%d"]   # WebUI on loopback only; same port inside/outside (qBittorrent validates the Host port)
    volumes: ["%s/appdata/qbittorrent:/config", "%s/.downloads:/downloads"]
    deploy: {resources: {limits: {cpus: "1.0", memory: 1g}}}
''' % (QB_IMAGE, QB_PORT, a.uid, a.gid, QB_PORT, QB_PORT, a.root, a.media)).replace('__TZ__', a.timezone)


def compose_aria2(a):
    return ('''services:
  aria2:
    build: .
    image: hotel-aria2:1.37
    container_name: hotel-aria2
    restart: unless-stopped
    user: "%d:%d"
    ports: ["127.0.0.1:%d:6800"]
    volumes: ["%s/appdata/aria2:/config", "%s/.downloads:/downloads"]
    deploy: {resources: {limits: {cpus: "1.0", memory: 512m}}}
''' % (a.uid, a.gid, ARIA_PORT, a.root, a.media))


DOCKERFILE = '''FROM %s
RUN apk add --no-cache aria2 && aria2c --version | head -1
CMD ["aria2c", "--conf-path=/config/aria2.conf"]
''' % ALPINE


def compose_prowlarr(a, image):
    return ('''services:
  prowlarr:
    image: %s
    container_name: hotel-prowlarr
    restart: unless-stopped
    environment: {PUID: "%d", PGID: "%d", TZ: __TZ__, PROWLARR__AUTH__METHOD: External}   # behind the hotel admin gateway
    ports: ["127.0.0.1:%d:9696"]   # owner reaches it through an SSH tunnel; not exposed to guest networks
    volumes: ["%s/appdata/prowlarr:/config"]
    deploy: {resources: {limits: {cpus: "1.0", memory: 1g}}}
''' % (image, a.uid, a.gid, PROWLARR_PORT, a.root)).replace('__TZ__', a.timezone)


def prepare(a):
    stage = Path(a.media) / '.downloads'
    for d in (stage, Path(a.root) / 'appdata' / 'qbittorrent', Path(a.root) / 'appdata' / 'aria2'):
        d.mkdir(parents=True, exist_ok=True); os.chown(d, a.uid, a.gid) if os.geteuid() == 0 else None
    conf = Path(a.root) / 'appdata' / 'aria2' / 'aria2.conf'
    if not conf.exists():
        secret = secrets.token_hex(24)
        write(conf, '\n'.join(['enable-rpc=true', 'rpc-listen-all=true', 'rpc-listen-port=6800', 'rpc-secret=' + secret, 'dir=/downloads',
                               'save-session=/config/aria2.session', 'input-file=/config/aria2.session', 'save-session-interval=30',
                               'continue=true', 'max-concurrent-downloads=3', 'max-connection-per-server=8', 'file-allocation=none', 'disk-cache=32M']) + '\n', 0o600)
        (conf.parent / 'aria2.session').touch()
        if os.geteuid() == 0: os.chown(conf, a.uid, a.gid); os.chown(conf.parent / 'aria2.session', a.uid, a.gid)
    stacks = Path(a.stacks)
    write(stacks / STACKS['qbittorrent'] / 'compose.yaml', compose_qb(a))
    write(stacks / STACKS['aria2'] / 'compose.yaml', compose_aria2(a)); write(stacks / STACKS['aria2'] / 'Dockerfile', DOCKERFILE)
    pull_image(QB_IMAGE)
    pull_image(ALPINE)
    run(['docker', 'compose', '-f', str(stacks / STACKS['aria2'] / 'compose.yaml'), 'build'])
    lock = {'qbittorrent': QB_IMAGE, 'aria2_base': ALPINE}
    if a.with_prowlarr:
        pull_image(PROWLARR)
        digest = subprocess.run(['docker', 'image', 'inspect', PROWLARR, '--format', '{{index .RepoDigests 0}}'], capture_output=True, text=True, check=True).stdout.strip()
        (Path(a.root) / 'appdata' / 'prowlarr').mkdir(parents=True, exist_ok=True)
        write(stacks / STACKS['prowlarr'] / 'compose.yaml', compose_prowlarr(a, digest)); lock['prowlarr'] = digest
    write(Path(a.root) / 'smart-download-images.lock.json', json.dumps(lock, indent=1) + '\n')
    print('已准备。后台“智能下载 → 连接与策略设置”填写：qBittorrent http://127.0.0.1:%d（下载器内目录 /downloads，本机目录 %s），'
          'aria2 http://127.0.0.1:%d/jsonrpc（目录同上，密钥在受保护的 %s 中，权限 0600）。' % (QB_PORT, stage, ARIA_PORT, conf))


def compose(a, *args):
    for name in ('qbittorrent', 'aria2', 'prowlarr'):
        file = Path(a.stacks) / STACKS[name] / 'compose.yaml'
        if file.exists(): run(['docker', 'compose', '-f', str(file)] + list(args))


def set_qb_password(a):
    password = sys.stdin.readline().strip()
    if len(password) < 12: raise SystemExit('密码至少 12 位（从标准输入读取，不会回显或保存到日志）')
    base = 'http://127.0.0.1:%d' % QB_PORT; op = build_opener(ProxyHandler({}))
    def login(pw):
        try: r = op.open(Request(base + '/api/v2/auth/login', data=urlencode({'username': 'admin', 'password': pw}).encode(), headers={'Referer': base + '/', 'Origin': base}), timeout=10)
        except HTTPError: return None
        cookie = r.headers.get('Set-Cookie', '').split(';')[0]
        return cookie if r.status in (200, 204) and 'SID' in cookie else None
    if login(password):
        print('qBittorrent 已使用本次安装器保存的密码');return
    logs = subprocess.run(['docker', 'logs', 'hotel-qbittorrent'], capture_output=True, text=True)
    temporary = re.findall(r'temporary password is provided for this session: (\S+)', logs.stdout + logs.stderr)
    sid = login(temporary[-1]) if temporary else None
    if not sid: raise SystemExit('无法用临时密码登录（可能已设置过密码）；请在 qBittorrent 网页中修改')
    op.open(Request(base + '/api/v2/app/setPreferences', data=urlencode({'json': json.dumps({'web_ui_password': password})}).encode(),
                    headers={'Referer': base + '/', 'Origin': base, 'Cookie': sid}), timeout=10).read()
    print('qBittorrent 密码已设置；请在后台智能下载设置中填写同一密码。' if login(password) else '设置后登录验证失败，请检查')


def main():
    p = argparse.ArgumentParser(); p.add_argument('command', choices=['prepare', 'start', 'stop', 'status', 'set-qb-password'])
    p.add_argument('--stacks', default='/opt/stacks'); p.add_argument('--root', default='/srv/nas'); p.add_argument('--media', default='/srv/hotel-tv/media')
    p.add_argument('--uid', type=int, default=os.getuid()); p.add_argument('--gid', type=int, default=os.getgid()); p.add_argument('--with-prowlarr', action='store_true')
    p.add_argument('--force', action='store_true'); p.add_argument('--timezone', default='UTC')
    a = p.parse_args()
    from zoneinfo import ZoneInfo
    ZoneInfo(a.timezone)
    if a.command == 'prepare': prepare(a)
    elif a.command == 'start': compose(a, 'up', '-d')
    elif a.command == 'stop': compose(a, 'stop')
    elif a.command == 'status': compose(a, 'ps')
    else: set_qb_password(a)


if __name__ == '__main__':
    main()
