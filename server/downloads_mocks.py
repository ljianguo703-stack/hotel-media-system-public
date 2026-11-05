"""Test doubles for smart-download e2e tests: Torznab, Prowlarr, qBittorrent WebUI API and aria2 JSON-RPC on loopback.

Boundary (documented in DL records): these mocks reproduce the HTTP contracts and *really write files* into the staging
directory, so verification, organising, scanning, compatibility and publishing run on real media. They are not the real
engines; real qBittorrent/aria2 runs are separate DL03 evidence."""
import json
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from downloads_net import magnet_hash, torrent_info


class MockHub:
    def __init__(self):
        self.torznab_items, self.prowlarr_items, self.files = [], [], {}
        self.knaben_hits, self.apibay_items, self.nyaa_rss, self.public_calls = [], [], b'', []
        self.qb, self.aria = {}, {}
        self.qb_password, self.aria_secret, self.prowlarr_key, self.torznab_key = 'qb-test', 'aria-test', 'prowlarr-test', 'tz-test'
        self.payloads = {}     # infohash or uri -> local source file the "engine" copies from
        self.seconds = 1.0     # simulated transfer time
        self.calls = {'qb_add': 0, 'aria_add': 0}
        self.broken = set()
        hub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def send(self, code, body=b'', ctype='application/json', headers=None):
                if isinstance(body, (dict, list)): body = json.dumps(body).encode()
                if isinstance(body, str): body = body.encode()
                self.send_response(code); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(body)))
                for k, v in (headers or {}).items(): self.send_header(k, v)
                self.end_headers(); self.wfile.write(body)
            def body(self):
                n = int(self.headers.get('Content-Length') or 0); return self.rfile.read(n) if n else b''
            def do_GET(self): self.route('GET')
            def do_POST(self): self.route('POST')
            def route(self, method):
                url = urlsplit(self.path); q = {k: v[-1] for k, v in parse_qs(url.query).items()}
                prefix = url.path.split('/')[1]
                if prefix in hub.broken: return self.send(503, 'down', 'text/plain')
                if prefix == 'torznab': return hub.torznab(self, url, q)
                if prefix == 'prowlarr': return hub.prowlarr(self, url, q)
                if prefix in ('knaben', 'apibay', 'nyaa'):
                    hub.public_calls.append((prefix, method, json.loads(self.body()) if method == 'POST' else q))
                    if prefix == 'knaben': return self.send(200, {'hits': hub.knaben_hits})
                    if prefix == 'apibay': return self.send(200, hub.apibay_items)
                    return self.send(200, hub.nyaa_rss, 'application/rss+xml')
                if prefix == 'qb': return hub.qbittorrent(self, method, url, q)
                if prefix == 'aria2': return hub.aria2(self)
                return self.send(404, 'nf', 'text/plain')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = 'http://127.0.0.1:%d' % self.server.server_port

    def close(self): self.server.shutdown(); self.server.server_close()

    # ---------- search sources ----------
    def torznab(self, h, url, q):
        if url.path.startswith('/torznab/file/'):
            name = url.path.rsplit('/', 1)[-1]
            return h.send(200, self.files[name], 'application/x-bittorrent') if name in self.files else h.send(404, 'nf', 'text/plain')
        if q.get('apikey') != self.torznab_key: return h.send(200, '<error code="100" description="Invalid API Key"/>', 'application/xml')
        if q.get('t') == 'caps': return h.send(200, '<caps><server version="1.0" title="mock"/></caps>', 'application/xml')
        from xml.sax.saxutils import escape, quoteattr
        items = ''.join('<item><title>%s</title><size>%d</size><link>%s</link><torznab:attr name="seeders" value="%d"/>%s%s</item>' % (
            escape(i['title']), i['size'], escape(self.base + '/torznab/file/' + i['file'] if i.get('file') else i.get('link', '')), i.get('seeders', 5),
            '<torznab:attr name="infohash" value="%s"/>' % i['infohash'] if i.get('infohash') else '',
            '<torznab:attr name="magneturl" value=%s/>' % quoteattr(i['magnet']) if i.get('magnet') else '') for i in self.torznab_items if not q.get('q') or i.get('match', q['q'].split(' ')[0]) in q['q'] or q['q'].split(' ')[0].lower() in i['title'].lower())
        return h.send(200, '<?xml version="1.0"?><rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>%s</channel></rss>' % items, 'application/rss+xml')

    def prowlarr(self, h, url, q):
        if h.headers.get('X-Api-Key') != self.prowlarr_key: return h.send(401, {'message': 'Unauthorized'})
        if url.path.endswith('/system/status'): return h.send(200, {'version': '1.24.3-mock'})
        if url.path.endswith('/search'): return h.send(200, self.prowlarr_items)
        return h.send(404, {})

    # ---------- qBittorrent ----------
    def qbittorrent(self, h, method, url, q):
        path = url.path[3:]
        form = {}
        raw = h.body() if method == 'POST' else b''
        ctype = h.headers.get('Content-Type', '')
        if 'multipart' in ctype:
            boundary = ctype.split('boundary=')[1].encode()
            for part in raw.split(b'--' + boundary):
                if b'Content-Disposition' not in part: continue
                head, _, data = part.partition(b'\r\n\r\n'); data = data[:-2]
                name = head.split(b'name="')[1].split(b'"')[0].decode()
                form[name] = data if b'filename=' in head else data.decode()
        elif raw: form = {k: v[-1] for k, v in parse_qs(raw.decode()).items()}
        if path == '/api/v2/auth/login':
            if form.get('password') == self.qb_password: return h.send(200, 'Ok.', 'text/plain', {'Set-Cookie': 'SID=mocksid; HttpOnly; path=/'})
            return h.send(200, 'Fails.', 'text/plain')
        if 'SID=mocksid' not in (h.headers.get('Cookie') or ''): return h.send(403, 'Forbidden', 'text/plain')
        if path == '/api/v2/app/webapiVersion': return h.send(200, '2.11.4', 'text/plain')
        if path == '/api/v2/app/version': return h.send(200, 'v5.0.0-mock', 'text/plain')
        if path == '/api/v2/torrents/categories': return h.send(200, {'hoteltv': {}})
        if path == '/api/v2/torrents/createCategory': return h.send(200, '', 'text/plain')
        if path == '/api/v2/torrents/add':
            self.calls['qb_add'] += 1
            info = torrent_info(form['torrents']) if 'torrents' in form else {'infohash': magnet_hash(form['urls']), 'name': 'magnet', 'files': [], 'size': 0}
            t = {'hash': info['infohash'], 'name': info['name'], 'save_path': form.get('savepath', ''), 'size': info['size'], 'progress': 0.0, 'state': 'downloading',
                 'category': form.get('category', ''), 'tags': form.get('tags', ''), 'files': info['files'], 'dlspeed': 0, 'completed': 0, 'paused': False}
            self.qb[t['hash']] = t
            threading.Thread(target=self._qb_transfer, args=(t,), daemon=True).start()
            return h.send(200, 'Ok.', 'text/plain')
        if path == '/api/v2/torrents/info':
            rows = [self._qb_public(t) for k, t in self.qb.items() if not q.get('hashes') or k in q['hashes'].split('|')]
            return h.send(200, rows)
        if path == '/api/v2/torrents/files':
            t = self.qb.get(q.get('hash'));
            return h.send(200, [{'name': f['path'] if len(t['files']) == 1 else t['name'] + '/' + f['path'], 'size': f['size'], 'progress': t['progress']} for f in t['files']] if t else [])
        if path in ('/api/v2/torrents/stop', '/api/v2/torrents/start'):
            for k in form.get('hashes', '').split('|'):
                if k in self.qb: self.qb[k]['paused'] = path.endswith('stop')
            return h.send(200, '', 'text/plain')
        if path == '/api/v2/torrents/delete':
            for k in form.get('hashes', '').split('|'): self.qb.pop(k, None)
            return h.send(200, '', 'text/plain')
        if path == '/api/v2/torrents/setDownloadLimit': return h.send(200, '', 'text/plain')
        if path == '/api/v2/torrents/reannounce':
            self.calls['qb_reannounce'] = self.calls.get('qb_reannounce', 0) + 1
            return h.send(200, '', 'text/plain')
        return h.send(404, 'nf', 'text/plain')

    def _qb_public(self, t):
        state = 'stoppedDL' if t['paused'] and t['progress'] < 1 else 'stalledUP' if t['progress'] >= 1 else t['state']
        return {'hash': t['hash'], 'name': t['name'], 'save_path': t['save_path'], 'size': t['size'], 'progress': t['progress'], 'state': state,
                'category': t['category'], 'tags': t['tags'], 'dlspeed': t['dlspeed'], 'completed': t['completed'], 'eta': 8640000 if t['progress'] >= 1 else 3, 'num_seeds': 1}

    def _qb_transfer(self, t):
        src = self.payloads.get(t['hash'])
        if not src: t['state'] = 'error'; return
        src = Path(src); multi = src.is_dir()
        if not t['files'] and not multi:  # emulate resolving magnet metadata from the known payload
            t.update(name=src.name, size=src.stat().st_size, files=[{'path': src.name, 'size': src.stat().st_size}])
        pairs = [(src / f['path'], Path(t['save_path']) / t['name'] / f['path']) for f in t['files']] if multi else [(src, Path(t['save_path']) / t['files'][0]['path'])]
        total = sum(s.stat().st_size for s, _ in pairs); steps = 10
        for source, dest in pairs:
            dest.parent.mkdir(parents=True, exist_ok=True); data = source.read_bytes()
            with open(dest, 'wb') as f:
                for i in range(steps):
                    while t['paused'] and t['hash'] in self.qb: time.sleep(0.05)
                    if t['hash'] not in self.qb: return
                    chunk = data[i * len(data) // steps:(i + 1) * len(data) // steps]; f.write(chunk); f.flush()
                    t['completed'] += len(chunk); t['progress'] = t['completed'] / total; t['dlspeed'] = int(len(chunk) / (self.seconds / steps / len(pairs)))
                    time.sleep(self.seconds / steps / len(pairs))
        t['progress'] = 1.0; t['dlspeed'] = 0

    # ---------- aria2 ----------
    def aria2(self, h):
        req = json.loads(h.body()); params = list(req.get('params', []))
        if not params or params[0] != 'token:' + self.aria_secret: return h.send(400, {'jsonrpc': '2.0', 'id': req.get('id'), 'error': {'code': 1, 'message': 'Unauthorized'}})
        params = params[1:]; method = req['method']; result = 'OK'
        if method == 'aria2.getVersion': result = {'version': '1.37.0-mock'}
        elif method == 'aria2.addUri':
            self.calls['aria_add'] += 1
            opts = params[1]; gid = opts['gid']
            name = opts.get('out') or params[0][0].rsplit('/', 1)[-1]
            t = {'gid': gid, 'dir': opts['dir'], 'path': str(Path(opts['dir']) / name), 'status': 'active', 'total': 0, 'done': 0, 'speed': 0, 'paused': False, 'uri': params[0][0]}
            self.aria[gid] = t; threading.Thread(target=self._aria_transfer, args=(t,), daemon=True).start(); result = gid
        elif method == 'aria2.tellStatus':
            t = self.aria.get(params[0])
            if not t: return h.send(400, {'jsonrpc': '2.0', 'id': req['id'], 'error': {'code': 1, 'message': 'GID %s is not found' % params[0]}})
            status = 'paused' if t['paused'] and t['status'] == 'active' else t['status']
            result = {'status': status, 'totalLength': str(t['total']), 'completedLength': str(t['done']), 'downloadSpeed': str(t['speed']), 'dir': t['dir'],
                      'files': [{'path': t['path'], 'length': str(t['total']), 'completedLength': str(t['done'])}]}
        elif method in ('aria2.forcePause', 'aria2.unpause'): self.aria[params[0]]['paused'] = method.endswith('Pause')
        elif method == 'aria2.forceRemove': self.aria.pop(params[0], None)
        return h.send(200, {'jsonrpc': '2.0', 'id': req['id'], 'result': result})

    def _aria_transfer(self, t):
        data = Path(self.payloads[t['uri']]).read_bytes(); t['total'] = len(data); Path(t['dir']).mkdir(parents=True, exist_ok=True); steps = 10
        with open(t['path'], 'wb') as f:
            for i in range(steps):
                while t['paused'] and t['gid'] in self.aria: time.sleep(0.05)
                if t['gid'] not in self.aria: return
                chunk = data[i * len(data) // steps:(i + 1) * len(data) // steps]; f.write(chunk); f.flush()
                t['done'] += len(chunk); t['speed'] = int(len(chunk) / (self.seconds / steps)); time.sleep(self.seconds / steps)
        t['status'] = 'complete'; t['speed'] = 0
