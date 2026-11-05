"""Unprivileged hotel HTTP server -> narrowly scoped local NPC controller."""
import json
import socket
from management import Problem

SOCKET = '/run/hotel-npc/control.sock'


def request(action='status', **payload):
    raw = json.dumps({'action': action, **payload}).encode()+b'\n'
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(45)
            conn.connect(SOCKET)
            conn.sendall(raw)
            with conn.makefile('rb') as response:
                result = json.loads(response.readline(65537))
    except (OSError, ValueError):
        raise Problem('NPC 管理服务暂不可用，请稍后刷新', 503)
    if not result.get('ok'):
        raise Problem(result.get('error', 'NPC 操作失败'), result.get('status', 503))
    return result['result']
