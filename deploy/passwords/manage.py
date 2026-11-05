#!/usr/bin/env python3
"""Root-only Cockpit action. Native application recovery; no HTTP listener or Ubuntu password storage."""
from contextlib import contextmanager
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import select
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

CONFIG = Path('/etc/hotel-tv')
DATA = Path('/srv/hotel-tv/data')
NAS = Path('/srv/nas/appdata')
STACKS = Path('/opt/stacks')
LABELS = {'admin': '管理后台', 'smb': 'NAS / SMB 共享', 'jellyfin': 'Jellyfin',
          'filebrowser': '文件中心', 'dockge': 'Dockge 容器管理',
          'qbittorrent': 'qBittorrent', 'aria2': 'aria2 连接密钥', 'tv_pin': '电视维护 PIN'}
CONTAINERS = {'jellyfin': 'nas-jellyfin', 'filebrowser': 'nas-filebrowser', 'dockge': 'nas-dockge',
              'qbittorrent': 'hotel-qbittorrent', 'aria2': 'hotel-aria2'}


class Failure(Exception):
    pass


def run(args, data=None, timeout=45):
    # Never pass a secret in argv, propagate subprocess output, or log an upstream response.
    result = subprocess.run(args, input=data, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        raise Failure('原生工具未完成；可重试此项，或在对应应用检查状态')
    return result.stdout


def load(path):
    return json.loads(path.read_text())


@contextmanager
def hotel_identity():
    owner = pwd.getpwnam('hotel-tv')
    uid, gid = os.geteuid(), os.getegid()
    try:
        os.setegid(owner.pw_gid); os.seteuid(owner.pw_uid)
        yield
    finally:
        os.seteuid(uid); os.setegid(gid)


def write_private(path, value, owner=None, mode=0o600):
    """Atomic replace, preserving ownership; caller controls the fixed parent directory."""
    if path.is_symlink():
        raise Failure('配置路径异常，未覆盖')
    stat = path.stat() if path.exists() else None
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            out.write(value); out.flush(); os.fsync(out.fileno())
        os.chmod(name, mode)
        if os.geteuid() == 0 and (owner or stat):
            os.chown(name, *(owner or (stat.st_uid, stat.st_gid)))
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def http(url, body=None, headers=None, form=False, method=None):
    raw = None if body is None else (urlencode(body) if form else json.dumps(body)).encode()
    h = dict(headers or {})
    if body is not None: h['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
    with build_opener(ProxyHandler({})).open(Request(url, data=raw, headers=h, method=method), timeout=12) as response:
        raw = response.read(2 * 1024 * 1024)
        return (json.loads(raw) if raw and 'json' in response.headers.get('Content-Type', '') else raw), dict(response.headers)


class Manager:
    def status(self):
        with hotel_identity(), sqlite3.connect(DATA / 'events.sqlite3') as db:
            users = [{'name': r[0], 'role': r[1], 'enabled': bool(r[2])}
                     for r in db.execute('SELECT name,role,enabled FROM users ORDER BY name')]
        try: containers = set(run(['docker', 'ps', '-a', '--format', '{{.Names}}']).splitlines())
        except (OSError, Failure): containers = set()
        installed = {k: v in containers for k, v in CONTAINERS.items()}
        installed.update(admin=True, smb=Path('/etc/samba/hotel-nas.conf').is_file(), tv_pin=True)
        return {'services': [{'id': k, 'name': name, 'installed': installed[k],
                              'account': 'nasadmin' if k == 'smb' else 'admin' if k not in ('aria2', 'tv_pin') else ''}
                             for k, name in LABELS.items()], 'users': users}

    def validate(self, body):
        if set(body) - {'action', 'passwords', 'admin_username'}:
            raise Failure('请求字段无效')
        values = body.get('passwords')
        if not isinstance(values, dict) or not values or set(values) - LABELS.keys():
            raise Failure('请至少填写一项有效的新密码')
        status = self.status()
        installed = {s['id'] for s in status['services'] if s['installed']}
        for key, value in values.items():
            if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise Failure(LABELS[key] + '含不支持的控制字符')
            if key == 'tv_pin':
                if not re.fullmatch(r'[0-9]{6}', value): raise Failure('电视维护 PIN 需为 6 位数字')
            elif not 12 <= len(value) <= 128:
                raise Failure(LABELS[key] + '密码需为 12–128 个字符')
            elif value != value.strip():
                raise Failure(LABELS[key] + '密码首尾不能包含空格')
            if key not in installed: raise Failure(LABELS[key] + '未安装，未执行任何修改')
        username = body.get('admin_username', 'admin')
        if 'admin' in values and not any(u['name'] == username and u['enabled'] for u in status['users']):
            raise Failure('请选择现有且启用的后台账号；恢复停用账号请先用 Ubuntu 管理数据库')
        return values, username

    def audit(self, key, status):
        with hotel_identity(), sqlite3.connect(DATA / 'events.sqlite3', timeout=15) as db:
            db.execute('INSERT INTO audit(at,actor,action,object) VALUES(?,?,?,?)',
                       (int(time.time() * 1000), 'system:Ubuntu密码管理', '密码管理', LABELS[key] + '：' + status))

    def save(self, body):
        values, username = self.validate(body)  # Validate the entire form before the first mutation.
        rows = []
        for key in LABELS:
            if key not in values: continue
            try:
                note = getattr(self, 'set_' + key)(values[key], username)
                row = {'id': key, 'name': LABELS[key], 'ok': True, 'message': note or '已修改'}
            except Exception as exc:
                row = {'id': key, 'name': LABELS[key], 'ok': False,
                       'message': str(exc) if isinstance(exc, Failure) else '未完成验证；请重试此项，或在原生应用检查'}
            try: self.audit(key, '完成' if row['ok'] else '待检查')
            except Exception:
                row['ok'] = False; row['message'] = '操作已执行，但审计未写入；请检查服务器后重试此项'
            rows.append(row)
        return {'ok': all(r['ok'] for r in rows), 'results': rows}

    def set_admin(self, value, username):
        salt = secrets.token_hex(16)
        hashed = salt + ':' + hashlib.pbkdf2_hmac('sha256', value.encode(), salt.encode(), 260000).hex()
        with hotel_identity(), sqlite3.connect(DATA / 'events.sqlite3', timeout=15) as db:
            if db.execute('UPDATE users SET password=? WHERE name=? AND enabled=1', (hashed, username)).rowcount != 1:
                raise Failure('账号状态已变化，请刷新后重试')
            db.execute('DELETE FROM sessions WHERE name=?', (username,))
            db.execute("UPDATE kv SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
        return username + ' 密码已修改，旧后台登录已退出'

    def set_smb(self, value, _):
        run(['smbpasswd', '-s', 'nasadmin'], value + '\n' + value + '\n')
        return 'nasadmin 密码已修改；电脑共享需重新连接'

    def set_jellyfin(self, value, _):
        with hotel_identity(): config = load(DATA / 'nas-integration.json')
        # Fixed loopback endpoint only; the sync token is kept on the host.
        headers = {'Authorization': 'MediaBrowser Client="Hotel Passwords", Device="Ubuntu", DeviceId="hotel-passwords", Version="1", Token="' + config['token'] + '"'}
        users, _ = http('http://127.0.0.1:8096/Users', headers=headers)
        user = next((u for u in users if u.get('Name') == 'admin' and u.get('Policy', {}).get('IsAdministrator')), None)
        if not user: raise Failure('未找到 Jellyfin 的 admin 管理员，请在原生应用核对账号')
        http('http://127.0.0.1:8096/Users/' + user['Id'] + '/Password', {'NewPw': value, 'ResetPassword': False}, headers)
        result, _ = http('http://127.0.0.1:8096/Users/AuthenticateByName', {'Username': 'admin', 'Pw': value},
                         {'Authorization': 'MediaBrowser Client="Hotel Passwords", Device="Ubuntu", DeviceId="hotel-passwords", Version="1"'})
        if not result.get('AccessToken'): raise Failure('密码已提交，但重新登录验证未通过')
        logout = dict(headers)
        logout['Authorization'] = logout['Authorization'].replace(config['token'], result['AccessToken'])
        http('http://127.0.0.1:8096/Sessions/Logout', {}, logout)
        return 'admin 密码已修改并验证；片库同步密钥保持可用'

    def set_filebrowser(self, value, _):
        # Quantum requires exclusive database access during its native CLI recovery.
        file = STACKS / 'filebrowser/compose.yaml'
        run(['docker', 'stop', 'nas-filebrowser'])
        try:
            run(['docker', 'compose', '-f', str(file), 'run', '--rm', '-T', '--no-deps', '--entrypoint', './filebrowser',
                 'filebrowser', 'user', 'set', 'admin', '--password', '-c', '/home/filebrowser/data/config.yaml'], value + '\n')
        finally: run(['docker', 'start', 'nas-filebrowser'])
        return 'admin 密码已由原生工具重置；如曾启用双重验证，需重新绑定'

    def set_dockge(self, value, _):
        # Use the project's recovery command and answer each readline prompt only when it appears.
        proc = subprocess.Popen(['docker', 'exec', '-i', 'nas-dockge', 'npm', 'run', 'reset-password'],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output = b''; answered = 0; deadline = time.monotonic() + 70
        prompts = (b'New Password:', b'Confirm New Password:')
        try:
            while time.monotonic() < deadline:
                if select.select([proc.stdout], [], [], .25)[0]:
                    chunk = os.read(proc.stdout.fileno(), 4096)
                    if not chunk: break
                    output = (output + chunk)[-65536:]
                    if answered < 2 and prompts[answered] in output:
                        proc.stdin.write((value + '\n').encode()); proc.stdin.flush(); answered += 1
                elif proc.poll() is not None: break
            if b'Password reset successfully.' not in output:
                raise Failure('Dockge 原生重置未确认，请重试此项')
        finally:
            if proc.poll() is None: proc.terminate()
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait()
            proc.stdin.close(); proc.stdout.close()
        run(['docker', 'restart', 'nas-dockge'])  # Reload JWT secret and invalidate old sessions.
        return '管理员密码已重置，旧 Dockge 登录已退出'

    def qb_login(self, password):
        try:
            raw, headers = http('http://127.0.0.1:8098/api/v2/auth/login', {'username': 'admin', 'password': password},
                                {'Referer': 'http://127.0.0.1:8098/', 'Origin': 'http://127.0.0.1:8098'}, True)
            cookie = next((v for k, v in headers.items() if k.lower() == 'set-cookie'), '')
            # qB 5.3 namespaces its session cookie by port; older versions use SID.
            return cookie.split(';')[0] if re.match(r'^(?:SID|QBT_SID_8098)=', cookie) else None
        except Exception: return None

    def set_qbittorrent(self, value, _):
        with hotel_identity():
            current = load(DATA / 'download-secrets.json').get('qbittorrent', {}).get('password', '')
        sid = self.qb_login(value) or self.qb_login(current)
        if not sid:
            # Official forgotten-password recovery: remove only WebUI password keys while stopped.
            path = NAS / 'qbittorrent/qBittorrent/config/qBittorrent.conf'
            if not path.is_file(): raise Failure('未找到标准 qBittorrent 配置，未重置未知路径')
            run(['docker', 'stop', 'hotel-qbittorrent'])
            try:
                lines = []; section = ''
                for line in path.read_text().splitlines():
                    if line.startswith('['): section = line.strip()
                    if line.startswith(('WebUI\\Password_PBKDF2=', 'WebUI\\Password_ha1=')): continue
                    if section == '[WebUI]' and line.startswith(('Password_PBKDF2=', 'Password_ha1=')): continue
                    lines.append(line)
                write_private(path, '\n'.join(lines) + '\n')
            finally: run(['docker', 'start', 'hotel-qbittorrent'])
            for attempt in range(20):
                logs = subprocess.run(['docker', 'logs', '--since', '2m', 'hotel-qbittorrent'], capture_output=True, text=True, timeout=10)
                temporary = re.findall(r'(?:temporary password is provided for this session:|临时密码[：:])\s*(\S+)', logs.stdout + logs.stderr)
                sid = self.qb_login(temporary[-1]) if temporary else None
                if sid: break
                time.sleep(.5)
            if not sid: raise Failure('临时凭据尚未就绪，可重试此项；下载任务保留')
        http('http://127.0.0.1:8098/api/v2/app/setPreferences', {'json': json.dumps({'web_ui_password': value})},
             {'Referer': 'http://127.0.0.1:8098/', 'Origin': 'http://127.0.0.1:8098', 'Cookie': sid}, True)
        if not self.qb_login(value): raise Failure('新密码重新登录验证未通过，请重试此项')
        self.sync_download_secret('qbittorrent', 'password', value)
        try: synced = self.sync_arr_downloaders(value)
        except Failure: raise
        except Exception:
            raise Failure('qBittorrent 密码已修改，但 Radarr/Sonarr 的下载连接未同步；请重试此项，或在“加片与下载”页点“修复下载器连接”')
        return 'admin 密码已修改并验证，后台下载连接' + ('及 Radarr、Sonarr ' if synced else '') + '已同步'

    def sync_arr_downloaders(self, password):
        """Radarr/Sonarr keep their own copy of the qB password; without this, Seerr requests silently stall."""
        path = DATA / 'media-automation.json'   # full install only: native app endpoints and API keys
        if not path.exists(): return []
        config = load(path); done = []
        for app in ('radarr', 'sonarr'):
            service = config.get(app) or {}
            if not service.get('api_key'): continue
            if not re.fullmatch(r'http://127\.0\.0\.1:\d{2,5}/?', service.get('url', '')): raise Failure(app + ' 连接配置异常，未发送密码')
            base, headers = service['url'].rstrip('/') + '/api/v3', {'X-Api-Key': service['api_key']}
            clients, _ = http(base + '/downloadclient', headers=headers)
            client = next((c for c in clients if c.get('name') == 'Hotel qB'), None)
            if not client: raise Failure(app + ' 中缺少“Hotel qB”下载连接；请在“加片与下载”页点“修复下载器连接”')
            for field in client.get('fields', []):
                if field.get('name') == 'password': field['value'] = password
            # The native PUT validates the connection; a wrong password is rejected, not silently saved.
            http(base + '/downloadclient/%d' % int(client['id']), client, headers, method='PUT')
            done.append(app)
        return done

    def sync_download_secret(self, service, field, value):
        try:
            with hotel_identity(), sqlite3.connect(DATA / 'events.sqlite3', timeout=15) as db:
                # Existing download settings also lock this database and check its revision.
                # Serialize their read/merge/write against this credential rotation.
                db.execute('BEGIN IMMEDIATE')
                path = DATA / 'download-secrets.json'; config = load(path)
                config.setdefault(service, {})[field] = value
                write_private(path, json.dumps(config, ensure_ascii=False))
                db.execute("UPDATE kv SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
        except Exception:
            raise Failure('应用凭据已修改，但后台下载连接未同步；请重试此项')

    def set_aria2(self, value, _):
        path = NAS / 'aria2/aria2.conf'
        run(['docker', 'stop', 'hotel-aria2'])
        try:
            lines = [l for l in path.read_text().splitlines() if not l.startswith('rpc-secret=')]
            write_private(path, '\n'.join(lines + ['rpc-secret=' + value]) + '\n')
        finally: run(['docker', 'start', 'hotel-aria2'])
        for attempt in range(20):
            try:
                result, _ = http('http://127.0.0.1:6800/jsonrpc', {'jsonrpc': '2.0', 'id': 'password-test',
                                  'method': 'aria2.getVersion', 'params': ['token:' + value]})
                if 'result' in result: break
            except Exception: pass
            time.sleep(.5)
        else: raise Failure('aria2 新密钥验证未通过，可重试此项')
        self.sync_download_secret('aria2', 'secret', value)
        defaults = NAS / 'download-tools/ariang/hotel-defaults.js'
        if defaults.exists():
            prefix = 'angular.module("ariaNg").config(["ariaNgDefaultOptions",function(o){Object.assign(o,'
            content = defaults.read_text()
            if not content.startswith(prefix): raise Failure('RPC 密钥和后台已更新；AriaNg 默认配置需人工检查')
            options, _ = json.JSONDecoder().raw_decode(content[len(prefix):])
            options['secret'] = base64.b64encode(value.encode()).decode()
            write_private(defaults, prefix + json.dumps(options, ensure_ascii=True) + ');}]);\n', mode=0o640)
        return 'RPC 密钥、后台和 AriaNg 默认连接已同步；浏览器曾单独保存的连接需填写新密钥'

    def set_tv_pin(self, value, _):
        with hotel_identity():
            path = DATA / 'maintenance-pin.json'
            write_private(path, json.dumps({'revision': secrets.token_hex(16), 'sha256': hashlib.sha256(value.encode()).hexdigest()}))
        return 'PIN 已保存；新版电视下次刷新同步，旧版电视需安装支持同步的客户端'


def main():
    if os.geteuid() != 0: raise Failure('需要先用 Ubuntu 账号登录并开启管理权限')
    raw = sys.stdin.read(16385)
    if len(raw) > 16384: raise Failure('请求过大')
    body = json.loads(raw)
    if not isinstance(body, dict) or body.get('action') not in ('status', 'save'): raise Failure('操作无效')
    with open('/run/lock/hotel-passwords.lock', 'a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise Failure('已有改密任务进行中，请稍后再试')
        manager = Manager()
        return manager.status() if body['action'] == 'status' else manager.save(body)


if __name__ == '__main__':
    try: result = main()
    except Exception as exc:
        result = {'error': str(exc) if isinstance(exc, Failure) else '密码管理未完成，请检查本机服务状态后重试'}
    print(json.dumps(result, ensure_ascii=False))
