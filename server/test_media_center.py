"""1.1.0 media automation center: health snapshot, one-click owner actions, secrecy and admin-only access."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import media_center
import test_management as fixture
from management import Problem

KEYS = {'radarr': 'radarr-secret-key', 'sonarr': 'sonarr-secret-key', 'prowlarr': 'prowlarr-secret-key',
        'bazarr': 'bazarr-secret-key', 'seerr': 'seerr-secret-key'}


class FakeNative:
    """One loopback server per app; records requests; behaviour set per test."""
    def __init__(self):
        self.calls, self.state = [], {
            'testall_valid': True, 'clients': {'radarr': [{'id': 3, 'name': 'Hotel qB', 'fields': [{'name': 'password', 'value': '********'}, {'name': 'host', 'value': 'x'}]}],
                                              'sonarr': []},
            'indexers': [{'id': 1, 'name': 'Nyaa.si', 'enable': True}, {'id': 2, 'name': 'Knaben', 'enable': True}],
            'indexerstatus': [], 'testall_status': 200,
            'profiles': {'radarr': {'id': 4, 'minFormatScore': 0, 'formatItems': [{'format': 9, 'name': 'Hotel HEVC x265', 'score': 0}]},
                         'sonarr': {'id': 6, 'minFormatScore': 0, 'formatItems': [{'format': 7, 'name': 'Hotel HEVC x265', 'score': 0}]}},
            'bazarr': {'general': {'enabled_providers': ['embeddedsubtitles']}, 'assrt': {'token': ''}, 'opensubtitlescom': {'username': ''}},
            'subtitle_profiles': [], 'subtitle_languages': [{'code2': 'en', 'enabled': True}],
            'subtitle_providers': [], 'subtitle_counts': {'movies': 3, 'episodes': 7},
            'tmdb': True, 'readback_broken': False}
        self.servers = {}
        for app in KEYS:
            handler = self.handler(app)
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.servers[app] = server

    def url(self, app): return 'http://127.0.0.1:%d' % self.servers[app].server_port

    def close(self):
        for s in self.servers.values(): s.shutdown(); s.server_close()

    def handler(self, app):
        native = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def body(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
                if self.headers.get('Content-Type', '').startswith('application/x-www-form-urlencoded'): return parse_qs(raw.decode())
                return json.loads(raw) if raw else None
            def reply(self, code, value):
                raw = json.dumps(value).encode(); self.send_response(code); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
            def handle_any(self, method):
                path = urlsplit(self.path).path; body = self.body() if method in ('POST', 'PUT') else None
                native.calls.append((app, method, self.path, body, self.headers.get('X-Api-Key')))
                if self.headers.get('X-Api-Key') != KEYS[app]: return self.reply(401, {'message': 'bad key'})
                st = native.state
                if path.endswith('/status'): return self.reply(200, {'version': '9.9'} if app != 'bazarr' else {'data': {'bazarr_version': '1.6'}})
                if path == '/api/v3/downloadclient/testall': return self.reply(200, [{'id': 3, 'isValid': st['testall_valid'], 'validationFailures': []}])
                if path == '/api/v3/health': return self.reply(200, [{'source': 'UpdateCheck', 'type': 'warning', 'message': 'update'}, {'source': 'RootFolderCheck', 'type': 'error', 'message': 'Missing root folder'}] if app == 'sonarr' else [])
                if path == '/api/v3/downloadclient' and method == 'GET': return self.reply(200, st['clients'][app])
                if path == '/api/v3/downloadclient/schema': return self.reply(200, [{'implementation': 'QBittorrent', 'fields': [{'name': 'host'}, {'name': 'port'}, {'name': 'username'}, {'name': 'password'}, {'name': 'tvCategory'}]}])
                if path == '/api/v3/downloadclient/test':
                    return self.reply(200, None) if st.get('client_test_valid', True) else self.reply(400, [{'errorMessage': 'Authentication failed'}])
                if path.startswith('/api/v3/downloadclient') and method in ('PUT', 'POST'):
                    if st.get('heal_on_write'): st['testall_valid'] = True   # browser fixture: the pushed password works
                    if method == 'POST' and path == '/api/v3/downloadclient': st['clients'][app] = st['clients'][app] + [{**body, 'id': 9}]
                    return self.reply(200, body)
                if path == '/api/v1/indexer': return self.reply(200, st['indexers'])
                if path == '/api/v1/indexerstatus': return self.reply(200, st['indexerstatus'])
                if path == '/api/v1/indexer/testall': return self.reply(st['testall_status'], [{'id': 2, 'isValid': st['testall_status'] == 200}])
                if path == '/api/v1/command': return self.reply(201, {'id': 1})
                if path.startswith('/api/v3/qualityprofile/'):
                    if method == 'PUT':
                        if not st['readback_broken']: st['profiles'][app] = body
                        return self.reply(202, body)
                    return self.reply(200, st['profiles'][app])
                if path == '/api/system/settings':
                    if method == 'POST':
                        st['bazarr_form'] = body
                        if not st.get('subtitle_readback_broken'):
                            for key, values in body.items():
                                if key.startswith('settings-'):
                                    section, field = key[len('settings-'):].split('-', 1)
                                    if st.get('subtitle_sync_gates_ignored') and section == 'subsync' and field in ('use_subsync_threshold', 'use_subsync_movie_threshold'): continue
                                    value = values if field == 'enabled_providers' else values[0]
                                    if value in ('true', 'false'): value = value == 'true'
                                    st['bazarr'].setdefault(section, {})[field] = value
                            if 'languages-profiles' in body: st['subtitle_profiles'] = json.loads(body['languages-profiles'][0])
                            if 'languages-enabled' in body: st['subtitle_languages'] = [{'code2': code, 'enabled': True} for code in body['languages-enabled']]
                        return self.reply(204, None)
                    return self.reply(200, st['bazarr'])
                if path == '/api/system/languages/profiles': return self.reply(200, st['subtitle_profiles'])
                if path == '/api/system/languages': return self.reply(200, st['subtitle_languages'])
                if path == '/api/providers': return self.reply(200, {'data': st['subtitle_providers']})
                if path in ('/api/movies/wanted', '/api/episodes/wanted'):
                    return self.reply(200, {'total': st['subtitle_counts'][path.split('/')[2]], 'data': []})
                if path == '/api/system/tasks':
                    if method == 'POST': return self.reply(204, None)
                    return self.reply(200, {'data': [{'job_id': task} for task in st.get('subtitle_tasks', ['wanted_search_missing_subtitles_movies', 'wanted_search_missing_subtitles_series'])]})
                if path == '/api/v1/search': return self.reply(200, {'results': (st.get('search_results') or [{'id': 9470}]) if st['tmdb'] else []})
                if path.startswith(('/api/v1/movie/', '/api/v1/tv/')):   # browser fixture: details of a search result
                    return self.reply(200, next((r for r in st.get('search_results') or [] if str(r['id']) == path.rsplit('/', 1)[1]), {'id': int(path.rsplit('/', 1)[1])}))
                return self.reply(404, {'message': 'unknown ' + path})
            def do_GET(self): self.handle_any('GET')
            def do_POST(self): self.handle_any('POST')
            def do_PUT(self): self.handle_any('PUT')
        return Handler


class MediaCenterE2E(unittest.TestCase):
    setUp_base = fixture.ManagementE2E.setUp
    tearDown_base = fixture.ManagementE2E.tearDown
    request = fixture.ManagementE2E.request
    state = fixture.ManagementE2E.state

    def setUp(self):
        self.setUp_base()
        self.native = FakeNative()
        config = {app: {'url': self.native.url(app), 'api_key': key} for app, key in KEYS.items()}
        config['radarr'].update(format=9, profile=4); config['sonarr'].update(format=7, profile=6)
        config.update(storage_root=str(self.media), compat_root=str(self.media / '_compat'))
        (self.data / 'media-automation.json').write_text(json.dumps(config))
        (self.data / 'download-secrets.json').write_text(json.dumps({'qbittorrent': {'password': 'current-qb-test-password'}}))
        with self.store.db() as db: self.store.put_kv(db, 'media_automation', {'enabled': True})
        big = type('Usage', (), {'total': 4 * 1024 ** 4, 'used': 0, 'free': 3 * 1024 ** 4})()
        self.disk = patch.object(media_center.shutil, 'disk_usage', return_value=big); self.disk.start()

    def tearDown(self):
        self.disk.stop(); self.native.close(); self.tearDown_base()

    def status(self): return self.store.media_center_status(refresh=True)

    def test_healthy_install_reports_services_and_only_real_issues(self):
        s = self.status()
        self.assertTrue(all(x['ok'] for x in s['services'])); self.assertEqual(len(s['services']), 5)
        titles = [i['title'] for i in s['issues']]
        self.assertIn('Sonarr 提示', titles)                      # real health error surfaced
        self.assertFalse(any('update' in i['detail'] for i in s['issues']))   # update notices ignored
        self.assertTrue(s['tmdb']); self.assertEqual(s['policy']['hevc'], 'allow')
        self.assertEqual({key: s['subtitles'][key] for key in ('embedded', 'assrt', 'opensubtitlescom')}, {'embedded': True, 'assrt': False, 'opensubtitlescom': False})
        self.assertEqual((s['subtitles']['wanted_movies'], s['subtitles']['wanted_episodes']), (3, 7))
        roles = ' · '.join(d['label'] for d in s['storage'])
        for role in ('片库与下载', '兼容副本', '系统盘'): self.assertIn(role, roles)
        self.assertEqual(sum(len(d['paths']) for d in s['storage']), 3)

    def test_broken_downloader_link_is_repaired_with_the_hotel_qb_password(self):
        self.native.state['testall_valid'] = False
        issue = next(i for i in self.status()['issues'] if i['action'] == 'repair_downloaders')
        self.assertEqual(issue['level'], 'error')
        self.native.state['testall_valid'] = True
        result = self.store.media_center_action({'action': 'repair_downloaders'}, 'admin')
        self.assertIn('Radarr、Sonarr', result['message'])
        writes = [c for c in self.native.calls if c[1] in ('PUT', 'POST') and '/downloadclient' in c[2] and '/test' not in c[2]]
        self.assertEqual([(c[0], c[1], c[2]) for c in writes], [('radarr', 'PUT', '/api/v3/downloadclient/3'), ('sonarr', 'POST', '/api/v3/downloadclient')])
        for c in writes:
            fields = {f['name']: f.get('value') for f in c[3]['fields']}
            self.assertEqual(fields['password'], 'current-qb-test-password'); self.assertEqual(fields['host'], 'hotel-qbittorrent')
        self.assertEqual(writes[1][3]['name'], 'Hotel qB')
        self.assertFalse(any(i['action'] == 'repair_downloaders' for i in result['status']['issues']))

    def test_repair_refuses_stale_saved_password_before_writing_or_claiming_success(self):
        self.native.state['client_test_valid'] = False
        with self.assertRaises(Problem) as error:
            self.store.media_center_action({'action': 'repair_downloaders'}, 'admin')
        self.assertIn('密码管理', str(error.exception))
        self.assertFalse(any(c[1] in ('PUT', 'POST') and '/downloadclient' in c[2] and '/test' not in c[2] for c in self.native.calls))

    def test_no_usable_indexer_is_an_error_and_retest_tolerates_failed_tests(self):
        self.native.state['indexerstatus'] = [{'indexerId': 1, 'disabledTill': '2099-01-01'}, {'indexerId': 2, 'disabledTill': '2099-01-01'}]
        issue = next(i for i in self.status()['issues'] if i['action'] == 'retest_indexers')
        self.assertEqual(issue['title'], '没有可用的资源搜索源')
        self.native.state['testall_status'] = 400
        self.assertIn('重新测试', self.store.media_center_action({'action': 'retest_indexers'}, 'admin')['message'])
        self.assertTrue(any(c[2] == '/api/v1/command' and c[3] == {'name': 'ApplicationIndexerSync'} for c in self.native.calls))

    def test_hevc_policy_is_applied_to_both_apps_read_back_and_remembered(self):
        result = self.store.media_center_action({'action': 'set_policy', 'hevc': 'prefer_h264'}, 'admin')
        self.assertEqual(result['status']['policy']['hevc'], 'prefer_h264')
        for app, fmt in (('radarr', 9), ('sonarr', 7)):
            p = self.native.state['profiles'][app]
            self.assertEqual(p['minFormatScore'], -10000); self.assertEqual(next(x['score'] for x in p['formatItems'] if x['format'] == fmt), -100)
        with self.assertRaises(Problem): self.store.media_center_action({'action': 'set_policy', 'hevc': 'anything'}, 'admin')
        self.native.state['readback_broken'] = True
        with self.assertRaises(Problem) as error: self.store.media_center_action({'action': 'set_policy', 'hevc': 'h264_only'}, 'admin')
        self.assertIn('读回不符', str(error.exception))
        with self.store.db() as db: self.assertEqual(self.store.kv(db, 'media_policy'), {'hevc': 'prefer_h264'})

    def test_subtitle_accounts_enable_providers_without_logging_secrets(self):
        result = self.store.media_center_action({'action': 'set_subtitles', 'assrt_token': 'assrt-secret-token'}, 'admin')
        form = self.native.state['bazarr_form']
        self.assertEqual(form['settings-assrt-token'], ['assrt-secret-token'])
        self.assertEqual(form['settings-general-enabled_providers'], ['embeddedsubtitles', 'assrt'])
        self.assertTrue(result['status']['subtitles']['assrt'])
        with self.store.db() as db: audit = json.dumps([dict(r) for r in db.execute('SELECT * FROM audit')], ensure_ascii=False)
        self.assertNotIn('assrt-secret-token', audit)
        with self.assertRaises(Problem): self.store.media_center_action({'action': 'set_subtitles', 'opensubtitles_username': 'u'}, 'admin')
        with self.assertRaises(Problem) as error:   # a new password alone must not silently switch the provider off
            self.store.media_center_action({'action': 'set_subtitles', 'opensubtitles_username': '', 'opensubtitles_password': 'p'}, 'admin')
        self.assertIn('用户名', str(error.exception))
        self.store.media_center_action({'action': 'set_subtitles', 'assrt_token': ''}, 'admin')
        self.assertEqual(self.native.state['bazarr_form']['settings-general-enabled_providers'], ['embeddedsubtitles'])

    def test_chinese_policy_preserves_all_existing_profiles_languages_and_credentials(self):
        old = {'profileId': 7, 'name': '人工英文规则', 'cutoff': 3, 'items': [{'id': 3, 'language': 'en'}],
               'mustContain': ['release'], 'mustNotContain': [], 'originalFormat': True, 'tag': 'keep'}
        self.native.state['subtitle_profiles'] = [old.copy()]
        self.native.state['bazarr']['general']['enabled_providers'] += ['assrt']
        self.native.state['bazarr']['assrt']['token'] = 'keep-existing-private-token'
        self.store.media_center_action({'action': 'subtitle_policy'}, 'admin')
        profiles = self.native.state['subtitle_profiles']
        self.assertEqual(profiles[0], old)
        self.assertEqual(profiles[1]['profileId'], 8)
        self.assertEqual([x['language'] for x in profiles[1]['items']], ['zh', 'zt'])
        self.assertEqual(self.native.state['bazarr']['assrt']['token'], 'keep-existing-private-token')
        self.assertEqual(self.native.state['bazarr']['general']['enabled_providers'], ['embeddedsubtitles', 'assrt'])
        self.assertEqual(self.native.state['bazarr_form']['languages-enabled'], ['en', 'zh', 'zt'])
        self.assertTrue(self.status()['subtitles']['auto_sync'])
        self.assertEqual(self.status()['subtitles']['auto_sync_scope'], 'all_downloads')
        self.assertFalse(self.native.state['bazarr']['subsync']['use_subsync_threshold'])
        self.assertFalse(self.native.state['bazarr']['subsync']['use_subsync_movie_threshold'])
        self.store.media_center_action({'action': 'subtitle_policy'}, 'admin')
        self.assertEqual(len(self.native.state['subtitle_profiles']), 2, 'repeated setup must reuse its own profile')

    def test_high_match_score_must_not_silently_exclude_audio_synchronization(self):
        self.native.state['bazarr']['subsync'] = {'use_subsync': True, 'use_subsync_threshold': True,
                                                 'use_subsync_movie_threshold': True, 'subsync_movie_threshold': 70}
        self.assertEqual(self.status()['subtitles']['auto_sync_scope'], 'score_limited')
        self.store.media_center_action({'action': 'subtitle_policy'}, 'admin')
        self.assertEqual(self.status()['subtitles']['auto_sync_scope'], 'all_downloads')
        self.assertEqual(self.native.state['bazarr']['subsync']['subsync_movie_threshold'], 70,
                         'Disabling the native gate must not rewrite other saved thresholds')

    def test_policy_does_not_claim_all_downloads_if_native_app_ignores_gate_change(self):
        self.native.state['bazarr']['subsync'] = {'use_subsync': True, 'use_subsync_threshold': True,
                                                 'use_subsync_movie_threshold': True}
        self.native.state['subtitle_sync_gates_ignored'] = True
        with self.assertRaises(Problem) as error: self.store.media_center_action({'action': 'subtitle_policy'}, 'admin')
        self.assertIn('读回', str(error.exception))
        self.assertEqual(self.status()['subtitles']['auto_sync_scope'], 'score_limited')

    def test_subtitle_policy_refuses_incomplete_inventory_and_does_not_claim_failed_readback(self):
        self.native.state['subtitle_profiles'] = None
        with self.assertRaises(Problem): self.store.media_center_action({'action': 'subtitle_policy'}, 'admin')
        self.assertFalse(any(c[1] == 'POST' and c[2] == '/api/system/settings' for c in self.native.calls))
        self.native.state['subtitle_profiles'] = []
        self.native.state['subtitle_readback_broken'] = True
        with self.assertRaises(Problem) as error: self.store.media_center_action({'action': 'subtitle_policy'}, 'admin')
        self.assertIn('读回', str(error.exception))

    def test_search_uses_native_scheduler_and_requires_an_online_source(self):
        with self.assertRaises(Problem): self.store.media_center_action({'action': 'search_subtitles'}, 'admin')
        self.assertFalse(any(c[1] == 'POST' and c[2] == '/api/system/tasks' for c in self.native.calls))
        self.store.media_center_action({'action': 'set_subtitles', 'assrt_token': 'test-private'}, 'admin')
        result = self.store.media_center_action({'action': 'search_subtitles'}, 'admin')
        self.assertIn('后台搜索', result['message']); self.assertNotIn('下载成功', result['message'])
        tasks = [c[3]['taskid'][0] for c in self.native.calls if c[1] == 'POST' and c[2] == '/api/system/tasks']
        self.assertEqual(tasks, ['wanted_search_missing_subtitles_movies', 'wanted_search_missing_subtitles_series'])
        self.native.state['subtitle_tasks'] = []
        with self.assertRaises(Problem): self.store.media_center_action({'action': 'search_subtitles'}, 'admin')

    def test_provider_throttle_is_reported_without_exposing_upstream_error_or_credentials(self):
        self.native.state['bazarr']['general']['enabled_providers'] += ['assrt']
        self.native.state['bazarr']['assrt']['token'] = 'never-show-this-token'
        self.native.state['subtitle_providers'] = [{'name': 'assrt', 'status': 'private-token-never-show-this-token', 'retry': 'later'}]
        result = self.status()
        self.assertEqual(result['subtitles']['provider_failures'], ['assrt'])
        self.assertNotIn('never-show-this-token', json.dumps(result))
        self.assertTrue(any(i['title'] == '部分字幕来源暂时不可用' for i in result['issues']))

    def test_http_view_hides_keys_and_actions_are_admin_only(self):
        status, _, body = self.request('/api/admin/media-automation', auth=True)
        self.assertEqual(status, 200)
        raw = json.dumps(body)
        for key in list(KEYS.values()) + ['current-qb-test-password']: self.assertNotIn(key, raw)
        status, _, _ = self.request('/api/admin/media-automation', {'action': 'refresh'}, auth=False)
        self.assertIn(status, (401, 403))
        status, _, body = self.request('/api/admin/media-automation', {'action': 'refresh', 'revision': self.state()['revision']}, auth=True)
        self.assertEqual(status, 200); self.assertEqual(body['message'], '已重新检查')

    def test_busy_snapshot_without_cache_is_not_reported_as_healthy(self):
        self.store.__dict__['_media_center_lock'] = threading.Lock(); self.store._media_center_lock.acquire()
        try: s = self.store.media_center_status(refresh=True)
        finally: self.store._media_center_lock.release()
        self.assertTrue(s['refreshing']); self.assertIsNone(s['checked_at'])

    def test_unsafe_or_missing_configuration_and_disabled_automation(self):
        path = self.data / 'media-automation.json'; config = json.loads(path.read_text())
        config['radarr']['url'] = 'http://192.0.2.10:7878'; path.write_text(json.dumps(config))
        s = self.status(); self.assertEqual(s['issues'][0]['title'], '片库自动化配置异常')
        self.assertFalse(any(c[0] == 'radarr' for c in self.native.calls))
        path.unlink(); self.assertEqual(self.status()['issues'][0]['title'], '缺少片库自动化私有配置')
        with self.store.db() as db: self.store.put_kv(db, 'media_automation', {'enabled': False})
        self.assertFalse(self.status()['enabled'])
        with self.assertRaises(Problem) as error: self.store.media_center_action({'action': 'refresh'}, 'admin')
        self.assertEqual(error.exception.status, 409)


if __name__ == '__main__': unittest.main()
