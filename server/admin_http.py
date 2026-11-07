"""Same-origin administration HTTP handlers, shared with the TV adapter."""
import csv
from http.cookies import SimpleCookie
import io
import json
import mimetypes
import os
from pathlib import Path
import secrets
import threading
import time
import npc_client
import remote_access
from urllib.parse import parse_qs, unquote, urlsplit

from management import Problem, encoded, now_ms, text
from software_http import SoftwareHTTP
from fleet_http import FleetHTTP


class AdminMixin(SoftwareHTTP, FleetHTTP):
    login_attempts = {}
    attempts_lock = threading.Lock()

    def secure_cookie(self):
        origin=urlsplit(self.headers.get('Origin',''))
        return '; Secure' if origin.scheme=='https' and origin.netloc==self.headers.get('Host') else ''

    def cookie_token(self):
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            return cookie['hotel_session'].value if 'hotel_session' in cookie else ''
        except Exception:
            return ''

    def admin_user(self, mutate=False, admin_only=False):
        user = self.store.session(self.cookie_token())
        if not user:
            raise Problem('请先登录后台', 401)
        if admin_only and user['role'] != 'admin':
            raise Problem('此操作需要管理员权限', 403)
        if mutate:
            if user['role'] == 'viewer': raise Problem('只读账号不能修改配置', 403)
            if not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), user['csrf']): raise Problem('操作校验失效，请重新登录', 403)
            self.same_origin()
        return user

    def same_origin(self):
        origin = self.headers.get('Origin')
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme not in ('http', 'https') or parsed.netloc != self.headers.get('Host'):
                raise Problem('拒绝跨站操作', 403)
        if self.headers.get('Sec-Fetch-Site') == 'cross-site': raise Problem('拒绝跨站操作', 403)

    def read_body(self, maximum=1024 * 1024):
        if self.headers.get('Transfer-Encoding'): raise Problem('请使用明确的 Content-Length', 411)
        try: size = int(self.headers.get('Content-Length', '0'))
        except ValueError: raise Problem('请求大小无效')
        if not 0 < size <= maximum: raise Problem('请求内容过大或为空', 413)
        raw = self.rfile.read(size)
        if len(raw) != size: raise Problem('上传未完整接收')
        try:
            value = json.loads(raw)
            if not isinstance(value, dict): raise ValueError()
            return value
        except (ValueError, UnicodeDecodeError): raise Problem('JSON 格式无效')

    def bytes_response(self, status, raw, content_type='application/octet-stream', extra=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('X-Frame-Options', 'DENY')
        for key, value in (extra or {}).items(): self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD': self.wfile.write(raw)

    def admin_get(self, route):
        if route == '/api/admin/fleet':
            return self.fleet_get()
        try:
            if route == '/api/admin/native-tools-auth':
                self.admin_user(admin_only=True)
                scheme = remote_access.native_scheme(self.store, self.headers, self.connection.getsockname()[0])
                return self.bytes_response(204, b'', extra={'X-Hotel-Native-Scheme': scheme} if scheme else None)
            if route == '/api/admin/remote-access':
                self.admin_user(admin_only=True)
                return self.send_json(200, remote_access.read(self.store, self.connection.getsockname()[0]))
            if route == '/api/admin/session':
                user = self.store.session(self.cookie_token())
                return self.send_json(200, {'user': user})
            if route == '/api/admin/software':
                self.admin_user(admin_only=True)
                return self.send_json(200, self.software_repository().state())
            if route.startswith('/api/admin/') or route.startswith('/media/'):
                user = self.admin_user()
                if route == '/api/admin/dashboard':
                    with self.store.db() as db:
                        settings=self.store.kv(db,'settings')
                        devices=[]
                        for record in db.execute('SELECT id,body,last_seen,ip FROM devices'):
                            device=json.loads(record['body']);device.update(id=record['id'],ip=record['ip'],online=bool(record['last_seen'] and 0<=now_ms()-record['last_seen']<120000))
                            devices.append(device)
                    snapshot=self.server.runtime_monitor.snapshot(devices,settings.get('timezone'),self.connection.getsockname()[0])
                    import shutil
                    try:
                        disk=shutil.disk_usage(self.store.media)
                        snapshot['server']['disk']={'total':disk.total,'used':disk.used,'percent':round(100*disk.used/disk.total,1)}
                    except OSError:snapshot['server']['disk']=None
                    return self.send_json(200,snapshot)
                if route == '/api/admin/npc':
                    if user['role'] != 'admin': raise Problem('远程管理配置需要管理员权限', 403)
                    return self.send_json(200, npc_client.request())
                if route.startswith('/api/admin/backups/'):
                    if user['role']!='admin':raise Problem('下载备份需要管理员权限',403)
                    path=self.store.backup_path(unquote(route[len('/api/admin/backups/'):]))
                    return self.bytes_response(200,path.read_bytes(),'application/vnd.sqlite3',{'Content-Disposition':'attachment; filename="'+path.name+'"'})
                if route == '/api/admin/operations':
                    args=parse_qs(urlsplit(self.path).query)
                    return self.send_json(200,self.store.operations_get(args.get('section',['entities'])[0],args,user,local_service_host=self.connection.getsockname()[0]))
                if route == '/api/admin/media-insights':
                    return self.send_json(200, self.store.media_insights())
                if route == '/api/admin/media-automation':
                    refresh = parse_qs(urlsplit(self.path).query).get('refresh', ['0'])[0] == '1'
                    return self.send_json(200, self.store.media_center_status(refresh=refresh and user['role'] == 'admin'))
                if route == '/api/admin/media-add':
                    return self.send_json(200, self.store.media_add_list())
                if route == '/api/admin/media-add/search':
                    args = parse_qs(urlsplit(self.path).query)
                    return self.send_json(200, self.store.media_add_search(args.get('q', [''])[0], args.get('kind', ['auto'])[0]))
                if route == '/api/admin/media-add/poster':
                    raw, kind = self.store.media_add_poster(parse_qs(urlsplit(self.path).query).get('p', [''])[0])
                    return self.bytes_response(200, raw, kind)
                if route == '/api/admin/state':
                    state = self.store.state()
                    if user['role'] != 'admin': state['users'] = []
                    return self.send_json(200, state)
                if route in ('/api/admin/reports', '/api/admin/reports.csv'):
                    args = parse_qs(urlsplit(self.path).query)
                    try: days = int(args.get('days', ['7'])[0])
                    except ValueError: raise Problem('报表天数无效')
                    reports = self.store.reports(days,{k:args[k][0] for k in ('from','until','tier','merchant','group','placement','dimension') if k in args})
                    if route.endswith('.csv'):
                        output = io.StringIO(); writer = csv.writer(output)
                        writer.writerow(['名称', '广告主', '设备组', '广告位', '档位', '请求', '开始/展示', '完成', '失败', '中断', '文字可见分钟'])
                        for row in reports['rows']:
                            # Spreadsheet formula injection is escaped in exported user-controlled strings.
                            name = row['name']; name = "'" + name if name.startswith(('=', '+', '-', '@', '\t', '\r')) else name
                            values=[name,row.get('merchant',''),row.get('group',''),row.get('placement',''),row['tier'],row['requested'],row['starts'],row['completes'],row['failed'],row['interrupted'],round(row['visible_ms']/60000,2)]
                            writer.writerow(["'"+v if isinstance(v,str) and v.startswith(('=','+','-','@','\t','\r')) else v for v in values])
                        return self.bytes_response(200, ('\ufeff' + output.getvalue()).encode(), 'text/csv; charset=utf-8', {'Content-Disposition': 'attachment; filename="hotel-ad-report.csv"'})
                    return self.send_json(200, reports)
                if route.startswith('/media/'):
                    return self.serve_media(route)
                raise Problem('接口不存在', 404)
            relative = 'index.html' if route in ('', '/') else unquote(route.lstrip('/'))
            root = self.admin_web.resolve(); path = (root / relative).resolve()
            if root not in path.parents or not path.is_file() or path.suffix not in ('.html', '.css', '.js', '.jpg', '.png', '.svg', '.webp', '.txt'):
                raise Problem('页面不存在', 404)
            mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
            return self.bytes_response(200, path.read_bytes(), mime + ('; charset=utf-8' if path.suffix in ('.html', '.css', '.js') else ''))
        except Problem as exc:
            return self.send_json(exc.status, {'error': str(exc)})

    def admin_post(self, route):
        if route.startswith('/api/admin/fleet/'):
            return self.fleet_post(route)
        try:
            if route == '/api/admin/login':
                self.same_origin()
                body = self.read_body(8192)
                ip = (self.client_address[0], str(body.get('username', ''))[:150].casefold())
                with self.attempts_lock:
                    cutoff = time.monotonic() - 300
                    if len(self.login_attempts) >= 4096:
                        self.login_attempts = {k: v for k, v in self.login_attempts.items() if v and v[-1] > cutoff}
                        if len(self.login_attempts) >= 4096: raise Problem('登录繁忙，请稍后重试', 429)
                    recent = [n for n in self.login_attempts.get(ip, []) if time.monotonic() - n < 300]
                    self.login_attempts[ip] = recent
                    if len(recent) >= 8: raise Problem('登录尝试过多，请 5 分钟后再试', 429)
                    recent.append(time.monotonic())
                token, user = self.store.login(body.get('username'), body.get('password'))
                with self.attempts_lock: self.login_attempts.pop(ip, None)
                return self.bytes_response(200, encoded({'user': user}).encode(), 'application/json; charset=utf-8', {'Set-Cookie': 'hotel_session=' + token + '; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200'+self.secure_cookie()})
            if route.startswith('/api/admin/software'):
                return self.software_post(route)
            admin_only = route.startswith(('/api/admin/users', '/api/admin/backups', '/api/admin/releases/', '/api/admin/sources', '/api/admin/npc', '/api/admin/media-cleanup/', '/api/admin/media-automation', '/api/admin/media-add'))
            if route == '/api/admin/logout':
                user = self.admin_user()
                self.same_origin()
                if not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), user['csrf']): raise Problem('操作校验失效，请重新登录', 403)
                self.store.logout(self.cookie_token())
                return self.bytes_response(200, b'{}', 'application/json', {'Set-Cookie': 'hotel_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'+self.secure_cookie()})
            user = self.admin_user(mutate=True, admin_only=admin_only)
            if route == '/api/admin/uploads':
                return self.upload_asset(user)
            body = self.read_body(1536 * 1024 if route == '/api/admin/media-add' else 1024 * 1024)   # a .torrent travels base64-encoded
            revision = body.get('revision')
            result = {'ok': True}
            if route == '/api/admin/remote-access':
                if user['role'] != 'admin': raise Problem('此操作需要管理员权限', 403)
                result = remote_access.save(self.store, body, user['name'], self.connection.getsockname()[0])
            elif route == '/api/admin/npc':
                if set(body) - {'action', 'config', 'revision'}: raise Problem('NPC 请求字段无效')
                action = body.get('action')
                if action not in ('save', 'start', 'stop', 'restart'): raise Problem('NPC 操作无效')
                result = npc_client.request(action, config=body.get('config'), revision=revision)
                with self.store.db() as db: self.store.audit(db, user['name'], 'NPC '+action, '远程管理客户端；未记录连接密钥')
            elif route == '/api/admin/operations': result = self.store.operations_post(body,user)
            elif route == '/api/admin/sources': result = self.store.add_source(body, user['name'], revision)
            elif route == '/api/admin/scan': result = self.store.start_scan(text(body.get('source_id'), 100, True), user['name'])
            elif route == '/api/admin/jellyfin/sync': result = self.store.start_jellyfin_sync(user['name'], bool(body.get('refresh')))
            elif route.startswith('/api/admin/categories/'):
                parts = route[len('/api/admin/categories/'):].split('/')
                ident = unquote(parts[0])
                if len(parts) == 1: result = self.store.save_category(ident, body, user['name'], revision)
                elif len(parts) == 2 and parts[1] in ('merge', 'delete'):
                    if user['role'] != 'admin': raise Problem('分类合并和删除需要管理员权限', 403)
                    if parts[1] == 'merge': result = self.store.merge_category(ident, body, user['name'], revision)
                    else: self.store.delete_category(ident, user['name'], revision)
                else: raise Problem('分类接口不存在', 404)
            elif route == '/api/admin/media-automation': result = self.store.media_center_action(body, user['name'])
            elif route == '/api/admin/media-add': result = self.store.media_add_action(body, user['name'])
            elif route == '/api/admin/media-rename': result = self.store.media_rename(body, user['name'])
            elif route == '/api/admin/media-cleanup/preview': result = self.store.cleanup_preview(body, user['name'], revision)
            elif route == '/api/admin/media-cleanup/execute': result = self.store.cleanup_execute(body, user['name'], revision)
            elif route == '/api/admin/media/batch': result = self.store.batch_media(body, user['name'], revision)
            elif route.startswith('/api/admin/media/'):
                result=self.store.save_media(unquote(route[len('/api/admin/media/'):]), body, user['name'], revision)
            elif route.startswith('/api/admin/campaign-bundles/'):
                result = self.store.save_campaign_bundle(unquote(route[len('/api/admin/campaign-bundles/'):]), body, user['name'], revision)
            elif route.startswith('/api/admin/campaigns/'):
                self.store.save_campaign(unquote(route[len('/api/admin/campaigns/'):]), body, user['name'], revision)
            elif route.startswith('/api/admin/devices/'):
                if user['role'] != 'admin': raise Problem('设备凭证管理需要管理员权限', 403)
                result = self.store.save_device(unquote(route[len('/api/admin/devices/'):]), body, user['name'], revision)
            elif route == '/api/admin/settings': self.store.settings(body, user['name'], revision)
            elif route == '/api/admin/users': self.store.save_user(body, user['name'], revision)
            elif route.startswith('/api/admin/releases/'):
                try: ident = int(route[len('/api/admin/releases/'):])
                except ValueError: raise Problem('历史配置编号无效')
                if body.get('confirm') is not True:raise Problem('请先预览恢复差异并明确确认')
                self.store.restore_release(ident, user['name'], revision)
            elif route == '/api/admin/backups':
                path = self.store.backup(user['name'])
                return self.bytes_response(200, path.read_bytes(), 'application/vnd.sqlite3', {'Content-Disposition': 'attachment; filename="' + path.name + '"'})
            else: raise Problem('接口不存在', 404)
            return self.send_json(200, result)
        except Problem as exc:
            return self.send_json(exc.status, {'error': str(exc)})
        except (sqlite3.IntegrityError, KeyError, ValueError, TypeError) as exc:
            return self.send_json(400, {'error': '数据格式或唯一标识冲突，请检查后重试'})
        except sqlite3.OperationalError:
            return self.send_json(503, {'error': '数据库忙或暂时无法写入，请稍后重试并检查磁盘空间'})

    def upload_asset(self, user):
        import hashlib
        from management import number
        if self.headers.get('Transfer-Encoding'): raise Problem('上传需要 Content-Length', 411)
        try: size = int(self.headers.get('Content-Length', '0'))
        except ValueError: raise Problem('上传大小无效')
        kind = parse_qs(urlsplit(self.path).query).get('kind', ['ads'])[0]
        if kind not in ('ads', 'posters', 'home'): raise Problem('上传分类无效')
        name = text(unquote(self.headers.get('X-Filename', '')), 250, True)
        suffix = Path(name).suffix.lower()
        allowed = {'.jpg', '.jpeg', '.png', '.webp'} if kind in ('posters', 'home') else {'.jpg', '.jpeg', '.png', '.webp', '.mp4', '.m4v', '.webm', '.mov'}
        if suffix not in allowed: raise Problem('请选择受支持的图片或视频文件')
        limit = 20 * 1024 * 1024 if kind in ('posters', 'home') else 512 * 1024 * 1024
        if not 0 < size <= limit: raise Problem('海报最大 20 MB，广告素材最大 512 MB', 413)
        disk = shutil.disk_usage(self.store.media)
        if disk.free - size < 1024 * 1024 * 1024: raise Problem('磁盘剩余空间不足，请先扩容', 507)
        folder = self.store.media / ('广告' if kind == 'ads' else '_home' if kind == 'home' else '_posters')
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / (secrets.token_hex(12) + suffix)
        temporary = folder / (dest.name + '.part')
        digest = hashlib.sha256()
        try:
            with temporary.open('xb') as output:
                remaining = size
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk: raise Problem('上传中断，文件未发布')
                    digest.update(chunk); output.write(chunk); remaining -= len(chunk)
                output.flush(); os.fsync(output.fileno())
            temporary.chmod(0o644); temporary.replace(dest)
        except Exception:
            # Only the incomplete temporary upload is cleaned up; original media is never touched.
            temporary.unlink(missing_ok=True)
            raise
        path = '/media/' + quote(dest.relative_to(self.store.media).as_posix(), safe='/')
        inventory={}
        if kind=='ads': inventory=self.store.register_asset(path,name,size,digest.hexdigest(),user['name'])
        else:
            with self.store.db() as db:self.store.audit(db,user['name'],'上传海报',name)
        return self.send_json(201, {'path': path, 'name': name, 'bytes': size, 'sha256': digest.hexdigest(),**inventory})


# Kept below imports above to keep the shared mixin independent of server.py.
import shutil
import sqlite3
from urllib.parse import quote
