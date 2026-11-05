"""Search adapters: Prowlarr, Torznab and public Knaben/ApiBay/Nyaa endpoints.

Each source returns normalised candidates plus a per-source status, so "no source", "no result" and "some sources
failed" are distinguishable. Release names are data only: attributes parsed from them are marked as name-derived."""
import hashlib
import json
import re
import secrets
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import xml.etree.ElementTree as ET
from urllib.parse import urlencode, urlsplit

from downloads_net import NetError, get_json, magnet_hash, normalize_url, request
from downloads_parse import release_attributes

TORZNAB_NS = '{http://torznab.com/schemas/2015/feed}'
MOVIE_CATS, TV_CATS = '2000', '5000'
SOURCE_TYPES = ('prowlarr', 'torznab', 'knaben', 'apibay', 'nyaa', 'btsearch')
NYAA_NS = '{https://nyaa.si/xmlns/nyaa}'
_public_locks, _public_last = {}, {}
_lock_guard = threading.Lock()


def trusted(url):
    parts = urlsplit(url)
    return {((parts.hostname or '').lower(), parts.port)}


def candidate(source, title, size, link='', magnet='', infohash='', seeders=None, peers=None, protocol='torrent', published='', indexer=''):
    infohash = (infohash or magnet_hash(magnet) or '').lower()
    key = 'btih:' + infohash if infohash else ('url:' + hashlib.sha256(normalize_url(link or magnet).encode()).hexdigest() if (link or magnet) else 'title:' + title)
    return {'key': key, 'source': source, 'indexer': indexer or source, 'title': title[:300], 'size': int(size or 0), 'link': link, 'magnet': magnet,
            'infohash': infohash, 'seeders': seeders, 'peers': peers, 'protocol': protocol, 'published': published,
            'attrs': release_attributes(title), 'attrs_from': 'name', 'resource_type': 'file' if protocol == 'http' else 'magnet' if magnet and not link else 'torrent'}


def _integer(value, default=None):
    try: return max(0, int(value)) if value is not None and not isinstance(value, bool) else default
    except (ValueError, TypeError, OverflowError): return default


def _size(value):
    """Nyaa publishes IEC sizes (e.g. 635.2 MiB); JSON sources publish bytes."""
    if isinstance(value, str):
        match = re.fullmatch(r'\s*([\d.]+)\s*(B|[KMGT]i?B)\s*', value, re.I)
        if match:
            try:
                unit = match[2].upper()
                return int(float(match[1]) * ((1024 if 'I' in unit else 1000) ** ('BKMGT'.index(unit[0]) if unit != 'B' else 0)))
            except (ValueError, OverflowError): return 0
    return _integer(value, 0)


def _torrent(config, title, size, infohash, magnet='', **kwargs):
    """Public APIs must supply a real btih; do not pass HTML/detail URLs off as torrents."""
    digest = magnet_hash('magnet:?xt=urn:btih:' + str(infohash or '')) or magnet_hash(magnet)
    if not title or not isinstance(title, str) or not digest: return None
    if not magnet or magnet_hash(magnet) != digest:
        magnet = 'magnet:?' + urlencode({'xt': 'urn:btih:' + digest, 'dn': title})
    return candidate(config['id'], title, _size(size), infohash=digest, magnet=magnet, **kwargs)


class PublicSource:
    """No account or key. Use administrator-configured endpoints and bounded HTTP responses.

    Protocol references: Prowlarr's Knaben.cs and Indexers' thepiratebay.yml/nyaasi.yml.
    Nyaa requests are spaced by at least two seconds, including connection tests and fallback searches.
    """
    interval = 1.0

    def __init__(self, config, api_key=''):
        self.config = config

    def _fetch(self, url, payload=None, headers=None):
        key = (self.config['type'], self.config['url'])
        with _lock_guard: lock = _public_locks.setdefault(key, threading.Lock())
        with lock:
            delay = self.interval - (time.monotonic() - _public_last.get(key, -self.interval))
            if delay > 0: time.sleep(delay)
            _public_last[key] = time.monotonic()
            return request(url, data=json.dumps(payload).encode() if payload is not None else None,
                           headers={'User-Agent': 'HotelTV-Search/1.0', 'Accept': 'application/json, application/rss+xml, application/xml',
                                    **({'Content-Type': 'application/json'} if payload is not None else {}), **(headers or {})},
                           timeout=int(self.config.get('timeout', 20)), trusted_hosts=trusted(self.config['url']))[2]

    def _json(self, url, payload=None):
        try: return json.loads(self._fetch(url, payload).decode('utf-8'))
        except (ValueError, UnicodeError): raise NetError('invalid', '搜索源返回的不是 JSON')

    def test(self):
        found = self.search('Big Buck Bunny', '', 1)
        return {'status': 'ok', 'message': '搜索接口正常（测试返回 %d 条）' % len(found)}


