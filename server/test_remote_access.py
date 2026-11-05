import json
import unittest
from management import Store
import test_management as fixture


class RemoteAccessTests(unittest.TestCase):
    setUp = fixture.ManagementE2E.setUp
    tearDown = fixture.ManagementE2E.tearDown
    request = fixture.ManagementE2E.request

    def current(self):
        return self.request('/api/admin/remote-access', auth=True)[2]

    def save(self, **changes):
        c = self.current()
        body = {'revision': c['revision'], 'local_host': c['local_host'], 'urls': c['urls']}
        return self.request('/api/admin/remote-access', {**body, **changes}, auth=True)

    def test_catalog_targets_and_no_rpc_or_credentials(self):
        c = self.current()
        self.assertEqual(len(c['services']), 14)
        self.assertEqual({s['port'] for s in c['services']}, {18791, 8096, 8095, 5001, 9090, 8098, 9117, 9696, 6801, 22, 5055, 7878, 8989, 6767})
        self.assertTrue(all(s['target'].startswith('127.0.0.1:') for s in c['services']))
        self.assertNotIn('vkey', json.dumps(c))

    def test_save_survives_restart_and_is_independent_of_business_revision(self):
        with self.store.db() as db: before = self.store.kv(db, 'revision')
        status, _, c = self.save(local_host='192.168.4.185', urls={'hotel': 'https://remote.example:25806', 'files': 'https://remote.example:25807/'})
        self.assertEqual(status, 200)
        self.assertEqual(c['urls']['hotel'], 'https://remote.example:25806/')
        reopened = Store(self.data, self.media, self.config, [self.media, self.external])
        from remote_access import read
        self.assertEqual(read(reopened, '127.0.0.1')['urls'], c['urls'])
        with self.store.db() as db: self.assertEqual(self.store.kv(db, 'revision'), before)
        self.assertEqual(self.save(revision=0, urls={'hotel':'https://remote.example:25806/','files':'https://remote.example:25807/'})[0], 409)
        self.assertEqual(self.current()['urls'], c['urls'])

    def test_urls_reject_code_credentials_query_and_bad_ports_atomically(self):
        for value in ['javascript:alert(1)', '//evil.example', 'http://user:pass@host/', 'http://host:99999', 'https://host/?key=secret', 'https://host/#secret', 'https://host/\nX-Test:x', 'http://host/"bad']:
            with self.subTest(url=value):
                self.assertEqual(self.save(urls={'files': value})[0], 400)
                self.assertEqual(self.current()['revision'], 0)

    def test_guarded_tools_require_same_hostname_and_scheme_as_hotel(self):
        self.assertEqual(self.save(urls={'qbittorrent':'https://remote.example:25807/'})[0], 400)
        self.assertEqual(self.save(urls={'hotel':'https://remote.example:25806/', 'qbittorrent':'http://remote.example:25807/'})[0], 400)
        self.assertEqual(self.save(urls={'hotel':'https://remote.example:25806/', 'qbittorrent':'https://another.example:25807/'})[0], 400)
        self.assertEqual(self.save(urls={'hotel':'https://remote.example:25806/', 'qbittorrent':'https://remote.example:25807/'})[0], 200)

    def test_admin_csrf_and_origin_required(self):
        self.assertEqual(self.request('/api/admin/remote-access')[0], 401)
        body={'revision':0,'local_host':'192.168.4.185','urls':{}}
        self.assertEqual(self.request('/api/admin/remote-access',body,headers={'Cookie':self.cookie})[0], 403)
        self.assertEqual(self.request('/api/admin/remote-access',body,auth=True,headers={'Origin':'https://evil.example'})[0], 403)
        for role in ('operator','viewer'):
            with self.store.db() as db: db.execute('UPDATE users SET role=? WHERE name=?',(role,'admin'))
            self.assertEqual(self.request('/api/admin/remote-access',auth=True)[0], 403)
            self.assertEqual(self.request('/api/admin/remote-access',body,auth=True)[0], 403)

    def test_native_gate_only_accepts_saved_authority_and_matching_origin(self):
        self.save(local_host='192.168.4.185',urls={'hotel':'https://remote.example:25806/','qbittorrent':'https://remote.example:25807/'})
        base={'X-Hotel-Native-Port':'8098','X-Hotel-Native-Host':'remote.example:25807','X-Hotel-Native-Origin':'https://remote.example:25807'}
        code,h,_=self.request('/api/admin/native-tools-auth',auth=True,headers=base)
        self.assertEqual((code,h['X-Hotel-Native-Scheme']),(204,'https'))
        for changes in [{'X-Hotel-Native-Host':'evil.example:25807'}, {'X-Hotel-Native-Origin':'https://evil.example'}, {'X-Hotel-Native-Port':'6800'}, {'X-Hotel-Native-Origin':'http://remote.example:25807'}]:
            self.assertEqual(self.request('/api/admin/native-tools-auth',auth=True,headers={**base,**changes})[0],403)
        self.assertEqual(self.request('/api/admin/native-tools-auth',headers=base)[0],401)
        local={**base,'X-Hotel-Native-Host':'192.168.4.185:8098','X-Hotel-Native-Origin':'http://192.168.4.185:8098'}
        self.assertEqual(self.request('/api/admin/native-tools-auth',auth=True,headers=local)[0],204)

    def test_native_lan_gate_works_before_first_configuration_save(self):
        headers={'X-Hotel-Native-Port':'8098','X-Hotel-Native-Local-Host':'192.168.4.185','X-Hotel-Native-Host':'192.168.4.185:8098','X-Hotel-Native-Origin':'http://192.168.4.185:8098'}
        code,h,_=self.request('/api/admin/native-tools-auth',auth=True,headers=headers)
        self.assertEqual((code,h['X-Hotel-Native-Scheme']),(204,'http'))
        self.assertEqual(self.current()['revision'],0)


if __name__ == '__main__': unittest.main()
