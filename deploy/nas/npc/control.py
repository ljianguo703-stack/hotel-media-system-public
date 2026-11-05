#!/usr/bin/env python3
"""Root-owned NPC controller. Fixed stack and actions; no web-provided commands or paths."""
import argparse
import json
import os
from pathlib import Path
import pwd
import shlex
import socket
import socketserver
import stat
import struct
import subprocess
import threading
import time

from npc_protocol import NpcError, validate_config

IMAGE = 'ghcr.io/djylb/npc:v0.34.10@sha256:64b05938c189edbdcb90865427f6e28e6aeb3e8e38f4a01495b69a0760944812'


class Controller:
    def __init__(self, stack=Path('/opt/stacks/npc'), data=Path('/srv/nas/appdata/npc'), run=None):
        self.stack, self.data = Path(stack), Path(data)
        self.run = run or self.command
        self.lock = threading.Lock()

    @staticmethod
    def command(args):
        try:
            return subprocess.run(args, check=True, capture_output=True, text=True, timeout=30).stdout
        except (subprocess.SubprocessError, OSError):
            # Never return Docker/Compose raw diagnostics: they may include environment values.
            raise NpcError('NPC 操作未完成，请检查容器或稍后重试', 503)

    @staticmethod
    def write(path, text):
        temp = path.with_name(path.name+'.tmp')
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(text); stream.flush(); os.fsync(stream.fileno())
            temp.chmod(0o600); temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)

    def load(self):
        path = self.data/'control.json'
        if path.exists(): return json.loads(path.read_text())
        # Adopt only the three known settings from NP01; never execute an .env as shell code.
        env = {}
        for line in (self.stack/'.env').read_text().splitlines():
            if line and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                try: value = shlex.split(value)[0] if value.strip() else ''
                except (ValueError, IndexError): raise NpcError('已有 NPC 配置无法读取', 503)
                env[key] = value
        return {'revision': 1, 'enabled': True, 'server': env.get('NPS_SERVER', ''),
                'vkey': env.get('NPS_VKEY', ''), 'type': env.get('NPS_TYPE', 'tcp'), 'dns': '', 'extra': ''}

    def persist(self, config):
        self.write(self.data/'control.json', json.dumps(config, ensure_ascii=False)+'\n')

    def environment(self, config):
        # Compose env_file format: raw preserves dollar signs and quotes literally.
        values = {'NPC_SERVER_ADDR': config['server'], 'NPC_SERVER_VKEY': config['vkey'],
                  'NPC_CONN_TYPE': config['type'], 'NPC_DNS_SERVER': config['dns'],
                  'NPC_EXTRA_ARGS': config['extra'], 'TZ': 'Asia/Singapore'}
        return ''.join(k+'='+v+'\n' for k, v in values.items())

    def compose(self, *args):
        return self.run(['/usr/bin/docker', 'compose', '--project-directory', str(self.stack),
                         '-f', str(self.stack/'compose.yaml'), *args])

    def apply(self, config):
        self.write(self.stack/'connection.env', self.environment(config))
        if config['enabled']:
            self.compose('up', '-d', '--force-recreate', '--pull', 'never')
        else:
            # Keep Docker's manual-stop state so unless-stopped also stays off after reboot.
            # The next start recreates the container with the saved environment.
            self.compose('stop')

    def status(self, config=None):
        config = config or self.load()
        public = {k: config[k] for k in ('revision', 'enabled', 'server', 'type', 'dns', 'extra')}
        public['key_set'] = bool(config['vkey'])
        try:
            obj = json.loads(self.run(['/usr/bin/docker', 'inspect', 'nas-npc']))[0]
            if obj['Config']['Image'] != IMAGE: raise NpcError('NPC 镜像与固定版本不一致', 503)
            running = bool(obj['State']['Running'])
            state = 'waiting_config' if running and not (config['server'] and config['vkey']) else 'running' if running else 'stopped'
            if obj['State'].get('Restarting'): state = 'error'
            runtime = {'state': state, 'running': running, 'version': '0.34.10',
                       'connection': 'unconfirmed' if running and state == 'running' else 'not_connected'}
        except NpcError:
            runtime = {'state': 'unavailable', 'running': False, 'version': '0.34.10', 'connection': 'unknown'}
        return {'config': public, 'runtime': runtime}

    def handle(self, request):
        if not isinstance(request, dict) or set(request) - {'action', 'config', 'revision'}:
            raise NpcError('请求格式无效')
        with self.lock:
            old = self.load(); action = request.get('action')
            if action == 'status': return self.status(old)
            if action not in ('save', 'start', 'stop', 'restart'): raise NpcError('操作无效')
            if type(request.get('revision')) is not int or request['revision'] != old['revision']:
                raise NpcError('NPC 配置已被修改，请刷新配置后重试', 409)
            if action == 'save':
                new = validate_config(request.get('config'), old)
            else:
                new = {k: v for k, v in old.items() if k != 'revision'}
                new['enabled'] = action != 'stop'
                if new['enabled'] and (not new['server'] or not new['vkey']):
                    raise NpcError('请先填写服务端地址和验证密钥，再启用或重启')
            new['revision'] = old['revision']+1
            # Inspect must succeed before any mutation, and only our pinned NPC may be controlled.
            obj = json.loads(self.run(['/usr/bin/docker', 'inspect', 'nas-npc']))[0]
            if obj['Config']['Image'] != IMAGE: raise NpcError('NPC 镜像与固定版本不一致', 503)
            previous_env = (self.stack/'connection.env').read_text()
            try:
                self.apply(new)
                self.persist(new)
            except (NpcError, OSError):
                self.write(self.stack/'connection.env', previous_env)
                try:
                    if obj['State']['Running']: self.compose('up', '-d', '--force-recreate', '--pull', 'never')
                    else: self.compose('stop')
                except NpcError: raise NpcError('应用失败，原配置已保留；请刷新检查客户端状态', 503)
                raise NpcError('应用失败，已恢复原配置，请稍后重试', 503)
            return self.status(new)


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(controller):
    address = Path('/run/hotel-npc/control.sock')
    allowed_uid = pwd.getpwnam('hotel-tv').pw_uid

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(50)
            uid = struct.unpack('3i', self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
            if uid not in (0, allowed_uid): return
            try:
                raw = self.rfile.readline(16385)
                if len(raw) > 16384 or not raw.endswith(b'\n'): raise NpcError('请求过大')
                result = {'ok': True, 'result': controller.handle(json.loads(raw))}
            except NpcError as exc:
                result = {'ok': False, 'error': str(exc), 'status': exc.status}
            except Exception:
                result = {'ok': False, 'error': 'NPC 管理服务暂时无法处理请求', 'status': 503}
            self.wfile.write((json.dumps(result, ensure_ascii=False)+'\n').encode())

    address.unlink(missing_ok=True)
    with Server(str(address), Handler) as server:
        address.chmod(0o660)
        server.serve_forever()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--prepare', action='store_true'); args = parser.parse_args()
    controller = Controller()
    if args.prepare:
        config = controller.load(); controller.persist(config); controller.apply(config)
        print('NPC 0.34.10 管理配置已迁移；密钥未输出。')
    else:
        serve(controller)
