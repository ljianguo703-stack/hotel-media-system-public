"""Small, shared schema for the local NPC controller. No shell input is accepted."""
import ipaddress
import re
import shlex
from urllib.parse import urlsplit


class NpcError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def clean(value, maximum):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise NpcError('配置含无效字符或长度超限')
    return value.strip()


def extra_options(value):
    value = clean(value, 600)
    try:
        parts = shlex.split(value)
    except ValueError:
        raise NpcError('额外参数的引号不完整')
    allowed = {'auto_reconnect': ('bool', 0, 0), 'disable_p2p': ('bool', 0, 0),
               'disconnect_timeout': ('int', 5, 600), 'keepalive': ('int', 0, 120),
               'p2p_timeout': ('int', 1, 60)}
    result = []; seen = set()
    for part in parts:
        match = re.fullmatch(r'--?([a-z][a-z0-9_]+)=(true|false|[0-9]+)', part)
        if not match or match[1] not in allowed or match[1] in seen:
            raise NpcError('额外参数仅支持 auto_reconnect、disable_p2p、disconnect_timeout、keepalive、p2p_timeout，格式为 -名称=值')
        name, val = match.groups(); kind, low, high = allowed[name]
        if (kind == 'bool' and val not in ('true', 'false')) or (kind == 'int' and (not val.isdigit() or not low <= int(val) <= high)):
            raise NpcError('额外参数值超出允许范围')
        result.append('-'+name+'='+val); seen.add(name)
    return ' '.join(result)


def validate_config(body, old):
    if not isinstance(body, dict) or set(body) - {'enabled', 'server', 'vkey', 'type', 'dns', 'extra'}:
        raise NpcError('NPC 配置字段无效')
    if type(body.get('enabled')) is not bool:
        raise NpcError('启用选项无效')
    server = clean(body.get('server', ''), 500)
    if server:
        for address in server.split(','):
            try:
                u = urlsplit('//'+address)
                host, port = u.hostname, u.port
                if not host or not port or u.path or u.query or u.fragment or u.username or u.password or any(c.isspace() for c in address):
                    raise ValueError()
                try: ipaddress.ip_address(host)
                except ValueError:
                    if not re.fullmatch(r'(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host): raise ValueError()
            except ValueError:
                raise NpcError('服务端地址请填写 主机:端口，例如 nps.example.com:8024')
    key = clean(body.get('vkey', ''), 1024) or old.get('vkey', '')
    protocol = body.get('type', 'tcp')
    if protocol not in ('tcp', 'tls', 'kcp', 'quic', 'ws', 'wss'):
        raise NpcError('请选择受支持的协议类型')
    dns = clean(body.get('dns', ''), 100)
    if dns:
        try: ipaddress.ip_address(dns)
        except ValueError: raise NpcError('DNS 服务器请填写 IPv4 或 IPv6 地址')
    if body['enabled'] and (not server or not key):
        raise NpcError('启用前请填写服务端地址和验证密钥')
    return {'enabled': body['enabled'], 'server': server, 'vkey': key, 'type': protocol,
            'dns': dns, 'extra': extra_options(body.get('extra', ''))}
