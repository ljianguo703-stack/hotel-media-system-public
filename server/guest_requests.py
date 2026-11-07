"""Anonymous film suggestions, stored locally and managed through the existing console."""
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
from urllib.parse import quote, urlsplit, urlunsplit

import qr_image

DEFAULTS = {'enabled': False, 'public_url': '', 'prompt': '想看什么影片？扫码留言',
            'first_delay_seconds': 180, 'interval_seconds': 1800, 'visible_seconds': 20, 'qr': ''}
STATES = {'new': '待处理', 'processing': '处理中', 'done': '已处理'}


class GuestRequests:
    def initialize_guest_requests(self, db):
        db.execute('''CREATE TABLE IF NOT EXISTS guest_requests(
            id TEXT PRIMARY KEY, submission_id TEXT NOT NULL UNIQUE, message TEXT NOT NULL,
            created_at INTEGER NOT NULL, status TEXT NOT NULL, updated_at INTEGER NOT NULL,
            version INTEGER NOT NULL, client_tag TEXT NOT NULL)''')
        db.execute('CREATE INDEX IF NOT EXISTS guest_requests_created ON guest_requests(created_at)')
        db.execute('CREATE INDEX IF NOT EXISTS guest_requests_client ON guest_requests(client_tag,created_at)')
        db.execute("INSERT OR IGNORE INTO kv VALUES('guest_requests',?)",
                   (json.dumps({**DEFAULTS, 'entry_token': secrets.token_urlsafe(24)}, ensure_ascii=False),))

    @staticmethod
    def guest_settings(db):
        row = db.execute("SELECT value FROM kv WHERE key='guest_requests'").fetchone()
        return {**DEFAULTS, **(json.loads(row[0]) if row else {})}

    @staticmethod
    def guest_entry_url(settings):
        base = settings['public_url']
        return base + '/request?entry=' + quote(settings.get('entry_token', ''), safe='') if base else ''

    def guest_public_config(self, db):
        settings = self.guest_settings(db)
        return {key: settings[key] for key in ('enabled', 'prompt', 'qr', 'first_delay_seconds',
                                               'interval_seconds', 'visible_seconds')}

    def guest_settings_save(self, body, user, revision):
        from management import Problem, number, text
        item = body.get('settings')
        if not isinstance(item, dict) or type(item.get('enabled')) is not bool:
            raise Problem('留言设置格式无效')
        url = text(item.get('public_url', ''), 160)
        if url:
            try:
                parsed = urlsplit(url if '://' in url else '//' + url)
                if (parsed.scheme not in ('', 'http', 'https') or not parsed.hostname or parsed.username is not None or
                        parsed.password is not None or parsed.path not in ('', '/') or parsed.query or parsed.fragment or
                        parsed.port == 0 or any(c.isspace() or ord(c) < 32 or c in '\\<>"\'' for c in url)):
                    raise ValueError()
                host = parsed.hostname.encode('idna').decode('ascii').lower()
                try:
                    address = ipaddress.ip_address(host)
                except ValueError:
                    address = None
                    if (re.fullmatch(r'[0-9.]+', host) or not re.fullmatch(
                            r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\.?', host)):
                        raise ValueError()
                if '%' in host:
                    raise ValueError()
                scheme = parsed.scheme or ('http' if address else 'https')
                authority = '[' + str(address) + ']' if address and address.version == 6 else host
                if parsed.port is not None and parsed.port != (443 if scheme == 'https' else 80):
                    authority += ':' + str(parsed.port)
                url = urlunsplit((scheme, authority, '', '', ''))
            except (ValueError, UnicodeError):
                raise Problem('请填写留言网址或公网 IP，可带端口，例如 wish.example.com 或 203.0.113.10:8080')
        if item['enabled'] and not url:
            raise Problem('启用前请填写留言网址或公网 IP')
        settings = {'enabled': item['enabled'], 'public_url': url, 'prompt': text(item.get('prompt'), 60, True),
                    'first_delay_seconds': number(item.get('first_delay_seconds'), 0, 86400),
                    'interval_seconds': number(item.get('interval_seconds'), 60, 86400),
                    'visible_seconds': number(item.get('visible_seconds'), 10, 120), 'qr': ''}
        if settings['visible_seconds'] >= settings['interval_seconds']:
            raise Problem('每次停留时长应小于重复间隔')
        with self.file_lock, self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.check_revision(db, revision)
            settings['entry_token'] = self.guest_settings(db)['entry_token']
            target_url = self.guest_entry_url(settings)
            if target_url:
                if len(target_url.encode('utf-8')) > 213:
                    raise Problem('留言网址过长，请使用较短的地址')
                folder = self.media / '_home'
                folder.mkdir(parents=True, exist_ok=True)
                name = 'request-' + hashlib.sha256(target_url.encode()).hexdigest()[:20] + '.png'
                target = folder / name
                if not target.exists():
                    temporary = folder / ('.' + name + '.tmp')
                    temporary.write_bytes(qr_image.qr_png(target_url))
                    temporary.replace(target)
                settings['qr'] = '/media/_home/' + name
            self.put_kv(db, 'guest_requests', settings)
            self.changed(db, user['name'], '保存扫码留言设置', '启用' if settings['enabled'] else '停用')
        return {'saved': True}

    def guest_admin_state(self, args):
        from management import Problem, text
        from operations_system import page_number
        query = text(args.get('q', [''])[0], 200)
        status = args.get('status', [''])[0]
        if status and status not in STATES:
            raise Problem('留言状态无效')
        offset = page_number(args, 'offset', 0, 0, 1000000000)
        limit = page_number(args, 'limit', 30, 1, 100)
        clauses, parameters = ['message LIKE ?'], ['%' + query + '%']
        if status:
            clauses.append('status=?')
            parameters.append(status)
        where = ' AND '.join(clauses)
        with self.db() as db:
            settings = self.guest_settings(db)
            public = {key: settings[key] for key in DEFAULTS}
            public['entry_url'] = self.guest_entry_url(settings)
            counts = {row['status']: row['n'] for row in db.execute('SELECT status,COUNT(*) n FROM guest_requests GROUP BY status')}
            total = db.execute('SELECT COUNT(*) FROM guest_requests WHERE ' + where, parameters).fetchone()[0]
            rows = [dict(row) for row in db.execute(
                'SELECT id,message,created_at,status,updated_at,version FROM guest_requests WHERE ' + where +
                ' ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', [*parameters, limit, offset])]
        return {'settings': public, 'rows': rows, 'counts': counts, 'total': total,
                'listener': getattr(self, 'guest_listener', {'available': False, 'host': '127.0.0.1', 'port': 18792})}

    def guest_status_save(self, body, user):
        from management import Problem, now_ms, number, text
        ident = text(body.get('id'), 100, True)
        status = body.get('status')
        version = number(body.get('version'), 1, 1000000000)
        if not isinstance(status, str) or status not in STATES:
            raise Problem('留言状态无效')
        with self.db() as db:
            changed = db.execute('UPDATE guest_requests SET status=?,updated_at=?,version=version+1 WHERE id=? AND version=?',
                                 (status, now_ms(), ident, version)).rowcount
            if not changed:
                raise Problem('留言已被其他管理员更新，请刷新后重试', 409)
            self.audit(db, user['name'], '处理观众留言', ident + ' ' + STATES[status])
        return {'saved': True}

    def guest_entry(self, token):
        from management import Problem
        with self.db() as db:
            settings = self.guest_settings(db)
        if (not settings['enabled'] or not isinstance(token, str) or
                not hmac.compare_digest(token.encode(), settings.get('entry_token', '').encode())):
            raise Problem('留言入口暂不可用，请稍后重新扫码', 404)
        return settings

    def guest_submit(self, token, submission_id, message, session):
        from management import Problem, now_ms, text
        message = text(message, 500, True)
        if not isinstance(submission_id, str) or not re.fullmatch(r'[a-f0-9]{32}', submission_id):
            raise Problem('提交标识失效，请重新扫码')
        if not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32}', session):
            raise Problem('页面会话失效，请重新扫码')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            settings = self.guest_settings(db)
            if not settings['enabled'] or not hmac.compare_digest(str(token).encode(), settings['entry_token'].encode()):
                raise Problem('留言入口暂不可用，请稍后重新扫码', 404)
            existing = db.execute('SELECT id,message,client_tag FROM guest_requests WHERE submission_id=?', (submission_id,)).fetchone()
            client_tag = hmac.new(settings['entry_token'].encode(), session.encode(), hashlib.sha256).hexdigest()
            if existing:
                if existing['message'] != message or existing['client_tag'] != client_tag:
                    raise Problem('提交标识已使用，请重新打开留言页', 409)
                return existing['id']
            stamp = now_ms()
            recent = stamp - 600000
            count = db.execute('SELECT COUNT(*) FROM guest_requests WHERE created_at>=? AND client_tag=?', (recent, client_tag)).fetchone()[0]
            total = db.execute('SELECT COUNT(*) FROM guest_requests WHERE created_at>=?', (recent,)).fetchone()[0]
            if count >= 5 or total >= 600:
                raise Problem('留言较频繁，请稍后再试', 429)
            ident = 'gr-' + secrets.token_hex(12)
            db.execute('INSERT INTO guest_requests VALUES(?,?,?,?,?,?,?,?)',
                       (ident, submission_id, message, stamp, 'new', stamp, 1, client_tag))
        return ident
