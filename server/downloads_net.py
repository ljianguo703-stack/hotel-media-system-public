"""Smart-download helpers that touch the network or torrent metadata.

Everything returned by search sources is treated as data: URLs are validated before any request, redirects are
followed manually and re-validated, responses are size-limited, and nothing here ever builds a shell command."""
import hashlib
import ipaddress
import json
import re
import socket
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from urllib.error import HTTPError, URLError


class NetError(Exception):
    """Network/remote failure; `kind` is one of unreachable, auth, http, invalid, blocked, timeout."""
    def __init__(self, kind, message, status=None, body=b''):
        super().__init__(message); self.kind, self.status, self.body = kind, status, body


def host_is_private(host):
    try: addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError: return False
    for address in addresses:
        ip = ipaddress.ip_address(address.split('%')[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified: return True
    return False


def check_url(url, trusted_hosts=()):
    """Allow http(s) only. Private, loopback and link-local targets (cloud metadata, admin APIs) are refused unless the
    host is one the administrator configured as a local service (trusted_hosts)."""
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname: raise NetError('blocked', '只允许 http/https 地址')
    if parts.username or parts.password: raise NetError('blocked', '地址中不能携带账号密码')
    host = parts.hostname.lower()
    if (host, parts.port) in trusted_hosts or host in {h for h, p in trusted_hosts if p is None}: return url
    if host_is_private(host): raise NetError('blocked', '拒绝访问内网或本机地址：' + host)
    return url


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def request(url, data=None, headers=None, timeout=15, limit=8 * 1024 * 1024, trusted_hosts=(), method=None, redirects=3):
    """One HTTP request without proxies from the environment. Returns (status, headers with lower-case names, body, final_url).
    A redirect to a magnet: link is returned as status 'magnet' with the link in body."""
    for _ in range(redirects + 1):
        check_url(url, trusted_hosts)
        req = Request(url, data=data, headers=headers or {}, method=method)
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(req, timeout=timeout) as response:
                body = response.read(limit + 1)
                if len(body) > limit: raise NetError('invalid', '响应过大')
                return response.status, {k.lower(): v for k, v in response.headers.items()}, body, url  # header names are case-insensitive
        except HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get('Location'):
                location = exc.headers['Location']
                if location.startswith('magnet:'): return 'magnet', {}, location.encode(), url
                url = urljoin(url, location); data = None if exc.code == 303 else data; continue
            try: detail = exc.read(4096)
            except OSError: detail = b''
            if exc.code in (401, 403): raise NetError('auth', '鉴权失败（HTTP %d）' % exc.code, exc.code, detail)
            raise NetError('http', 'HTTP %d' % exc.code, exc.code, detail)
        except socket.timeout: raise NetError('timeout', '连接超时')
        except URLError as exc:
            reason = getattr(exc, 'reason', exc)
            if isinstance(reason, socket.timeout): raise NetError('timeout', '连接超时')
            raise NetError('unreachable', '无法连接：' + str(reason)[:120])
        except (ConnectionError, OSError) as exc: raise NetError('unreachable', '无法连接：' + type(exc).__name__)
    raise NetError('invalid', '重定向次数过多')


def get_json(url, **kwargs):
    status, headers, body, _ = request(url, **kwargs)
    try: return json.loads(body.decode('utf-8') or 'null')
    except ValueError: raise NetError('invalid', '返回的不是 JSON')


# ---------- bencode / torrents ----------
def bdecode(data):
    def parse(i):
        c = data[i:i + 1]
        if c == b'i':
            end = data.index(b'e', i); return int(data[i + 1:end]), end + 1
        if c == b'l':
            i += 1; out = []
            while data[i:i + 1] != b'e': v, i = parse(i); out.append(v)
            return out, i + 1
        if c == b'd':
            i += 1; out = {}
            while data[i:i + 1] != b'e': k, i = parse(i); v, i = parse(i); out[k] = v
            return out, i + 1
        if c.isdigit():
            colon = data.index(b':', i); n = int(data[i:colon]); start = colon + 1
            if start + n > len(data): raise ValueError('truncated')
            return data[start:start + n], start + n
        raise ValueError('bad bencode')
    value, end = parse(0)
    if end != len(data): raise ValueError('trailing data')
    return value


def bencode(value):
    if isinstance(value, int): return b'i%de' % value
    if isinstance(value, str): value = value.encode()
    if isinstance(value, bytes): return b'%d:%s' % (len(value), value)
    if isinstance(value, list): return b'l' + b''.join(bencode(v) for v in value) + b'e'
    if isinstance(value, dict): return b'd' + b''.join(bencode(k) + bencode(value[k]) for k in sorted(value, key=lambda k: k if isinstance(k, bytes) else k.encode())) + b'e'
    raise TypeError(type(value))


def info_span(data):
    """Exact bytes of the info dictionary as stored (the hash must be computed over the original encoding)."""
    def skip(i):
        c = data[i:i + 1]
        if c == b'i': return data.index(b'e', i) + 1
        if c in (b'l', b'd'):
            i += 1
            while data[i:i + 1] != b'e': i = skip(i)
            return i + 1
        colon = data.index(b':', i); return colon + 1 + int(data[i:colon])
    if data[:1] != b'd': raise ValueError('not a dictionary')
    i, found = 1, None
    while data[i:i + 1] != b'e':
        colon = data.index(b':', i)
        end = colon + 1 + int(data[i:colon])
        key = data[colon + 1:end]
        stop = skip(end)
        if key == b'info':
            if found is not None: raise ValueError('duplicate info')
            found = data[end:stop]
        i = stop
    if found is None: raise ValueError('no info')
    return found


def torrent_info(data):
    meta = bdecode(data)
    if not isinstance(meta, dict) or b'info' not in meta: raise ValueError('not a torrent')
    info = meta[b'info']
    if info.get(b'meta version') == 2 and b'pieces' not in info: raise ValueError('仅支持v1或混合种子，请使用下载器原生页面处理v2种子')
    files = [{'path': '/'.join(p.decode('utf-8', 'replace') for p in f[b'path']), 'size': f[b'length']} for f in info.get(b'files', [])] or \
            [{'path': info.get(b'name', b'').decode('utf-8', 'replace'), 'size': info.get(b'length', 0)}]
    return {'infohash': hashlib.sha1(info_span(data)).hexdigest(), 'name': info.get(b'name', b'').decode('utf-8', 'replace'),
            'files': files, 'size': sum(f['size'] for f in files), 'private': bool(info.get(b'private'))}


def make_torrent(path, announce='', piece_length=262144, name=None):
    """Single-file or multi-file (directory) torrent for tests and self-made sources."""
    import os
    path = str(path)
    files = [path] if os.path.isfile(path) else sorted(os.path.join(d, f) for d, _, fs in os.walk(path) for f in fs)
    pieces, buffer, size = [], b'', 0
    for file in files:
        with open(file, 'rb') as f:
            while True:
                chunk = f.read(piece_length - len(buffer))
                if not chunk: break
                buffer += chunk; size += len(chunk)
                if len(buffer) == piece_length: pieces.append(hashlib.sha1(buffer).digest()); buffer = b''
    if buffer: pieces.append(hashlib.sha1(buffer).digest())
    info = {'name': name or path.rstrip('/').rsplit('/', 1)[-1], 'piece length': piece_length, 'pieces': b''.join(pieces)}
    if os.path.isfile(path): info['length'] = size
    else: info['files'] = [{'length': os.path.getsize(f), 'path': os.path.relpath(f, path).split(os.sep)} for f in files]
    meta = {'info': info}
    if announce: meta['announce'] = announce
    return bencode(meta)


def magnet_hash(uri):
    if not uri.startswith('magnet:?'): return ''
    for xt in parse_qs(urlsplit(uri).query).get('xt', []):
        m = re.fullmatch(r'urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})', xt)
        if m:
            value = m.group(1)
            if len(value) == 32:
                import base64
                value = base64.b32decode(value.upper()).hex()
            return value.lower()
    return ''


def normalize_url(url):
    """Canonical form used for duplicate detection: lowercase scheme/host, sorted query, no fragment, secrets dropped."""
    parts = urlsplit(url)
    query = sorted((k, v) for k, vs in parse_qs(parts.query, keep_blank_values=True).items() for v in vs if k.lower() not in ('apikey', 'api_key', 'passkey', 'token'))
    return '%s://%s%s?%s' % (parts.scheme.lower(), (parts.hostname or '').lower() + (':%d' % parts.port if parts.port else ''), parts.path, urlencode(query))
