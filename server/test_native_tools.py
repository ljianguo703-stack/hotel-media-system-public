"""Native applications must never inherit operator/viewer access."""
import unittest
import test_management as fixture
from management import password_hash


class NativeToolsAuth(unittest.TestCase):
    setUp = fixture.ManagementE2E.setUp
    tearDown = fixture.ManagementE2E.tearDown
    request = fixture.ManagementE2E.request

    def test_gate_requires_live_admin_session_and_returns_no_secrets(self):
        route = '/api/admin/native-tools-auth'
        self.assertEqual(self.request(route)[0], 401)
        self.assertEqual(self.request(route,auth=True,raw=True)[0],403)
        status, headers, raw = self.request(route, auth=True, raw=True,headers={'X-Hotel-Native-Port':'8098','X-Hotel-Native-Host':'127.0.0.1:8098','X-Hotel-Native-Local-Host':'127.0.0.1'})
        self.assertEqual((status, raw), (204, b''))
        self.assertEqual(headers['Cache-Control'], 'no-store')
        for role in ('operator', 'viewer'):
            with self.store.db() as db:
                db.execute('INSERT INTO users VALUES(?,?,?,1)', (role, password_hash('native-role-test-password'), role))
            _, headers, _ = self.request('/api/admin/login', {'username': role, 'password': 'native-role-test-password'})
            cookie = headers['Set-Cookie'].split(';')[0]
            self.assertEqual(self.request(route, headers={'Cookie': cookie})[0], 403)
        with self.store.db() as db:
            db.execute("UPDATE users SET enabled=0 WHERE name='admin'")
        self.assertEqual(self.request(route, auth=True)[0], 401)


if __name__ == '__main__':
    unittest.main()
