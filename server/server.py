#!/usr/bin/env python3
"""LAN-only media adapter for the Hotel TV pilot. Python standard library only."""
import argparse
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit
from admin_http import AdminMixin
from management import Store, Problem, encoded

ROOT = Path(__file__).resolve().parent


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def database(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, device TEXT NOT NULL, received INTEGER NOT NULL, body TEXT NOT NULL)")
    return conn


def make_server(host, port, config_path, data_dir, media_dir, store=None, admin_web=None):
    data_dir, media_dir = Path(data_dir), Path(media_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    store = store or Store(data_dir, media_dir, config_path)
    db_path = data_dir / "events.sqlite3"
    database(db_path).close()

    class Handler(AdminMixin, BaseHTTPRequestHandler):
        server_version = "HotelTV/0.2"
        def setup(self):
            super().setup()
            self.connection.settimeout(60)

        @property
        def store(self):
            return store

        @property
        def admin_web(self):
            return Path(admin_web) if admin_web else None

        def log_message(self, fmt, *args):
            # Never put credentials, query strings, file names or room names into console logs.
            print("request", self.command, self.path.split("?", 1)[0].split("/")[1:3], flush=True)

        def device(self):
            token = self.headers.get("X-TV-Token", "")
            touch = urlsplit(self.path).path in ('/api/v1/catalog', '/api/v1/ads', '/api/v1/messages', '/api/v1/managed-status') and self.headers.get('X-TV-Probe') != '1'
            return store.device(token, self.client_address[0], touch)

        def send_json(self, status, data):
            raw = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            route = urlsplit(self.path).path
            if admin_web:
                return self.admin_get(route)
            device = self.device()
            if not device:
                return self.send_json(401, {"error": "device_auth_required"})
            if route == '/api/v1/messages':
                return self.fleet_tv_get(device)
            if route == "/api/v1/health":
                return self.send_json(200, {"ok": True, "server_time_ms": int(time.time() * 1000)})
            if route == "/api/v1/channels":
                content = store.tv_channels()
                content["version"] = hashlib.sha256(encoded(content).encode()).hexdigest()[:16]
                content["server_time_ms"] = int(time.time() * 1000)
                return self.send_json(200, content)
            if route in ("/api/v1/catalog", "/api/v1/ads"):
                try:
                    content = store.catalog(device) if route.endswith('catalog') else store.ad_manifest(device, record=self.headers.get('X-TV-Probe') != '1', protocol=2 if self.headers.get('X-TV-Ad-Protocol')=='2' else 1)
                    if route.endswith('catalog') and self.headers.get('X-TV-Pin-Protocol') == '2':
                        from pin_credentials import public_verifier
                        pin_file = data_dir / 'maintenance-pin.json'
                        if pin_file.is_file():
                            try: pin=json.loads(pin_file.read_text())
                            except (OSError, ValueError): pin={}
                            key=(device['id'],pin.get('revision'))
                            cache=store.__dict__.setdefault('_pin_verifiers',{})
                            if key not in cache:
                                if len(cache)>=1000:cache.clear()
                                cache[key]=public_verifier(pin,device['id'])
                            if cache[key]:content['maintenance']=cache[key]
                    content["version"] = hashlib.sha256(encoded(content).encode()).hexdigest()[:16]
                    content["server_time_ms"] = int(time.time() * 1000)
                    content["room"] = device.get('room', device['id'])
                    return self.send_json(200, content)
                except (OSError, ValueError):
                    return self.send_json(503, {"error": "manifest_unavailable"})
            if route.startswith("/media/"):
                if not store.tv_route_allowed(device, route): return self.send_json(404, {'error': 'media_not_published'})
                return self.serve_media(route)
            if route.startswith('/api/v1/subtitles/'):
                try:
                    match = re.fullmatch(r'/api/v1/subtitles/([a-f0-9]{32})\.vtt', route)
                    if not match: raise Problem('字幕不存在', 404)
                    raw = store.subtitle_document(match[1])
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/vtt; charset=utf-8')
                    self.send_header('Content-Length', str(len(raw)))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.end_headers()
                    if self.command != 'HEAD': self.wfile.write(raw)
                    return
                except Problem as exc:
                    return self.send_json(exc.status, {'error': 'subtitle_unavailable'})
            return self.send_json(404, {"error": "not_found"})

        def serve_media(self, route):
            try:
                path = store.media_path(unquote(route))
            except Problem:
                return self.send_json(404, {"error": "media_not_found"})
            size = path.stat().st_size
            start, end, status = 0, size - 1, 200
            header = self.headers.get("Range")
            if header:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
                if not match or not any(match.groups()):
                    return self.invalid_range(size)
                left, right = match.groups()
                if left:
                    start = int(left)
                    end = min(int(right) if right else end, end)
                else:
                    length = int(right)
                    start = max(0, size - length)
                if start > end or start >= size or (not left and int(right) == 0):
                    return self.invalid_range(size)
                status = 206
            self.send_response(status)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(max(0, end - start + 1)))
            if status == 206:
                self.send_header("Content-Range", "bytes %s-%s/%s" % (start, end, size))
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                with path.open("rb") as source:
                    source.seek(start)
                    remaining = end - start + 1
                    while remaining > 0:
                        block = source.read(min(256 * 1024, remaining))
                        if not block:
                            break
                        self.wfile.write(block)
                        remaining -= len(block)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Seeking or leaving the player intentionally closes the old request.

        def invalid_range(self, size):
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%s" % size)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            if admin_web:
                return self.admin_post(urlsplit(self.path).path)
            device = self.device()
            if not device:
                return self.send_json(401, {"error": "device_auth_required"})
            route = urlsplit(self.path).path
            if route in ('/api/v1/managed-status', '/api/v1/messages/receipt'):
                return self.fleet_tv_post(route, device)
            if urlsplit(self.path).path == '/api/v1/apps':
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 128 * 1024: return self.send_json(413, {'error': 'invalid_body_size'})
                    return self.send_json(200, store.save_tv_apps(device, json.loads(self.rfile.read(length))))
                except Problem as exc: return self.send_json(exc.status, {'error': str(exc)})
                except (ValueError, TypeError): return self.send_json(400, {'error': 'invalid_apps'})
            if urlsplit(self.path).path != "/api/v1/events":
                return self.send_json(404, {"error": "not_found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 256 * 1024:
                    return self.send_json(413, {"error": "invalid_body_size"})
                events = json.loads(self.rfile.read(length)).get("events")
                if not isinstance(events, list) or not 1 <= len(events) <= 100:
                    raise ValueError("invalid event batch")
                valid = all(isinstance(e, dict) and isinstance(e.get("id"), str)
                            and 1 <= len(e["id"]) <= 128 and isinstance(e.get("type"), str)
                            for e in events)
                if not valid:
                    raise ValueError("invalid events")
            except (ValueError, TypeError, AttributeError):
                return self.send_json(400, {"error": "invalid_events"})
            try: store.record_events(device, events)
            except Problem as exc: return self.send_json(exc.status, {'error':str(exc)})
            return self.send_json(200, {"accepted": [e["id"] for e in events]})

    class HotelHTTPServer(ThreadingHTTPServer):
        request_queue_size = 128
    server = HotelHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18790)
    parser.add_argument("--config", type=Path, default=ROOT / "data/devices.json")
    parser.add_argument("--data", type=Path, default=ROOT / "data")
    parser.add_argument("--media", type=Path, default=ROOT / "media")
    parser.add_argument('--admin-port', type=int)
    parser.add_argument('--admin-web', type=Path, default=ROOT.parent / 'admin')
    parser.add_argument('--library-root', action='append', default=[])
    args = parser.parse_args()
    store = Store(args.data, args.media, args.config, [args.media] + args.library_root)
    store.run_jellyfin_scheduler()
    store.run_management_scheduler()
    store.recover_compat_jobs()
    store.run_automation_scheduler()
    store.recover_download_tasks()
    server = make_server(args.host, args.port, args.config, args.data, args.media, store=store)
    store.tv_port=server.server_port
    admin_server = None
    if args.admin_port:
        if not args.admin_web.is_dir(): parser.error('admin web folder does not exist')
        admin_server = make_server(args.host, args.admin_port, args.config, args.data, args.media, store=store, admin_web=args.admin_web)
        threading.Thread(target=admin_server.serve_forever, daemon=True).start()
        print('Hotel TV administration listening on port', admin_server.server_port, flush=True)
    print("Hotel TV LAN server listening on port", server.server_port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if admin_server:
            admin_server.shutdown(); admin_server.server_close()
        server.server_close()


if __name__ == "__main__":
    main()
