"""Storage compose changes preserve engine identity/state and refuse conflicting binds."""
import unittest
import download_locations_deploy as deploy


class DownloadStorageDeploy(unittest.TestCase):
    def test_engine_mappings_preserve_legacy_state_and_are_idempotent(self):
        for kind in ('qbittorrent', 'aria2'):
            service = {'image': 'pinned@sha256:abc', 'ports': [{'target': 6800, 'published': '6800', 'host_ip': '127.0.0.1'}],
                       'environment': {'PUID': '1000', 'PGID': '1000', 'KEEP': 'untouched'}, 'user': '1000:1000',
                       'volumes': [{'type': 'bind', 'source': '/old', 'target': '/downloads'}], 'deploy': {'resources': {'limits': {'memory': '512m'}}}}
            config = {'name': 'hotel-' + kind, 'services': {kind: service}}
            result = deploy.mapped_config(config, kind, 984)
            current = result['services'][kind]
            self.assertEqual(current['volumes'][0], service['volumes'][0])
            for field in ('image', 'ports', 'deploy'): self.assertEqual(current[field], service[field])
            self.assertEqual(len(current['volumes']), 6)
            self.assertTrue(all(v['bind']['propagation'] == 'rslave' for v in current['volumes'][1:]))
            self.assertEqual(current['environment']['PUID'], '1000')
            self.assertEqual(current['environment']['KEEP'], 'untouched')
            self.assertEqual(current['environment']['PGID'] if kind == 'qbittorrent' else current['user'], '984' if kind == 'qbittorrent' else '1000:984')
            self.assertEqual(deploy.mapped_config(result, kind, 984), result)
            self.assertEqual(len(service['volumes']), 1)

    def test_conflicting_container_path_is_rejected(self):
        config = {'services': {'aria2': {'user': '1000:1000', 'volumes': [{'target': '/hotel-storage/mnt', 'source': '/unrelated'}]}}}
        with self.assertRaisesRegex(ValueError, 'Conflicting'): deploy.mapped_config(config, 'aria2', 984)


if __name__ == '__main__': unittest.main()