class KnabenSource(PublicSource):
    def search(self, query, kind, limit=100):
        data = self._json(self.config['url'].rstrip('/'), {'query': query, 'search_type': '100%', 'search_field': 'title',
                         'from': 0, 'size': min(100, limit), 'order_by': 'seeders', 'order_direction': 'desc'})
        if not isinstance(data, dict) or not isinstance(data.get('hits'), list): raise NetError('invalid', 'Knaben 搜索响应格式无效')
        out = []
        for row in data['hits'][:limit]:
            if not isinstance(row, dict): continue
            c = _torrent(self.config, row.get('title'), row.get('bytes'), row.get('hash'), magnet=row.get('magnetUrl') or '',
                         seeders=_integer(row.get('seeders')), peers=_integer(row.get('peers')), published=str(row.get('date') or ''),
                         indexer='Knaben · ' + str(row.get('cachedOrigin') or '聚合')[:60])
            if c: out.append(c)
        return out


class _TitleText(HTMLParser):
    def __init__(self): super().__init__(convert_charrefs=True); self.parts = []
    def handle_data(self, value): self.parts.append(value)


class BTSearchSource(PublicSource):
    """Normal public search protocol used by btsearch.love's own shipped web client.

    x-sign is the public client checksum (MD5 over sorted parameters), not a user API key.
    See /_next/static/chunks/769-3c517b2ffab557ab.js. No page script is executed.
    """
    # Only this public hash index needs bootstrap trackers. Keep uploaded/private
    # torrents and other sources' original announce lists untouched.
    trackers = ('udp://tracker.opentrackr.org:1337/announce', 'udp://tracker.torrent.eu.org:451/announce',
                'udp://open.demonii.com:1337/announce')

    def search(self, query, kind, limit=100):
        base = self.config['url'].rstrip('/'); out = []; offset = 0
        self.total, self.warning = None, ''
        for _ in range(5):
            size = min(100, max(1, limit - offset))
            params = {'keyword': query, 'limit': str(size), 'offset': str(offset), 'mode': '', 'time': '', 'sort': '', 'sort_type': 'desc', 'size': ''}
            stamp, nonce = str(int(time.time())), secrets.token_hex(4)
            parts = ['timestamp=' + stamp, 'nonce=' + nonce] + [k + '=' + v for k, v in params.items()]
            checksum = hashlib.md5(('&'.join(sorted(parts)) + '&key=long2ice').encode()).hexdigest().upper()
            try:
                raw = self._fetch(base + '/api/search?' + urlencode(params), headers={'x-timestamp': stamp, 'x-nonce': nonce, 'x-sign': checksum,
                                 'Referer': base + '/search?' + urlencode({'keyword': query})})
                data = json.loads(raw.decode('utf-8'))
                if not isinstance(data, dict) or not isinstance(data.get('data'), list): raise ValueError('schema')
            except (ValueError, UnicodeError): raise NetError('invalid', 'BTSearch 搜索响应格式无效')
            except NetError as exc:
                if not out: raise
                self.warning = '已取回%d条，后续页暂不可用：%s' % (len(out), exc); break
            rows = data['data']; self.total = _integer(data.get('total'), len(rows))
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get('name'), str): continue
                parser = _TitleText(); parser.feed(row['name'])
                title = ''.join(parser.parts).strip()
                c = _torrent(self.config, title, row.get('size'), row.get('hash'), published=str(row.get('created_at') or ''), indexer='BTSearch')
                if c:
                    c['magnet'] += '&' + urlencode([('tr', tracker) for tracker in self.trackers])
                    c.update(file_count=_integer(row.get('count')), popularity=_integer(row.get('hot')))
                    out.append(c)
            offset += len(rows)
            if not rows or len(rows) < size or offset >= min(limit, self.total): break
        return out[:limit]


