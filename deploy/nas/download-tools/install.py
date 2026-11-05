#!/usr/bin/env python3
"""Expose existing native download UIs behind the hotel administrator session.

No replacement download/search service. Run as root after publishing the auth gate.
The official AriaNg archive and official nginx image are pinned below.
"""
import argparse
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from docker_images import copy_helper, pull_image

IMAGE = 'nginx@sha256:47928bf67eb62a1863cb08d9ecb52d3e704304ba614eaee17884204677702951'
ARIA_SHA = 'd2317e441966ad31f1e342cb6a6cc1c51808fb0a10cf6e39cb30b0517b3e7823'
ROOT = Path('/srv/nas/appdata/download-tools')
STACK = Path('/opt/stacks/hotel-download-tools')


def proxy_config(host):
    ipaddress.IPv4Address(host)
    common = '''
    access_log off;
    client_max_body_size 16m;
    add_header Cache-Control "no-store" always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy no-referrer always;
    auth_request /_hotel_admin_auth;
    location = /_hotel_admin_auth {
        internal;
        auth_request off;
        proxy_pass http://127.0.0.1:18791/api/admin/native-tools-auth;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header Cookie $http_cookie;
        proxy_set_header X-Hotel-Native-Port $server_port;
        proxy_set_header X-Hotel-Native-Local-Host $server_addr;
        proxy_set_header X-Hotel-Native-Host $http_host;
        proxy_set_header X-Hotel-Native-Origin $http_origin;
    }
    auth_request_set $native_scheme $upstream_http_x_hotel_native_scheme;
    error_page 401 403 =403 /_hotel_login_required;
    location = /_hotel_login_required {
        internal;
        auth_request off;
        default_type text/html;
        charset utf-8;
        return 403 '<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>管理入口需要验证</title><h1>请检查登录与入口地址</h1><p>请在同一浏览器登录酒店后台，再重新打开此页面。远程访问时，请确认已在 NAS 与应用中保存该工具的公网地址，且与酒店后台使用相同域名和协议。</p></html>';
    }
'''.replace('HOST', host)
    config = '''map $http_cookie $native_cookie {
    default $http_cookie;
    "~*^(.*?)\\bhotel_session=[^;]*(;\\s*)?(.*)$" "$1$3";
}
map $http_origin $native_upstream_origin {
    "" "";
    default "http://127.0.0.1:$server_port";
}
'''
    for port in (8098, 9696, 9117, 5055, 7878, 8989, 6767, 5001, 8095, 8096):
        config += 'server {\n    listen %s:%d;\n    server_name %s;\n' % (host, port, host) + common
        config += '''    location / {
        proxy_pass http://127.0.0.1:PORT;
        proxy_set_header Host 127.0.0.1:PORT;
        proxy_set_header Cookie $native_cookie;
        proxy_set_header Origin $native_upstream_origin;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $native_scheme;
        proxy_redirect http://127.0.0.1:PORT/ $native_scheme://$http_host/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 180s;
        proxy_hide_header Cache-Control;
    }
}
'''.replace('PORT', str(port))
    config += 'server {\n    listen %s:6801;\n    server_name %s;\n' % (host, host) + common
    config += '''    root /usr/share/nginx/html;
    location / { try_files $uri $uri/ /index.html; }
    location = /jsonrpc {
        proxy_pass http://127.0.0.1:6800/jsonrpc;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 180s;
    }
}
'''
    return 'map $http_upgrade $connection_upgrade { default upgrade; "" close; }\n' + config


def aria_defaults(secret):
    defaults = {'language': 'zh_Hans', 'rpcInterface': 'jsonrpc',
                'secret': base64.b64encode(secret.encode()).decode(), 'rpcAlias': '酒店 aria2'}
    return 'angular.module("ariaNg").config(["ariaNgDefaultOptions",function(o){Object.assign(o,' + json.dumps(defaults, ensure_ascii=True) + ');o.rpcPort=location.port||(location.protocol==="https:"?"443":"80");o.protocol=location.protocol.slice(0,-1);}]);\n'


