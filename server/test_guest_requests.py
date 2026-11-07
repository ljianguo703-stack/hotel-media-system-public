import concurrent.futures
import http.client
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

from guest_http import make_guest_server
from management import Store
import test_management as fixture


class GuestRequestsE2E(unittest.TestCase):
    request = fixture.ManagementE2E.request
    state = fixture.ManagementE2E.state
    mutate = fixture.ManagementE2E.mutate

    def setUp(self):
        fixture.ManagementE2E.setUp(self)
        self.guest = make_guest_server('127.0.0.1', 0, self.store, Path(__file__).resolve().parents[1] / 'admin')
        threading.Thread(target=self.guest.serve_forever, daemon=True).start()

    def tearDown(self):
        self.guest.shutdown()
        self.guest.server_close()
        fixture.ManagementE2E.tearDown(self)

    def settings(self, **changes):
        result = {'enabled': True, 'public_url': 'https://wish.example.test', 'prompt': '想看什么影片？扫码留言',
                  'first_delay_seconds': 180, 'interval_seconds': 1800, 'visible_seconds': 20}
        result.update(changes)
        return result

    def enable(self, **changes):
        response = self.mutate('/operations', {'kind': 'guest_request', 'action': 'guest_settings', 'settings': self.settings(**changes)})
        self.assertEqual(response[0], 200, response[2])
        return self.admin_state()['settings']

    def admin_state(self):
        response = self.request('/api/admin/operations?section=guest_requests', auth=True)
        self.assertEqual(response[0], 200)
        return response[2]

    def guest_request(self, path, fields=None, cookie='', headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.guest.server_port, timeout=10)
        request_headers = {'Cookie': cookie, **(headers or {})}
        if fields is not None:
            request_headers['Content-Type'] = 'application/x-www-form-urlencoded'
        connection.request('POST' if fields is not None else 'GET', path,
                           body=urlencode(fields) if fields is not None else None, headers=request_headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode()
        connection.close()
        return result

    def form(self):
        settings = self.enable()
        parsed = urlsplit(settings['entry_url'])
        status, headers, body = self.guest_request(parsed.path + '?' + parsed.query)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Referrer-Policy'], 'same-origin')
        fields = {'entry': parse_qs(parsed.query)['entry'][0], 'message': '想看 星际穿越 🎬',
                  'submission_id': re.search(r'name="submission_id" value="([a-f0-9]+)"', body)[1]}
        return fields, headers['Set-Cookie'].split(';', 1)[0]

    def test_old_database_defaults_and_public_listener_routes(self):
        self.assertFalse(self.admin_state()['settings']['enabled'])
        for path in ('/request', '/', '/api/admin/state', '/media/clip.mp4'):
            self.assertEqual(self.guest_request(path)[0], 404)
        reopened = Store(self.data, self.media, self.config, [self.media])
        self.assertEqual(reopened.state()['revision'], self.state()['revision'])

    def test_settings_validation_stale_revision_and_generated_tv_qr(self):
        for changes in ({'public_url': 'ftp://example.test'}, {'public_url': 'https://a.test/path'},
                        {'public_url': ''}, {'visible_seconds': 60, 'interval_seconds': 60},
                        {'prompt': ''}, {'interval_seconds': True}):
            response = self.mutate('/operations', {'kind': 'guest_request', 'action': 'guest_settings', 'settings': self.settings(**changes)})
            self.assertEqual(response[0], 400, response[2])
        settings = self.enable()
        response = self.request('/api/v1/catalog', headers={'X-TV-Token': 'tv-test-token'}, tv=True)
        public = response[2]['home']['guest_request']
        self.assertTrue(public['enabled'])
        self.assertNotIn('entry_token', json.dumps(public))
        self.assertNotIn('public_url', public)
        status, _, raw = self.request(public['qr'], headers={'X-TV-Token': 'tv-test-token'}, tv=True, raw=True)
        self.assertEqual(status, 200)
        self.assertTrue(raw.startswith(b'\x89PNG'))
        response = self.request('/api/admin/operations', {'kind': 'guest_request', 'action': 'guest_settings', 'settings': self.settings(), 'revision': 0}, auth=True)
        self.assertEqual(response[0], 409)
        self.assertIn('/request?entry=', settings['entry_url'])

    def test_domain_and_ip_inputs_normalize_persist_and_generate_qr(self):
        cases = {
            'www.test.com': 'https://www.test.com',
            ' wish.example.test:8443/ ': 'https://wish.example.test:8443',
            '203.0.113.10': 'http://203.0.113.10',
            '203.0.113.10:8080': 'http://203.0.113.10:8080',
            'http://203.0.113.10:80/': 'http://203.0.113.10',
            'https://203.0.113.10:443': 'https://203.0.113.10',
            'http://Wish.Example.test:8080': 'http://wish.example.test:8080',
            'HTTPS://Wish.Example.test:443/': 'https://wish.example.test',
            'https://wish.example.test.': 'https://wish.example.test.',
            '[2001:db8::1]:8080': 'http://[2001:db8::1]:8080',
            'https://[2001:db8::1]': 'https://[2001:db8::1]',
            'https://影片.example': 'https://xn--p3t423b.example'
        }
        for entered, expected in cases.items():
            with self.subTest(entered=entered):
                saved = self.enable(public_url=entered)
                self.assertEqual(saved['public_url'], expected)
                self.assertTrue(saved['entry_url'].startswith(expected + '/request?entry='))
                self.assertTrue((self.media / saved['qr'].removeprefix('/media/')).read_bytes().startswith(b'\x89PNG'))
                reopened = Store(self.data, self.media, self.config, [self.media])
                self.assertEqual(reopened.guest_admin_state({})['settings']['public_url'], expected)

    def test_invalid_entry_addresses_remain_rejected(self):
        for address in ('javascript:alert(1)', '//wish.example.test', 'https://user:pass@wish.example.test',
                        'https://wish.example.test?entry=bad', 'wish.example.test/path',
                        'wish.example.test#fragment', '203.0.113.10:0', '203.0.113.10:65536',
                        '203.0.113.10:abc', '999.999.999.999', 'wish.exa mple.test',
                        'wish.example.test\n.evil', 'https://[fe80::1%25eth0]', 'https://-bad.example'):
            with self.subTest(address=address):
                result = self.mutate('/operations', {'kind': 'guest_request', 'action': 'guest_settings',
                                                     'settings': self.settings(public_url=address)})
                self.assertEqual(result[0], 400, result[2])

    def test_http_ip_cookie_and_submission_with_proxy_host(self):
        for entered, expected, secure in (('203.0.113.10:8080', 'http://203.0.113.10:8080', False),
                                         ('https://wish.example.test:443', 'https://wish.example.test', True)):
            with self.subTest(entered=entered):
                settings = self.enable(public_url=entered)
                parsed = urlsplit(settings['entry_url'])
                status, headers, body = self.guest_request(parsed.path + '?' + parsed.query,
                                                         headers={'Host': parsed.netloc})
                self.assertEqual(status, 200)
                self.assertEqual('; Secure' in headers['Set-Cookie'], secure)
                fields = {'entry': parse_qs(parsed.query)['entry'][0], 'message': 'IP 入口观影心愿',
                          'submission_id': re.search(r'name="submission_id" value="([a-f0-9]+)"', body)[1]}
                response = self.guest_request('/request', fields, headers['Set-Cookie'].split(';', 1)[0],
                                              {'Origin': expected, 'Host': '127.0.0.1:18792'})
                self.assertEqual(response[0], 200, response[2])
                self.assertEqual(self.guest_request('/request', fields, headers['Set-Cookie'].split(';', 1)[0],
                                                   {'Origin': 'http://foreign.example'})[0], 403)

    def test_real_form_submit_retry_restart_backup_and_revision_isolation(self):
        fields, cookie = self.form()
        revision = self.state()['revision']
        status, _, body = self.guest_request('/request', fields, cookie, {'Accept': 'application/json'})
        self.assertEqual(status, 200, body)
        receipt = json.loads(body)['receipt']
        self.assertEqual(json.loads(self.guest_request('/request', fields, cookie, {'Accept': 'application/json'})[2])['receipt'], receipt)
        self.assertEqual(self.state()['revision'], revision)
        rows = self.admin_state()['rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['message'], fields['message'])
        reopened = Store(self.data, self.media, self.config, [self.media])
        self.assertEqual(reopened.guest_admin_state({})['rows'], rows)
        backup = self.store.backup('admin')
        with sqlite3.connect(str(backup)) as db:
            self.assertEqual(db.execute('SELECT message FROM guest_requests').fetchone()[0], fields['message'])

    def test_concurrent_retry_stores_one_row_and_rejects_changed_body(self):
        fields, cookie = self.form()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.guest_request('/request', fields, cookie), range(4)))
        self.assertEqual([result[0] for result in results], [200] * 4)
        self.assertEqual(self.admin_state()['total'], 1)
        self.assertEqual(self.guest_request('/request', {**fields, 'message': '其他内容'}, cookie)[0], 409)

    def test_public_origin_survives_proxy_host_rewrite(self):
        fields, cookie = self.form()
        response = self.guest_request('/request', fields, cookie,
                                      {'Origin': 'https://wish.example.test', 'Host': '127.0.0.1:18792'})
        self.assertEqual(response[0], 200, response[2])

    def test_error_preserves_text_origin_validation_limits_and_disable(self):
        fields, cookie = self.form()
        self.assertEqual(self.guest_request('/request', fields, cookie, {'Origin': 'https://foreign.example'})[0], 403)
        status, _, body = self.guest_request('/request', {**fields, 'message': '<script>hi</script>' * 30}, cookie)
        self.assertEqual(status, 400)
        self.assertIn('&lt;script&gt;', body)
        for _ in range(5):
            fields['submission_id'] = secrets.token_hex(16)
            self.assertEqual(self.guest_request('/request', fields, cookie)[0], 200)
        fields['submission_id'] = secrets.token_hex(16)
        self.assertEqual(self.guest_request('/request', fields, cookie)[0], 429)
        self.enable(enabled=False)
        self.assertEqual(self.guest_request('/request', fields, cookie)[0], 404)

    def test_status_version_conflict_permissions_and_queue_filter(self):
        fields, cookie = self.form()
        self.assertEqual(self.guest_request('/request', fields, cookie)[0], 200)
        row = self.admin_state()['rows'][0]
        body = {'kind': 'guest_request', 'action': 'guest_status', 'id': row['id'], 'version': row['version'], 'status': 'processing'}
        self.assertEqual(self.mutate('/operations', body)[0], 200)
        self.assertEqual(self.mutate('/operations', body)[0], 409)
        self.assertEqual(self.request('/api/admin/operations?' + urlencode({'section':'guest_requests','status':'processing','q':'星际'}), auth=True)[2]['total'], 1)
        self.assertEqual(self.request('/api/admin/operations?section=guest_requests')[0], 401)
        self.assertEqual(self.mutate('/operations', {**body, 'status': {}})[0], 400)
        with self.store.db() as db:
            db.execute("UPDATE users SET role='viewer' WHERE name='admin'")
        self.assertEqual(self.request('/api/admin/operations?section=guest_requests', auth=True)[0], 403)
        self.assertEqual(self.mutate('/operations', body)[0], 403)


if __name__ == '__main__':
    unittest.main()
