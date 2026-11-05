#!/usr/bin/env python3
"""Configure the existing NAS and native download applications on a fresh host."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy'))
from bootstrap import apt, install_docker, private_json, service_identity
from docker_images import pull_image


def command(args, password=None):
    return subprocess.run([str(a) for a in args], input=password + '\n' if password is not None else None, text=True, check=True)


def wait_file(path):
    for _ in range(120):
        if path.is_file() and path.stat().st_size: return
        time.sleep(.5)
    raise RuntimeError('应用尚未生成初始化配置：' + str(path))


def connect_downloads(state):
    sys.path.insert(0, '/opt/hotel-tv/current/server')
    from management import Store
    config = Path('/srv/nas/appdata/prowlarr/config.xml'); wait_file(config)
    key = ET.fromstring(config.read_text()).findtext('ApiKey')
    if not key: raise RuntimeError('Prowlarr API key 尚未就绪')
    conf = Path('/srv/nas/appdata/aria2/aria2.conf').read_text()
    aria = next(l.split('=', 1)[1] for l in conf.splitlines() if l.startswith('rpc-secret='))
    body = {'settings': {'auto_mode': False, 'auto_publish': False, 'min_free_gb': 5,
                        'staging_dir': '/srv/hotel-tv/media/.downloads', 'save_dir': '/srv/hotel-tv/media/.downloads',
                        'engines': {'qbittorrent': {'enabled': True, 'url': 'http://127.0.0.1:8098', 'username': 'admin', 'directory': '/downloads', 'host_directory': '/srv/hotel-tv/media/.downloads'},
                                    'aria2': {'enabled': True, 'url': 'http://127.0.0.1:6800/jsonrpc', 'directory': '/downloads', 'host_directory': '/srv/hotel-tv/media/.downloads'}},
                        'sources': [{'id': 'prowlarr', 'name': 'Prowlarr', 'type': 'prowlarr', 'url': 'http://127.0.0.1:9696', 'enabled': True, 'timeout': 20}]},
            'secrets': {'qbittorrent': {'password': state.get('service_passwords',{}).get('qbittorrent',state['nas_password'])}, 'aria2': {'secret': aria}, 'sources': {'prowlarr': key}}}
    with service_identity():
        store = Store(Path('/srv/hotel-tv/data'), Path('/srv/hotel-tv/media'), Path('/etc/hotel-tv/devices.json'), [Path('/srv/hotel-tv/media'), Path('/mnt'), Path('/media')])
        with store.db() as db: revision = store.kv(db, 'revision')
        store.save_download_settings(body, {'name': 'system:安装器', 'role': 'admin'}, revision)


def download(url, path, expected):
    path = Path(path)
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == expected: return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent); os.close(fd)
    try:
        # Use curl's bounded transport retry for fixed public installer assets, not media tasks.
        subprocess.run(['curl', '--fail', '--silent', '--show-error', '--location', '--retry', '3',
                        '--retry-all-errors', '--retry-delay', '1', '--connect-timeout', '15',
                        '--max-time', '180', '--max-filesize', '16777216', '--proto-redir', '=https',
                        '--output', temporary, url], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if hashlib.sha256(Path(temporary).read_bytes()).hexdigest() != expected:
            raise RuntimeError('上游文件校验不符，未执行：' + path.name)
        os.chmod(temporary, 0o600); os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def native_tools(state):
    folder = Path('/var/lib/hotel-tv-setup/downloads'); folder.mkdir(parents=True, exist_ok=True)
    archive = folder / 'AriaNg.zip'
    download('https://github.com/mayswind/AriaNg/releases/download/1.3.15/AriaNg-1.3.15.zip', archive, 'd2317e441966ad31f1e342cb6a6cc1c51808fb0a10cf6e39cb30b0517b3e7823')
    command([sys.executable, ROOT / 'deploy/nas/download-tools/install.py', '--listen', state['host'], '--ariang-zip', archive])


def plugin():
    sys.path.insert(0, str(ROOT / 'deploy/nas/download-tools'))
    import jackett
    path = Path('/var/lib/hotel-tv-setup/downloads/jackett-plugin.py')
    download(jackett.PLUGIN_URL, path, jackett.PLUGIN_SHA256)
    command([sys.executable, ROOT / 'deploy/nas/download-tools/jackett_configure.py', '--plugin', path, '--report', '/var/lib/hotel-tv-setup/jackett-report.json'])


def remove_bootstrap_environment():
    path = Path('/opt/stacks/filebrowser/compose.yaml'); value = json.loads(path.read_text())
    value['services']['filebrowser']['environment'].pop('FILEBROWSER_ADMIN_PASSWORD', None)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    Path('/opt/stacks/filebrowser/.env').unlink(missing_ok=True)
    command(['docker', 'compose', '-f', path, 'up', '-d'])


def main():
    p = argparse.ArgumentParser(); p.add_argument('--state', type=Path, required=True); args = p.parse_args()
    if os.geteuid() != 0: raise RuntimeError('仅供 root 首装编排调用')
    state = json.loads(args.state.read_text())
    track = Path('/etc/hotel-tv/nas-install-state.json')
    status = json.loads(track.read_text()) if track.exists() else {'completed': []}

    def phase(name, operation):
        if name in status['completed']: return
        print('NAS 阶段：' + name, flush=True)
        operation(); status['completed'].append(name); private_json(track, status)

    phase('packages', lambda: apt(['acl', 'samba', 'smbclient', 'cockpit', 'cockpit-packagekit', 'python3-yaml', 'adb', 'aapt', 'apksigner', 'default-jre-headless']))
    phase('docker', install_docker)
    images = json.loads((ROOT / 'deploy/images.lock.json').read_text())
    phase('nas-images', lambda: [pull_image(images[n]) for n in ('jellyfin', 'filebrowser', 'dockge')])
    options = ['--host', state['host'], '--operator', state['operator']]
    phase('nas-compose', lambda: command([sys.executable, ROOT / 'deploy/nas/configure.py', *options, '--timezone', state['timezone']], json.dumps(state['service_passwords']) if state.get('service_passwords') else state['nas_password']))
    phase('nas-initialize', lambda: command([sys.executable, ROOT / 'deploy/nas/initialize.py', *options], json.dumps(state['service_passwords']) if state.get('service_passwords') else state['nas_password']))
    phase('nas-bootstrap-cleanup', remove_bootstrap_environment)
    command(['systemctl', 'enable', '--now', 'cockpit.socket'])
    identity = pwd.getpwnam(state['operator'])
    phase('download-prepare', lambda: command([sys.executable, ROOT / 'deploy/nas/smart_download.py', 'prepare', '--uid', identity.pw_uid, '--gid', pwd.getpwnam('hotel-tv').pw_gid, '--with-prowlarr', '--timezone', state['timezone']]))
    phase('download-start', lambda: command([sys.executable, ROOT / 'deploy/nas/smart_download.py', 'start']))
    # qB may take a few seconds to create its temporary password; the helper never prints it.
    def password():
        for attempt in range(60):
            try: command([sys.executable, ROOT / 'deploy/nas/smart_download.py', 'set-qb-password'], state.get('service_passwords',{}).get('qbittorrent',state['nas_password'])); return
            except subprocess.CalledProcessError:
                if attempt == 59: raise
                time.sleep(1)
    phase('download-password', password)
    phase('download-adapter', lambda: connect_downloads(state))
    phase('prowlarr-native-client', lambda: command([sys.executable, ROOT / 'deploy/nas/download-tools/commission.py', '--backup', '/var/backups/hotel-nas/fresh-native']))
    phase('jackett', lambda: command([sys.executable, ROOT / 'deploy/nas/download-tools/jackett.py', '--timezone', state['timezone']]))
    phase('native-proxy', lambda: native_tools(state))
    phase('qb-official-plugin', plugin)
    phase('npc', lambda: command(['sh', ROOT / 'deploy/nas/npc/install.sh']))
    phase('npc-control', lambda: command(['sh', ROOT / 'deploy/nas/npc/install_control.sh']))
    phase('software-control', lambda: command(['sh', ROOT / 'deploy/software/install.sh']))
    print('NAS、原生下载、NPC等待配置和软件管理已接入；无需外网即可访问已配置的本地服务。')


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        print('NAS 安装阶段未完成：' + type(exc).__name__ + '；未输出上游响应或密钥。', file=sys.stderr)
        sys.exit(1)