def refresh_proxy(host):
    """Update only the existing proxy and AriaNg bootstrap; retain all application data."""
    assert os.geteuid() == 0, 'root required'
    conf = ROOT / 'default.conf'
    script = ROOT / 'ariang/hotel-defaults.js'
    assert conf.is_file() and script.is_file(), 'existing native proxy required'
    secret = next(line.split('=', 1)[1] for line in Path('/srv/nas/appdata/aria2/aria2.conf').read_text().splitlines() if line.startswith('rpc-secret='))
    old_conf, old_script = conf.read_bytes(), script.read_bytes()
    try:
        conf.write_text(proxy_config(host)); script.write_text(aria_defaults(secret))
        subprocess.run(['docker', 'exec', 'hotel-download-tools', 'nginx', '-c', '/etc/nginx/native.conf', '-t'], check=True)
        subprocess.run(['docker', 'exec', 'hotel-download-tools', 'nginx', '-c', '/etc/nginx/native.conf', '-s', 'reload'], check=True)
    except Exception:
        conf.write_bytes(old_conf); script.write_bytes(old_script)
        subprocess.run(['docker', 'exec', 'hotel-download-tools', 'nginx', '-c', '/etc/nginx/native.conf', '-s', 'reload'], check=False)
        raise
    print('EXISTING_NATIVE_PROXY_REFRESHED_NO_CONTAINERS_RECREATED')


def install(host, archive, check_only=False):
    assert os.geteuid() == 0, 'root required'
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == ARIA_SHA
    ROOT.mkdir(parents=True, exist_ok=True)
    web = ROOT / 'ariang'; web.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        for name in z.namelist():
            dest = (web / name).resolve()
            assert web.resolve() in dest.parents
        z.extractall(web)
    # Configure upstream's native defaults, leaving all user-saved AriaNg settings intact.
    # This file is protected by the same admin gate as /jsonrpc; no secrets in URLs/logs.
    aria_conf = Path('/srv/nas/appdata/aria2/aria2.conf').read_text()
    secret = next(line.split('=', 1)[1] for line in aria_conf.splitlines() if line.startswith('rpc-secret='))
    (web / 'hotel-defaults.js').write_text(aria_defaults(secret))
    index = web / 'index.html'
    index.write_text(index.read_text().replace('</body>', '<script src="hotel-defaults.js"></script></body>'))
    (ROOT / 'default.conf').write_text(proxy_config(host))
    (ROOT / 'nginx.conf').write_text('''worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr warn;
events { worker_connections 512; }
http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    sendfile on;
    client_body_temp_path /tmp/client_temp;
    proxy_temp_path /tmp/proxy_temp;
    include /etc/nginx/conf.d/*.conf;
}
''')
    for path in [ROOT, *ROOT.rglob('*')]:
        os.chown(path, 0, 101)
        path.chmod(0o750 if path.is_dir() else 0o640)
    STACK.mkdir(parents=True, exist_ok=True)
    (STACK / 'install.py').write_text(Path(__file__).read_text())
    copy_helper(STACK)
    (STACK / 'upstream.lock.json').write_text(json.dumps({'nginx_image': IMAGE, 'ariang_version': '1.3.15', 'ariang_zip_sha256': ARIA_SHA}, indent=2))
    (STACK / 'compose.yaml').write_text('''services:
  native-tools:
    image: IMAGE
    container_name: hotel-download-tools
    restart: unless-stopped
    network_mode: host
    user: "101:101"
    entrypoint: ["nginx", "-c", "/etc/nginx/native.conf", "-g", "daemon off;"]
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    tmpfs: ["/tmp:uid=101,gid=101,mode=0700", "/var/cache/nginx:uid=101,gid=101,mode=0700"]
    volumes:
      - /srv/nas/appdata/download-tools/default.conf:/etc/nginx/conf.d/default.conf:ro
      - /srv/nas/appdata/download-tools/nginx.conf:/etc/nginx/native.conf:ro
      - /srv/nas/appdata/download-tools/ariang:/usr/share/nginx/html:ro
    deploy:
      resources:
        limits: {cpus: "0.25", memory: 128m}
'''.replace('IMAGE', IMAGE))
    pull_image(IMAGE)
    compose = ['docker', 'compose', '-f', str(STACK / 'compose.yaml')]
    subprocess.run(compose + ['run', '--rm', '--no-deps', 'native-tools', '-t'], check=True)
    if check_only:
        print('NATIVE_PROXY_IMAGE_CONFIG_AND_UNPRIVILEGED_FILESYSTEM_CHECKED')
        return
    subprocess.run(compose + ['up', '-d'], check=True)
    subprocess.run(['docker', 'exec', 'hotel-download-tools', 'nginx', '-c', '/etc/nginx/native.conf', '-s', 'reload'], check=True)
    print('NATIVE_UI_PROXY_INSTALLED_WITH_ADMIN_AUTH')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--listen', required=True)
    parser.add_argument('--ariang-zip', type=Path)
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--refresh-proxy', action='store_true')
    args = parser.parse_args()
    if args.refresh_proxy:
        refresh_proxy(args.listen)
    else:
        if not args.ariang_zip: parser.error('--ariang-zip is required for installation')
        install(args.listen, args.ariang_zip, args.check_only)
