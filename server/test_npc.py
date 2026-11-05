import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from npc_protocol import NpcError, validate_config
import test_management as fixtures

spec = importlib.util.spec_from_file_location('npc_controller_test', Path(__file__).resolve().parents[1]/'deploy/nas/npc/control.py')
control = importlib.util.module_from_spec(spec); spec.loader.exec_module(control)


class FakeDocker:
    def __init__(self): self.running = True; self.fail = False; self.calls = []
    def __call__(self, args):
        self.calls.append(args)
        if 'inspect' in args:
            return json.dumps([{'Config': {'Image': control.IMAGE}, 'State': {'Running': self.running}}])
        if self.fail:
            self.fail = False; raise NpcError('private docker error not forwarded', 503)
        if 'stop' in args or 'create' in args: self.running = False
        if 'up' in args: self.running = True
        return ''


class NpcControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = Path(self.tmp.name)
        self.stack = root/'stack'; self.data = root/'data'; self.stack.mkdir(); self.data.mkdir()
        (self.stack/'.env').write_text('NPS_SERVER=\nNPS_VKEY=\nNPS_TYPE=tcp\n')
        (self.stack/'connection.env').write_text('NPC_SERVER_ADDR=\nNPC_SERVER_VKEY=\n')
        self.docker = FakeDocker(); self.manager = control.Controller(self.stack, self.data, self.docker)
        self.config = {'enabled': True, 'server': 'nps.example.com:8024', 'vkey': "test$literal#'quoted", 'type': 'tls', 'dns': '1.1.1.1', 'extra': '-keepalive=30'}

    def tearDown(self): self.tmp.cleanup()

    def save(self, config=None, revision=1): return self.manager.handle({'action': 'save', 'revision': revision, 'config': config or self.config})

    def test_empty_legacy_state_is_waiting_and_has_no_secret(self):
        result = self.manager.handle({'action': 'status'})
        self.assertEqual(result['runtime']['state'], 'waiting_config'); self.assertNotIn('vkey', result['config'])

    def test_save_retains_literal_key_and_blank_preserves_without_returning_it(self):
        first = self.save(); self.assertTrue(first['config']['key_set']); self.assertNotIn(self.config['vkey'], json.dumps(first))
        self.assertIn('NPC_SERVER_VKEY='+self.config['vkey']+'\n', (self.stack/'connection.env').read_text())
        self.assertEqual((self.data/'control.json').stat().st_mode & 0o777, 0o600)
        second = self.save({**self.config, 'vkey': '', 'dns': '8.8.8.8'}, 2)
        self.assertEqual(second['config']['revision'], 3)
        self.assertEqual(json.loads((self.data/'control.json').read_text())['vkey'], self.config['vkey'])

    def test_stale_revision_does_not_touch_running_container_or_config(self):
        self.save(); calls = len(self.docker.calls)
        with self.assertRaises(NpcError) as error: self.save(revision=1)
        self.assertEqual(error.exception.status, 409); self.assertEqual(len(self.docker.calls), calls)

    def test_stop_and_start_are_persistent_and_revisioned(self):
        self.save(); stopped = self.manager.handle({'action': 'stop', 'revision': 2})
        self.assertFalse(stopped['config']['enabled']); self.assertEqual(stopped['runtime']['state'], 'stopped')
        running = self.manager.handle({'action': 'start', 'revision': 3})
        self.assertTrue(running['config']['enabled']); self.assertEqual(running['runtime']['state'], 'running')
        self.assertEqual(running['runtime']['connection'], 'unconfirmed')

    def test_apply_failure_restores_previous_private_config_and_environment(self):
        self.save(); old = (self.stack/'connection.env').read_bytes(); self.docker.fail = True
        with self.assertRaises(NpcError) as error: self.save({**self.config, 'server': 'another.example.com:9999'}, 2)
        self.assertIn('已恢复原配置', str(error.exception))
        self.assertEqual((self.stack/'connection.env').read_bytes(), old)
        self.assertEqual(self.manager.load()['revision'], 2); self.assertTrue(self.docker.running)

    def test_parameters_cannot_override_logging_version_config_or_execute_shell(self):
        for extra in ['-debug=true', '-log_level=trace', '-config=/tmp/x', '-version', '-keepalive=30;touch /tmp/x', '-keepalive=$(id)', '-keepalive=9999']:
            with self.subTest(extra=extra), self.assertRaises(NpcError): validate_config({**self.config, 'extra': extra}, {})
        self.assertEqual(validate_config({**self.config, 'extra': '--disable_p2p=true -disconnect_timeout=60'}, {})['extra'], '-disable_p2p=true -disconnect_timeout=60')

    def test_address_dns_and_key_validation_before_docker(self):
        for changes in [{'server': 'http://x:8024'}, {'server': 'x:0'}, {'dns': 'not-an-ip'}, {'vkey': 'a\nNPC_EXTRA_ARGS=-debug=true'}, {'type': 'shell'}]:
            with self.subTest(changes=changes), self.assertRaises(NpcError): self.save({**self.config, **changes})
        self.assertFalse(self.docker.calls)


class NpcHttpTests(unittest.TestCase):
    setUp = fixtures.ManagementE2E.setUp
    tearDown = fixtures.ManagementE2E.tearDown
    request = fixtures.ManagementE2E.request
    state = fixtures.ManagementE2E.state

    def test_admin_only_get_and_mutations_require_csrf_and_origin(self):
        with patch('npc_client.request', return_value={'config': {'revision': 1, 'key_set': False}, 'runtime': {'state': 'waiting_config'}}) as rpc:
            self.assertEqual(self.request('/api/admin/npc')[0], 401)
            self.assertEqual(self.request('/api/admin/npc', auth=True)[0], 200)
            self.assertEqual(self.request('/api/admin/npc', {'action': 'stop', 'revision': 1}, headers={'Cookie': self.cookie})[0], 403)
            self.assertEqual(self.request('/api/admin/npc', {'action': 'stop', 'revision': 1}, auth=True, headers={'Origin': 'https://evil.example'})[0], 403)
            with self.store.db() as db: db.execute("UPDATE users SET role='operator' WHERE name='admin'")
            self.assertEqual(self.request('/api/admin/npc', auth=True)[0], 403)
            self.assertEqual(self.request('/api/admin/npc', {'action': 'stop', 'revision': 1}, auth=True)[0], 403)
            self.assertEqual(rpc.call_count, 1)

    def test_successful_write_audit_and_response_never_contain_key(self):
        key = 'http-test-secret-never-audit'
        with patch('npc_client.request', return_value={'config': {'revision': 2, 'key_set': True}, 'runtime': {'state': 'running'}}):
            code, _, response = self.request('/api/admin/npc', {'action': 'save', 'revision': 1, 'config': {'vkey': key}}, auth=True)
        self.assertEqual(code, 200); self.assertNotIn(key, json.dumps(response)); self.assertNotIn(key, json.dumps(self.state()))
