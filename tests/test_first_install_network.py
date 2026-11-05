import argparse
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy'))
import lan_firewall as network
spec = importlib.util.spec_from_file_location('first_install_bootstrap', ROOT / 'deploy/bootstrap.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class FirstInstallNetworkTests(unittest.TestCase):
    def test_all_published_application_entry_ports_have_a_policy(self):
        sys.path.insert(0, str(ROOT / 'server'))
        from remote_access import SERVICES
        expected = {port for _, _, port, _, _, _ in SERVICES if port != 22}
        self.assertTrue(expected <= set(network.FULL_TCP))
        self.assertTrue({139, 445, 18790} <= set(network.FULL_TCP))
        self.assertNotIn(22, network.FULL_TCP)
        self.assertNotIn(6800, network.FULL_TCP)  # private aria2 RPC is not a LAN UI

    def test_policy_handles_dnat_and_ipv6_without_flushing_other_tables(self):
        policy = network.render('full', ['192.168.4.0/24'])
        self.assertIn('hook input', policy)
        self.assertIn('hook forward', policy)
        self.assertIn('ct original proto-dst', policy)
        self.assertIn('meta nfproto ipv6', policy)
        self.assertNotIn('flush ruleset', policy)
        self.assertNotIn('policy drop', policy)
        self.assertNotIn('22,', policy)

    def test_core_profile_does_not_open_nas_or_native_apps(self):
        policy = network.render('core', network.DEFAULT_NETWORKS)
        for port in ['18790', '18791', '9090']:
            self.assertIn(port, policy)
        for port in ['8095', '8096', '9696', '445']:
            self.assertNotIn(port, policy)

    def test_public_ipv6_loopback_and_empty_networks_are_rejected(self):
        for values in [['0.0.0.0/0'], ['8.8.8.0/24'], ['::/0'], ['127.0.0.0/8'], ['169.254.0.0/16'], ['198.51.100.0/24'], []]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                network.networks(values)
        self.assertEqual(network.networks(['192.168.4.20/24', '192.168.4.0/24']), ['192.168.4.0/24'])

    def test_resume_cannot_mix_another_release_or_change_network_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / 'state.json'
            state = {'release': 'another-release', 'profile': 'full', 'lan_cidrs': ['192.168.4.0/24']}
            state_path.write_text(json.dumps(state))
            args = argparse.Namespace(profile='full', resume=True, lan_cidr=None)
            with patch.object(bootstrap, 'STATE', state_path):
                with self.assertRaisesRegex(RuntimeError, '原版本'):
                    bootstrap.setup_state(args)
                state['release'] = (ROOT / 'VERSION').read_text().strip()
                state_path.write_text(json.dumps(state))
                args.lan_cidr = ['10.0.0.0/8']
                with self.assertRaisesRegex(RuntimeError, '不能更改允许网段'):
                    bootstrap.setup_state(args)


if __name__ == '__main__':
    unittest.main()