class ApiBaySource(PublicSource):
    def search(self, query, kind, limit=100):
        base = self.config['url'].rstrip('/')
        data = self._json(base + '/q.php?' + urlencode({'q': query, 'cat': 0}))
        if not isinstance(data, list): raise NetError('invalid', 'ApiBay 搜索响应格式无效')
        # Some API mirrors ignore unsupported queries and return the popular list. Reject unrelated rows.
        terms = re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', query).casefold())
        out = []
        for row in data:
            if not isinstance(row, dict) or str(row.get('id', '0')) == '0': continue
            title = str(row.get('name') or '')
            normal = unicodedata.normalize('NFKC', title).casefold()
            if not terms or not all(term in normal for term in terms): continue
            c = _torrent(self.config, title, row.get('size'), row.get('info_hash'), seeders=_integer(row.get('seeders')),
                         peers=_integer(row.get('leechers')), published=str(row.get('added') or ''), indexer='ApiBay')
            if c: out.append(c)
            if len(out) >= limit: break
        return out


class NyaaSource(PublicSource):
    interval = 2.0

    def search(self, query, kind, limit=100):
        raw = self._fetch(self.config['url'].rstrip('/') + '/?' + urlencode({'page': 'rss', 'q': query, 'c': '0_0'}))
        try: root = ET.fromstring(raw)
        except ET.ParseError: raise NetError('invalid', 'Nyaa 返回的不是 RSS XML')
        if root.tag != 'rss' or root.find('channel') is None: raise NetError('invalid', 'Nyaa RSS 格式无效')
        out = []
        for item in root.findall('./channel/item')[:limit]:
            enclosure = item.find('enclosure')
            link = (enclosure.get('url', '') if enclosure is not None else '') or item.findtext('link') or ''
            # Only the download link, never guid/detail page. A valid hash also works via DHT.
            link = link if urlsplit(link).path.endswith('.torrent') else ''
            c = _torrent(self.config, item.findtext('title'), item.findtext(NYAA_NS + 'size'), item.findtext(NYAA_NS + 'infoHash'),
                         link=link, seeders=_integer(item.findtext(NYAA_NS + 'seeders')), peers=_integer(item.findtext(NYAA_NS + 'leechers')),
                         published=item.findtext('pubDate') or '', indexer='Nyaa')
            if c: out.append(c)
        return out


class ProwlarrSource:
    def __init__(self, config, api_key):
        self.config, self.api_key = config, api_key

    def _headers(self): return {'X-Api-Key': self.api_key, 'Accept': 'application/json'}

    def test(self):
        if not self.config.get('url') or not self.api_key: return {'status': 'unconfigured', 'message': '未配置地址或 API Key'}
        base = self.config['url'].rstrip('/')
        info = get_json(base + '/api/v1/system/status', headers=self._headers(), timeout=8, trusted_hosts=trusted(base))
        return {'status': 'ok', 'message': '连接成功', 'version': str(info.get('version', ''))}

    def search(self, query, kind, limit=100):
        base = self.config['url'].rstrip('/')
        params = [('query', query), ('type', 'search'), ('limit', str(limit)), ('offset', '0')]
        for cat in ([MOVIE_CATS] if kind == 'movie' else [TV_CATS] if kind == 'series' else []): params.append(('categories', cat))
        data = get_json(base + '/api/v1/search?' + urlencode(params), headers=self._headers(), timeout=int(self.config.get('timeout', 20)), trusted_hosts=trusted(base))
        out = []
        for r in data if isinstance(data, list) else []:
            protocol = str(r.get('protocol', 'torrent')).lower()
            out.append(candidate(self.config['id'], str(r.get('title', '')), r.get('size'), link=str(r.get('downloadUrl') or ''), magnet=str(r.get('magnetUrl') or ''),
                                 infohash=str(r.get('infoHash') or ''), seeders=r.get('seeders'), peers=r.get('leechers'), protocol=protocol,
                                 published=str(r.get('publishDate', '')), indexer=str(r.get('indexer', ''))))
        return out


