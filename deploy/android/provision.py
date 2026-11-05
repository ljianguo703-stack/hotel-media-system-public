#!/usr/bin/env python3
"""Provision one explicitly selected installed Hotel TV client; no scans, resets or reboots."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shlex
import sqlite3
import subprocess
import sys

CONFIG_RECEIVER = 'com.xjg.hoteltv/.guard.ConfigReceiver'


def target_address(value):
    address = ipaddress.IPv4Address(value)
    if address.is_loopback or not address.is_private or address.is_unspecified or address.is_multicast:
        raise ValueError('请输入单台电视的局域网 IPv4')
    return str(address)


def broadcast(extras):
    args = ['am', 'broadcast', '-a', 'com.xjg.hoteltv.CONFIG', '-n', CONFIG_RECEIVER]
    for key, value in extras.items(): args += ['--ez' if isinstance(value, bool) else '--es', key, str(value).lower() if isinstance(value, bool) else value]
    return shlex.join(args) + '\n'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--tv', required=True, type=target_address); p.add_argument('--port', type=int, default=5555)
    p.add_argument('--token-stdin',action='store_true',help='读取后台一次性设备凭据，不在命令行传递'); p.add_argument('--device', default='tv-001'); p.add_argument('--operator'); p.add_argument('--plan', action='store_true')
    a = p.parse_args()
    if not 1 <= a.port <= 65535: raise ValueError('ADB 端口无效')
    endpoint = f'{a.tv}:{a.port}'
    if a.plan:
        print('只连接 ' + endpoint + '，从本机 SQLite 读取指定设备凭证，通过受保护 receiver 写入服务器/PIN；不安装、卸载、清数据或重启。'); return
    if os.geteuid() != 0: raise ValueError('需要 sudo 读取本机私有安装凭据；ADB 以原普通用户运行')
    state = json.loads(Path('/etc/hotel-tv/install-state.json').read_text())
    operator = a.operator or os.environ.get('SUDO_USER') or state['operator']
    identity = pwd.getpwnam(operator)
    if identity.pw_uid == 0 or identity.pw_dir == '/nonexistent': raise ValueError('请用 sudo 从普通登录用户运行，或 --operator 指定该用户')
    with sqlite3.connect('file:/srv/hotel-tv/data/events.sqlite3?mode=ro', uri=True) as db:
        row = db.execute('SELECT token FROM devices WHERE id=?', (a.device,)).fetchone()
    if not row: raise ValueError('设备不存在；先在后台设备页创建房间设备，再传 --device')
    device_token=row[0]
    if device_token.startswith('sha256:'):
        candidate=sys.stdin.readline().strip() if a.token_stdin else state.get('tv_token','')
        if 'sha256:'+hashlib.sha256(candidate.encode()).hexdigest()!=device_token:
            raise ValueError('设备凭据已单向保存；请从后台取得/轮换一次性凭据，通过 --token-stdin 提供')
        device_token=candidate
    pin_hash = hashlib.sha256(state['staff_pin'].encode()).hexdigest()
    managed_pin = Path('/srv/hotel-tv/data/maintenance-pin.json')
    if managed_pin.is_file():
        pin_hash = json.loads(managed_pin.read_text())['sha256']
        if not re.fullmatch(r'[a-f0-9]{64}', pin_hash): raise ValueError('维护 PIN 配置无效，未写入电视')
    prefix = ['runuser', '-u', operator, '--', 'env', 'ADB_SERVER_SOCKET=tcp:localhost:5040', 'adb']
    def adb(args, input=None):
        result = subprocess.run(prefix + args, input=input, text=True, capture_output=True, timeout=45)
        if result.returncode: raise RuntimeError('ADB 操作未完成；检查授权和连接，未清理原配置')
        return result.stdout
    changed = False; transaction = secrets.token_hex(16)
    try:
        adb(['connect', endpoint])
        if adb(['-s', endpoint, 'get-state']).strip() != 'device': raise RuntimeError('电视尚未授权本机 ADB')
        installed = adb(['-s', endpoint, 'shell', 'pm', 'path', 'com.xjg.hoteltv'])
        if not installed.startswith('package:'): raise RuntimeError('请先安装 GitHub Release 的通用电视 APK')
        # Input is sent over stdin; credentials are not in the local command line or output.
        result = adb(['-s', endpoint, 'shell'], broadcast({'provision_server': f"http://{state['host']}:18790",
              'provision_token': device_token, 'staff_pin_sha256': pin_hash, 'provision_id': transaction}))
        if 'hotel_provisioned' not in result: raise RuntimeError('电视没有确认写入')
        changed = True
        result = adb(['-s', endpoint, 'shell'], broadcast({'provision_status': True}))
        if 'hotel_configured=true pin_configured=true' not in result: raise RuntimeError('配置回读未通过')
        print('指定电视连接与员工 PIN 已配置。返回影院首页后刷新片库；本工具不改变 HOME 接管或 ADB 开关。')
    except Exception:
        if changed:
            adb(['-s', endpoint, 'shell'], broadcast({'provision_rollback': transaction}))
        raise
    finally:
        subprocess.run(prefix + ['disconnect', endpoint], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        print(str(exc) if isinstance(exc, (ValueError, RuntimeError)) else '配置未完成：' + type(exc).__name__, file=sys.stderr); sys.exit(1)
