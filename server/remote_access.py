"""Saved navigation addresses, independent of NPC credentials and tunnel creation."""
import ipaddress
import json
from urllib.parse import urlsplit, urlunsplit

SERVICES = (
    ('hotel', '酒店后台', 18791, 'http', '/', ''),
    ('jellyfin', 'Jellyfin', 8096, 'http', '/', ''),
    ('files', '文件中心', 8095, 'http', '/', ''),
    ('dockge', 'Dockge 容器管理', 5001, 'http', '/', ''),
    ('cockpit', '硬盘与系统管理', 9090, 'https', '/', '使用 Ubuntu 账号登录'),
    ('qbittorrent', 'qBittorrent', 8098, 'http', '/', '先在同一域名和协议下登录酒店后台'),
    ('jackett', 'Jackett', 9117, 'http', '/UI/Dashboard', '先在同一域名和协议下登录酒店后台'),
    ('prowlarr', 'Prowlarr', 9696, 'http', '/search', '先在同一域名和协议下登录酒店后台'),
    ('ariang', 'AriaNg 直链下载', 6801, 'http', '/', '使用 6801 网页入口，RPC 无需单独代理'),
    ('seerr', 'Seerr 加片', 5055, 'http', '/', '先登录同一域名和协议下的酒店后台'),
    ('radarr', 'Radarr 电影归档', 7878, 'http', '/', '先登录酒店后台'),
    ('sonarr', 'Sonarr 剧集归档', 8989, 'http', '/', '先登录酒店后台'),
    ('bazarr', 'Bazarr 字幕', 6767, 'http', '/', '先登录酒店后台'),
    ('ssh', 'SSH 维护', 22, 'ssh', '', '通过 SSH 客户端连接'),
)
GUARDED = {8098, 9117, 9696, 6801, 5055, 7878, 8989, 6767, 5001, 8095, 8096}


def problem(message, status=400):
    from management import Problem
    return Problem(message, status)


def host_text(value):
    try:
        host = str(ipaddress.ip_address(value))
    except (ValueError, TypeError):
        raise problem('Ubuntu 地址请填写有效的内网 IP')
    if ipaddress.ip_address(host).is_unspecified or ipaddress.ip_address(host).is_multicast:
        raise problem('Ubuntu 地址不能使用通配或组播地址')
    return host


def authority(host, port):
    return ('[' + host + ']' if ':' in host else host) + ':' + str(port)


def origin(url):
    p = urlsplit(url)
    return p.scheme.lower(), p.hostname.lower(), p.port or (443 if p.scheme == 'https' else 80)


def validate_url(value):
    if not isinstance(value, str) or len(value) > 1000:
        raise problem('公网地址格式无效')
    value = value.strip()
    if not value:
        return ''
    if any(ord(c) < 33 or c in '\\<>"\'' for c in value):
        raise problem('公网地址含有无效字符')
    try:
        p = urlsplit(value)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username is not None or p.password is not None:
            raise ValueError()
        if p.query or p.fragment or p.port == 0:
            raise ValueError()
        p.hostname.encode('idna')
        p.port
    except (ValueError, UnicodeError):
        raise problem('请输入完整的 HTTP / HTTPS 地址，不要包含账号、密钥或查询参数')
    return urlunsplit((p.scheme, p.netloc, p.path or '/', '', ''))


def stored(store, db, local_host):
    row = db.execute("SELECT value FROM kv WHERE key='remote_access'").fetchone()
    return json.loads(row[0]) if row else {'revision': 0, 'local_host': host_text(local_host), 'urls': {}}


def payload(config):
    return {**config, 'services': [
        {'id': ident, 'name': name, 'port': port, 'scheme': scheme, 'path': path,
         'target': authority(config['local_host'], port), 'note': note, 'protocol': 'TCP'}
        for ident, name, port, scheme, path, note in SERVICES]}


def read(store, local_host):
    with store.db() as db:
        return payload(stored(store, db, local_host))


def save(store, body, actor, local_host):
    if set(body) != {'revision', 'local_host', 'urls'} or type(body['revision']) is not int:
        raise problem('远程入口配置字段无效')
    host = host_text(body['local_host'])
    raw = body['urls']
    web = {s[0] for s in SERVICES if s[3] != 'ssh'}
    if not isinstance(raw, dict) or set(raw) - web:
        raise problem('远程入口服务无效')
    urls = {ident: validate_url(raw.get(ident, '')) for ident in web}
    guarded = [urls[s[0]] for s in SERVICES if s[2] in GUARDED and urls[s[0]]]
    if guarded:
        if not urls['hotel']:
            raise problem('请同时填写酒店后台公网地址，用于共享管理员登录')
        hotel_origin = origin(urls['hotel'])
        if any(origin(u)[:2] != hotel_origin[:2] for u in guarded):
            raise problem('下载工具与酒店后台须使用相同的域名和协议，端口可以不同')
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        old = stored(store, db, local_host)
        if body['revision'] != old['revision']:
            raise problem('入口配置已被其他管理员更新；请重新读取后再保存', 409)
        config = {'revision': old['revision'] + 1, 'local_host': host, 'urls': urls}
        store.put_kv(db, 'remote_access', config)
        store.audit(db, actor, '保存远程访问入口', '已填写 ' + str(sum(bool(u) for u in urls.values())) + ' 项；未创建 NPS 隧道')
    return payload(config)


def native_scheme(store, headers, local_host):
    """The native nginx proxy supplies original authority/origin, never a probe URL."""
    value = headers.get('X-Hotel-Native-Port')
    if value is None:
        raise problem('原生管理网关需更新配置，请先执行配套配置升级', 403)
    try:
        port = int(value)
        if port not in GUARDED:
            raise ValueError()
        host = headers.get('X-Hotel-Native-Host', '')
        if any(c in host for c in '/?#@\\'):
            raise ValueError()
        with store.db() as db:
            config = stored(store, db, host_text(headers.get('X-Hotel-Native-Local-Host', local_host)))
        allowed = [('http', config['local_host'], port)]
        service = next(s for s in SERVICES if s[2] == port)
        if config['urls'].get(service[0]):
            allowed.append(origin(config['urls'][service[0]]))
        candidates = [item for item in allowed if origin(item[0] + '://' + host) == item]
        if not candidates:
            raise ValueError()
        request_origin = headers.get('X-Hotel-Native-Origin', '')
        if request_origin:
            parsed = urlsplit(request_origin)
            if parsed.path not in ('', '/') or parsed.query or parsed.fragment or parsed.username is not None:
                raise ValueError()
        if request_origin and not any(origin(request_origin) == item for item in candidates):
            raise ValueError()
        return candidates[0][0]
    except (ValueError, TypeError, AttributeError):
        raise problem('此管理入口的地址或来源未获允许，请核对公网入口配置', 403)
