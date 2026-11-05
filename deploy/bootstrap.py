#!/usr/bin/env python3
"""Fresh Ubuntu orchestration; reuses the existing service and native application installers."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import ipaddress
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / 'deploy'))
from lan_firewall import DEFAULT_NETWORKS, networks
CONFIG = Path('/etc/hotel-tv')
STATE = CONFIG / 'install-state.json'
DATA = Path('/srv/hotel-tv/data')
OPENER = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))


def run(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


@contextmanager
def service_identity():
    """SQLite WAL and private app files must be created by their real service owner."""
    owner = pwd.getpwnam('hotel-tv'); uid, gid = os.geteuid(), os.getegid()
    try:
        os.setegid(owner.pw_gid); os.seteuid(owner.pw_uid)
        yield
    finally:
        os.seteuid(uid); os.setegid(gid)


def discover_ip():
    # UDP connect only chooses a route; it sends no packet and requires no Internet response.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(('192.0.2.1', 9))
        return s.getsockname()[0]


def validate_host(value):
    addr = ipaddress.IPv4Address(value)
    if not any(addr in ipaddress.ip_network(cidr) for cidr in DEFAULT_NETWORKS):
        raise ValueError('请使用本机局域网 IPv4；不用公网或回环地址')
    return str(addr)


def os_release():
    return dict(line.strip().split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)


def apt(packages):
    run(['apt-get', '-o', 'DPkg::Lock::Timeout=180', '-o', 'Acquire::Retries=3', 'install', '-y', '--no-install-recommends', *packages], env={**os.environ, 'DEBIAN_FRONTEND': 'noninteractive'})


def install_docker():
    if shutil.which('docker'):
        run(['docker', 'compose', 'version'])
        engine=subprocess.check_output(['docker','version','--format','{{.Server.Version}}'],text=True).strip()
        if not engine.split('.',1)[0].isdigit() or int(engine.split('.',1)[0])<28:
            raise RuntimeError('完整安装要求Docker Engine 28或更新版本以保护回环端口；现有引擎未修改，请先升级后继续')
        return
    # Docker's official signed APT repository. Do not remove another engine or unrelated packages.
    release = os_release()
    codename = release.get('UBUNTU_CODENAME', release.get('VERSION_CODENAME', '')).strip('"')
    arch = subprocess.check_output(['dpkg', '--print-architecture'], text=True).strip()
    key = Path('/etc/apt/keyrings/docker.asc'); key.parent.mkdir(parents=True, exist_ok=True)
    with OPENER.open('https://download.docker.com/linux/ubuntu/gpg', timeout=45) as response: key_bytes = response.read()
    if key.exists() and key.read_bytes() != key_bytes: raise RuntimeError('现有 Docker APT key 不同，请先人工核对')
    key.write_bytes(key_bytes); key.chmod(0o644)
    repo = Path('/etc/apt/sources.list.d/docker.sources')
    content = f'Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: {codename}\nComponents: stable\nArchitectures: {arch}\nSigned-By: {key}\n'
    if repo.exists() and repo.read_text() != content: raise RuntimeError('现有 Docker APT 源不同，未覆盖')
    repo.write_text(content); repo.chmod(0o644)
    run(['apt-get', 'update'])
    apt(['docker-ce', 'docker-ce-cli', 'containerd.io', 'docker-buildx-plugin', 'docker-compose-plugin'])
    run(['systemctl', 'enable', '--now', 'docker'])


def validate_storage(value):
    p = Path(value)
    if not p.is_absolute() or '..' in p.parts or any(c in str(p) for c in '\n\r\0\\\"%'):
        raise ValueError('媒体目录须为绝对路径，不含穿越或配置控制字符')
    p = p.resolve()
    protected = [Path(x).resolve() for x in ('/etc','/usr','/bin','/sbin','/var','/home','/root','/opt','/dev','/proc','/sys','/run','/boot')]
    if p == Path('/') or p == Path('/srv') or any(p == x or x in p.parents for x in protected):
        raise ValueError('请选择独立媒体子目录')
    # The cinema service reads media only under these roots (see hotel-tv.service --library-root).
    if not any(base in p.parents for base in (Path('/srv/hotel-tv/media'), Path('/mnt'), Path('/media'))):
        raise ValueError('数据目录须位于 /mnt 或 /media 下的已挂载数据盘（例如 /mnt/media-disk/data），或使用默认目录')
    return str(p)


def setup_state(args):
    if STATE.exists():
        if not args.resume: raise RuntimeError('已有本安装器记录；中断恢复用 --resume，日常升级用后台软件与更新')
        state = json.loads(STATE.read_text())
        if state['release'] != (SOURCE / 'VERSION').read_text().strip():
            raise RuntimeError('首装恢复须使用原版本安装包；已装系统的后续版本使用后台升级')
        if state['profile'] != args.profile: raise RuntimeError('恢复时请保留原 profile')
        if getattr(args, 'lan_cidr', None) and networks(args.lan_cidr) != state.get('lan_cidrs', DEFAULT_NETWORKS):
            raise RuntimeError('首装恢复时不能更改允许网段')
        chosen = getattr(args, 'storage_root', '/srv/hotel-tv/media/.automation')
        if chosen != '/srv/hotel-tv/media/.automation' and validate_storage(chosen) != state.get('storage_root'):
            raise RuntimeError('恢复时不能更换媒体目录；保留原安装状态')
        return state
    if Path('/opt/hotel-tv/current').exists() or (DATA / 'events.sqlite3').exists() or Path('/etc/hotel-tv/devices.json').exists():
        raise RuntimeError('发现已有影院，首装流程停止；请使用原后台的升级功能并先备份')
    operator = args.operator or os.environ.get('SUDO_USER') or 'hotel-tv'
    if operator == 'root': operator = 'hotel-tv'
    if operator != 'hotel-tv': pwd.getpwnam(operator)
    state = {'schema': 1, 'install_id': secrets.token_hex(16), 'release': (SOURCE / 'VERSION').read_text().strip(), 'profile': args.profile,
             'host': validate_host(args.lan_ip or discover_ip()), 'operator': operator, 'hotel': args.hotel,
             'lan_cidrs': networks(getattr(args, 'lan_cidr', None) or DEFAULT_NETWORKS),
             'timezone': args.timezone, 'created': datetime.now(timezone.utc).isoformat(), 'completed': [],
             'tv_token': secrets.token_urlsafe(32), 'staff_pin': str(secrets.randbelow(900000) + 100000),
             'storage_root': validate_storage(getattr(args, 'storage_root', '/srv/hotel-tv/media/.automation')), 'nas_password': secrets.token_urlsafe(24)}
    state['service_passwords']={name:secrets.token_urlsafe(24) for name in ('jellyfin','filebrowser','dockge','smb','qbittorrent')}
    private_json(STATE, state)
    return state


def prepare_seed(stage, state):
    stage = Path(stage)
    for folder in ('server', 'admin'):
        shutil.copytree(SOURCE / folder, stage / folder, ignore=shutil.ignore_patterns('__pycache__', 'test_*.py', 'tests', 'data', 'media', '.env', '.env.*', 'local', 'node_modules'))
    (stage / 'units').mkdir(); shutil.copyfile(SOURCE / 'deploy/ubuntu/hotel-tv.service', stage / 'units/hotel-tv.service')
    unit = stage / 'units/hotel-tv.service'
    storage = state.get('storage_root', '/srv/hotel-tv/media/.automation')
    unit.write_text(unit.read_text().replace(' --library-root /media', ' --library-root /media --library-root \"'+storage+'\"'))
    for name in ('LICENSE', 'NOTICE'): shutil.copyfile(SOURCE / name, stage / name)
    (stage / 'seed/media').mkdir(parents=True)
    private_json(stage / 'seed/devices.json', {'devices': {'tv-001': {'room': '待分配', 'group': '全部客房', 'token': state['tv_token']}}})
    private_json(stage / 'seed/catalog.json', {'hotel': state['hotel'], 'items': []})
    private_json(stage / 'seed/ads.json', {'ads': [], 'midroll_interval_seconds': 1200})


def api(path, body=None, headers=None):
    h = dict(headers or {})
    if body is not None: h['Content-Type'] = 'application/json'
    req = Request(path, data=json.dumps(body).encode() if body is not None else None, headers=h)
    with OPENER.open(req, timeout=5) as response: return json.load(response)


def check_core(state):
    for _ in range(90):
        try:
            result = api('http://127.0.0.1:18790/api/v1/catalog', headers={'X-TV-Token': state['tv_token'], 'X-TV-Probe': '1'})
            if isinstance(result.get('items'), list): break
        except (OSError, URLError, ValueError): pass
        time.sleep(.5)
    else: raise RuntimeError('影院启动检查超时；请运行 journalctl -u hotel-tv 查看本机日志')
    credentials = dict(line.split('=', 1) for line in (DATA / 'admin-initial-password.txt').read_text().splitlines() if '=' in line)
    result = api('http://127.0.0.1:18791/api/admin/login', credentials)
    if result.get('user', {}).get('role') != 'admin': raise RuntimeError('首装管理账号校验失败')
    api('http://127.0.0.1:18791/api/admin/logout', {}, {'X-CSRF-Token': result['user']['csrf']})
    return True


def core_install(state):
    current = Path('/opt/hotel-tv/current')
    expected = Path('/opt/hotel-tv/releases') / state['release']
    if current.exists():
        if current.resolve() != expected: raise RuntimeError('影院程序已被其他发布替换，恢复流程停止')
    else:
        with tempfile.TemporaryDirectory(prefix='hotel-fresh-') as tmp:
            prepare_seed(tmp, state)
            run(['sh', SOURCE / 'deploy/ubuntu/install.sh', tmp, state['release'], state['operator']])
    check_core(state)
    # Apply the chosen local timezone to the application's settings, not the machine or its clock.
    import sqlite3
    with service_identity():
        with sqlite3.connect(DATA / 'events.sqlite3') as db:
            value = json.loads(db.execute("SELECT value FROM kv WHERE key='settings'").fetchone()[0])
            value['timezone'] = state['timezone']; db.execute("UPDATE kv SET value=? WHERE key='settings'", (json.dumps(value),))


def initialize_pin(state):
    path = DATA / 'maintenance-pin.json'
    if path.exists(): return
    with service_identity():
        private_json(path, {'revision': secrets.token_hex(16), 'sha256': hashlib.sha256(state['staff_pin'].encode()).hexdigest()})


def validate_source(profile):
    required = ['server/server.py', 'server/media_automation.py', 'server/media_center.py', 'admin/index.html', 'admin/media-center.js', 'admin/passwords/manifest.json', 'deploy/passwords/manage.py']
    if profile == 'full': required += ['deploy/nas/media_automation.py', 'deploy/images.lock.json', 'deploy/docker_images.py']
    for name in required:
        if not (SOURCE / name).is_file(): raise RuntimeError('安装包缺少文件：' + name)


def summary(state):
    original = (DATA / 'admin-initial-password.txt').read_text()
    applications=''.join(name+'_password='+value+'\n' for name,value in state.get('service_passwords',{}).items())
    text = original + applications + f"staff_pin={state['staff_pin']}\nnas_username=admin\nnas_password={state['nas_password']}\nsmb_username=nasadmin\nserver=http://{state['host']}:18790\nfirst_tv_id=tv-001\n"
    path = CONFIG / 'setup-credentials.txt'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f: f.write(text)
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=('core', 'full'), default='full')
    parser.add_argument('--storage-root', default='/srv/hotel-tv/media/.automation', help='下载与片库共用的独立数据目录；不格式化磁盘')
    parser.add_argument('--lan-ip'); parser.add_argument('--operator')
    parser.add_argument('--lan-cidr', action='append', help='可重复指定酒店私有IPv4网段；默认允许RFC1918私网访问本项目端口')
    parser.add_argument('--hotel', default='酒店影院'); parser.add_argument('--timezone', default=None, help='业务时区；默认沿用Ubuntu主机时区')
    parser.add_argument('--resume', action='store_true'); parser.add_argument('--plan', action='store_true')
    parser.add_argument('--skip-packages', action='store_true', help='仅供已准备依赖的测试环境')
    args = parser.parse_args()
    from zoneinfo import ZoneInfo
    if args.timezone is None:
        zonefile=Path('/etc/timezone')
        local=str(Path('/etc/localtime').resolve())
        args.timezone=zonefile.read_text().strip() if zonefile.is_file() else local.split('/zoneinfo/',1)[-1] if '/zoneinfo/' in local else 'UTC'
    ZoneInfo(args.timezone)
    validate_source(args.profile)
    if args.plan:
        print(json.dumps({'profile': args.profile, 'core': ['Python/systemd 18790/18791', '本地片库', '广告与管理', 'Cockpit / Ubuntu 验证的密码管理'],
                          'full': ['Seerr/Radarr/Sonarr/Bazarr', 'Jellyfin', 'FileBrowser', 'SMB', 'Dockge', 'Cockpit', 'qB/aria2/Prowlarr/Jackett', 'NPC', '软件更新'],
                          'network': {'allowed_networks': networks(args.lan_cidr or DEFAULT_NETWORKS), 'policy': '本项目主机/容器端口仅私网；保留SSH及其他规则'},
                          'does_not_modify': ['磁盘分区/格式', 'SSH/网卡', '未知应用/已有影院']}, ensure_ascii=False, indent=2)); return
    if os.geteuid() != 0: raise RuntimeError('请使用 sudo ./install.sh；--plan 可以普通用户运行')
    release = os_release()
    if release.get('ID', '').strip('"') != 'ubuntu' or release.get('VERSION_ID', '').strip('"') not in ('24.04', '26.04'):
        raise RuntimeError('首装目标为 Ubuntu 24.04/26.04 LTS；其他系统不自动修改')
    if not Path('/run/systemd/system').is_dir(): raise RuntimeError('需要已启动 systemd 的 Ubuntu 主机或完整虚拟机')
    state = setup_state(args)
    if not args.skip_packages:
        run(['apt-get', '-o', 'DPkg::Lock::Timeout=180', '-o', 'Acquire::Retries=3', 'update'])
        apt(['python3', 'ffmpeg', 'ca-certificates', 'curl', 'sqlite3', 'adb', 'cockpit', 'tzdata', 'nftables'])
    from lan_firewall import install as install_lan_policy
    print('配置本项目局域网端口；不安装或启用SSH，不修改网卡或已有其他规则。', flush=True)
    install_lan_policy(state, private_json, run)
    for phase, operation in [('core', lambda: core_install(state)), ('full', lambda: run([sys.executable, SOURCE / 'deploy/nas/portable.py', '--state', STATE])), ('media', lambda: run([sys.executable, SOURCE / 'deploy/nas/media_automation.py', '--state', STATE]))]:
        if phase in ('full', 'media') and state['profile'] != 'full': continue
        if phase in state['completed'] and phase != 'media': continue
        print('安装阶段：' + phase, flush=True)
        operation()
        if phase not in state['completed']: state['completed'].append(phase)
        private_json(STATE, state)
    run(['sh', SOURCE / 'deploy/passwords/install.sh'])
    initialize_pin(state)
    check_core(state); summary(state)
    media = state.get('storage_root', '/srv/hotel-tv/media/.automation') if state['profile'] == 'full' else '/srv/hotel-tv/media'
    print(f"安装完成（{state['release']}）：后台 http://{state['host']}:18791\n片库与下载数据 {media}\n初始账号与员工PIN：sudo cat /etc/hotel-tv/setup-credentials.txt\n"
          + ("之后的日常操作都在后台完成：加片看“加片与下载”，需要处理的事项会显示在运营概览。\n" if state['profile'] == 'full' else '')
          + "电视首次凭证由后台设备页或本机provision工具读取；不要上传以上私有文件。")


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        # Commands and upstream responses can contain credentials; report class, not raw request bodies.
        if isinstance(exc, (RuntimeError, ValueError)): print(str(exc), file=sys.stderr)
        else: print('安装未完成：' + type(exc).__name__ + '；保留状态，可修正原因后用同一profile加 --resume。', file=sys.stderr)
        sys.exit(1)
