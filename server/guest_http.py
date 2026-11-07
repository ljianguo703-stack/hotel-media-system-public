"""Loopback-only guest listener. Its routes expose a form and submission result."""
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
import json
import secrets
import socket
from string import Template
import threading
from urllib.parse import parse_qs, urlsplit

from management import Problem


def make_guest_server(host, port, store, web):
    template = Template((web / 'guest-request.html').read_text(encoding='utf-8'))

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(8)

        def log_message(self, *_args):
            pass

        def session(self):
            try:
                cookie = SimpleCookie(self.headers.get('Cookie', ''))
                return cookie['guest_session'].value if 'guest_session' in cookie else ''
            except CookieError:
                return ''

        def respond(self, status, raw, content_type, session=None, nonce='', secure=False):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'same-origin')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-" + nonce + "'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
            if session:
                self.send_header('Set-Cookie', 'guest_session=' + session + '; HttpOnly; SameSite=Lax; Path=/request; Max-Age=86400' + ('; Secure' if secure else ''))
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(raw)

        def page(self, status, token='', submission_id='', message='', error='', receipt='', session=None, secure=False):
            nonce = secrets.token_urlsafe(18)
            raw = template.substitute(entry=escape(token, quote=True), submission_id=escape(submission_id, quote=True),
                                      message=escape(message), error=escape(error), receipt=escape(receipt),
                                      nonce=nonce, form_hidden='hidden' if receipt else '',
                                      receipt_hidden='' if receipt else 'hidden').encode('utf-8')
            self.respond(status, raw, 'text/html; charset=utf-8', session, nonce, secure)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            try:
                if len(self.path) > 1024 or urlsplit(self.path).path != '/request':
                    raise Problem('留言页面不存在，请扫描电视上的二维码', 404)
                args = parse_qs(urlsplit(self.path).query, max_num_fields=3)
                token = args.get('entry', [''])[0]
                settings = store.guest_entry(token)
                session = self.session()
                import re
                if not re.fullmatch(r'[A-Za-z0-9_-]{32}', session):
                    session = secrets.token_urlsafe(24)
                self.page(200, token, secrets.token_hex(16), session=session,
                          secure=urlsplit(settings['public_url']).scheme == 'https')
            except (Problem, ValueError, UnicodeError) as exc:
                self.page(exc.status if isinstance(exc, Problem) else 400,
                          error=str(exc) if isinstance(exc, Problem) else '留言地址格式无效')

        def do_POST(self):
            token, submission_id, message = '', '', ''
            wants_json = 'application/json' in self.headers.get('Accept', '')
            try:
                if urlsplit(self.path).path != '/request':
                    raise Problem('留言接口不存在', 404)
                if self.headers.get('Transfer-Encoding'):
                    raise Problem('请求格式无效', 411)
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 8192:
                    raise Problem('留言内容过长或为空', 413)
                if self.headers.get('Content-Type', '').split(';', 1)[0] != 'application/x-www-form-urlencoded':
                    raise Problem('留言表单格式无效', 415)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise Problem('留言尚未完整接收，请重试')
                fields = parse_qs(raw.decode('utf-8'), keep_blank_values=True, max_num_fields=3, errors='strict')
                if set(fields) != {'entry', 'submission_id', 'message'} or any(len(values) != 1 for values in fields.values()):
                    raise Problem('留言表单格式无效')
                token, submission_id, message = (fields[key][0] for key in ('entry', 'submission_id', 'message'))
                settings = store.guest_entry(token)
                origin = self.headers.get('Origin')
                if origin:
                    parsed = urlsplit(origin)
                    public = urlsplit(settings['public_url'])
                    host = self.headers.get('Host', '')
                    local = urlsplit('//'+host).hostname in ('127.0.0.1', 'localhost', '::1')
                    same_public = (parsed.scheme, parsed.netloc) == (public.scheme, public.netloc)
                    same_local = local and parsed.scheme == 'http' and parsed.netloc == host
                    if not (same_public or same_local) or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
                        raise Problem('请从扫码打开的页面提交', 403)
                receipt = store.guest_submit(token, submission_id, message, self.session())
                if wants_json:
                    self.respond(200, json.dumps({'receipt': receipt}).encode(), 'application/json; charset=utf-8')
                else:
                    self.page(200, receipt=receipt)
            except (Problem, ValueError, UnicodeError, socket.timeout) as exc:
                status = exc.status if isinstance(exc, Problem) else 400
                error = str(exc) if isinstance(exc, Problem) else '提交未完成，请重试'
                if wants_json:
                    self.respond(status, json.dumps({'error': error}, ensure_ascii=False).encode(), 'application/json; charset=utf-8')
                else:
                    self.page(status, token, submission_id, message, error)

    class GuestServer(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 8

        def __init__(self, address, handler):
            self.slots = threading.BoundedSemaphore(8)
            super().__init__(address, handler)

        def process_request(self, request, client_address):
            if not self.slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except Exception:
                self.slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self.slots.release()

    server = GuestServer((host, port), Handler)
    store.guest_listener = {'available': True, 'host': host, 'port': server.server_port}
    return server