class TorznabSource:
    def __init__(self, config, api_key):
        self.config, self.api_key = config, api_key

    def _url(self, params):
        base = self.config['url']
        sep = '&' if '?' in base else '?'
        if self.api_key: params = params + [('apikey', self.api_key)]
        return base + sep + urlencode(params)

    def _fetch(self, params, timeout):
        status, headers, body, _ = request(self._url(params), timeout=timeout, trusted_hosts=trusted(self.config['url']), headers={'Accept': 'application/rss+xml, application/xml'})
        try: root = ET.fromstring(body)
        except ET.ParseError: raise NetError('invalid', '返回的不是 Torznab XML')
        if root.tag == 'error':
            code = root.attrib.get('code', '')
            raise NetError('auth' if code in ('100', '101', '102') else 'http', 'Torznab 错误 %s：%s' % (code, root.attrib.get('description', '')[:100]))
        return root

    def test(self):
        if not self.config.get('url'): return {'status': 'unconfigured', 'message': '未配置地址'}
        root = self._fetch([('t', 'caps')], 8)
        server = root.find('server')
        return {'status': 'ok', 'message': '连接成功', 'version': server.attrib.get('version', '') if server is not None else ''}

    def search(self, query, kind, limit=100):
        params = [('t', 'search'), ('q', query), ('limit', str(limit)), ('offset', '0')]
        if kind in ('movie', 'series'): params.append(('cat', MOVIE_CATS if kind == 'movie' else TV_CATS))
        root = self._fetch(params, int(self.config.get('timeout', 20)))
        out = []
        for item in root.iter('item'):
            attrs = {a.attrib.get('name'): a.attrib.get('value') for a in item.iter(TORZNAB_NS + 'attr')}
            enclosure = item.find('enclosure')
            link = (item.findtext('link') or (enclosure.attrib.get('url') if enclosure is not None else '') or '').strip()
            magnet = attrs.get('magneturl') or (link if link.startswith('magnet:') else '')
            size = attrs.get('size') or item.findtext('size') or (enclosure.attrib.get('length') if enclosure is not None else 0)
            seeders = attrs.get('seeders')
            out.append(candidate(self.config['id'], (item.findtext('title') or '').strip(), size, link='' if link.startswith('magnet:') else link, magnet=magnet,
                                 infohash=attrs.get('infohash', ''), seeders=int(seeders) if seeders and seeders.isdigit() else None,
                                 peers=int(attrs['peers']) if attrs.get('peers', '').isdigit() else None, published=item.findtext('pubDate') or ''))
        return out


def build(config, api_key):
    adapters = {'prowlarr': ProwlarrSource, 'torznab': TorznabSource, 'knaben': KnabenSource, 'apibay': ApiBaySource, 'nyaa': NyaaSource, 'btsearch': BTSearchSource}
    if config.get('type') not in adapters: raise NetError('invalid', '未知搜索源类型')
    return adapters[config['type']](config, api_key)


def search_all(sources, query, kind, limit=100):
    """Query every enabled source; return (deduplicated candidates, per-source status list)."""
    seen, merged, status = {}, [], []
    def search_one(entry):
        config, api_key = entry
        started = time.monotonic()
        try:
            runner = build(config, api_key); found = runner.search(query, kind, limit)[:limit]
            state = {'id': config['id'], 'name': config.get('name', config['id']), 'status': 'ok', 'count': len(found), 'ms': int((time.monotonic() - started) * 1000)}
            if getattr(runner, 'total', None) is not None: state['total'] = runner.total
            if getattr(runner, 'warning', ''): state.update(status='partial', message=runner.warning)
            return found, state
        except NetError as exc:
            return [], {'id': config['id'], 'name': config.get('name', config['id']), 'status': exc.kind, 'message': str(exc), 'count': 0}
        except (ValueError, TypeError, KeyError, AttributeError):
            return [], {'id': config['id'], 'name': config.get('name', config['id']), 'status': 'invalid', 'message': '搜索源响应格式无效', 'count': 0}
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(sources)))) as pool:
        results = list(pool.map(search_one, sources))
    for found, state in results:
        status.append(state)
        for c in found:
            if c['key'] in seen:
                kept = seen[c['key']]; kept.setdefault('also_from', []).append(c['source'])
                if (c.get('seeders') or 0) > (kept.get('seeders') or 0): kept['seeders'] = c['seeders']
                continue
            seen[c['key']] = c; merged.append(c)
    return merged, status
