"""Validate interruption recovery and corruption rejection for large Release assets."""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
helper = types.ModuleType('dependency_download_test')
source = (ROOT / 'install-from-release.py').read_text().replace('__HOTEL_DEPENDENCY_MANIFEST__', '{}')
exec(compile(source, str(ROOT / 'install-from-release.py'), 'exec'), helper.__dict__)


class Response(io.BytesIO):
    status = 200
    headers = {}


class DependencyDownloadTests(unittest.TestCase):
    def test_embedded_manifest_uses_numeric_rest_asset_ids(self):
        manifest = helper.DEPENDENCIES
        assets = manifest['images'] + manifest['native_files'] + [manifest['docker_key'], manifest['installer']]
        for asset in assets:
            with self.subTest(file=asset['file']):
                self.assertIs(type(asset['asset_id']), int)
                self.assertGreater(asset['asset_id'], 0)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cache = Path(temporary.name)
        self.data = b'first-chunk-last-chunk'
        self.asset = {'file': 'image.tar', 'asset_id': 1, 'bytes': len(self.data),
                      'sha256': hashlib.sha256(self.data).hexdigest()}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(redirect_stderr(io.StringIO()))
        self.stack.enter_context(patch.object(helper, 'CACHE', self.cache))
        self.stack.enter_context(patch.object(helper.time, 'sleep'))

    def test_verified_cache_avoids_the_network(self):
        target = self.cache / self.asset['file']
        target.write_bytes(self.data)
        with patch.object(helper, 'urlopen') as network:
            self.assertEqual(helper.download(self.asset), target)
            network.assert_not_called()

    def test_partial_download_uses_range_and_is_verified_before_publication(self):
        partial = self.cache / 'image.tar.partial'
        partial.write_bytes(self.data[:7])
        response = Response(self.data[7:])
        response.status = 206
        response.headers = {'Content-Range': f'bytes 7-{len(self.data)-1}/{len(self.data)}'}
        with patch.object(helper, 'urlopen', return_value=response) as network:
            target = helper.download(self.asset)
            self.assertEqual(network.call_args.args[0].get_header('Range'), 'bytes=7-')
        self.assertEqual(target.read_bytes(), self.data)
        self.assertFalse(partial.exists())

    def test_server_ignoring_range_restarts_instead_of_appending(self):
        (self.cache / 'image.tar.partial').write_bytes(b'bad-partial')
        with patch.object(helper, 'urlopen', return_value=Response(self.data)):
            self.assertEqual(helper.download(self.asset).read_bytes(), self.data)

    def test_corruption_preserves_the_old_target_and_removes_the_bad_partial(self):
        target = self.cache / 'image.tar'
        target.write_bytes(b'previous-file')
        with patch.object(helper, 'urlopen', return_value=Response(b'x' * len(self.data))):
            with self.assertRaisesRegex(RuntimeError, 'SHA256'):
                helper.download(self.asset)
        self.assertEqual(target.read_bytes(), b'previous-file')
        self.assertFalse((self.cache / 'image.tar.partial').exists())

    def test_exhausted_disconnects_keep_the_verified_prefix_for_a_later_run(self):
        partial = self.cache / 'image.tar.partial'
        partial.write_bytes(self.data[:7])
        with patch.object(helper, 'urlopen', side_effect=ConnectionResetError()):
            with self.assertRaisesRegex(RuntimeError, '重跑同一命令'):
                helper.download(self.asset)
        self.assertEqual(partial.read_bytes(), self.data[:7])
        self.assertFalse((self.cache / 'image.tar').exists())

    def isolate_entrypoint(self, arguments):
        self.stack.enter_context(patch.dict(helper.os.environ, {}, clear=False))
        state = self.cache / 'state.json'
        state.write_text(json.dumps({'release': helper.DEPENDENCIES['backend_release'], 'profile': 'full'}))
        os_release = self.cache / 'os-release'
        os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
        actual_path = Path
        self.stack.enter_context(patch.object(helper, 'Path', side_effect=lambda value: os_release if value == '/etc/os-release' else actual_path(value)))
        self.stack.enter_context(patch.object(helper, 'STATE', state))
        self.stack.enter_context(patch.object(helper.os, 'geteuid', return_value=0))
        self.stack.enter_context(patch.object(helper.platform, 'machine', return_value='x86_64'))
        self.stack.enter_context(patch.object(helper.sys, 'argv', ['install-from-release.py', *arguments]))
        self.stack.enter_context(patch.object(helper, 'prepare_docker'))
        images = {image['reference']: image for image in helper.DEPENDENCIES['images']}
        self.stack.enter_context(patch.object(helper, 'docker_identity', side_effect=lambda reference: {'Id': images[reference]['image_id']}))
        self.stack.enter_context(patch.object(helper, 'download', side_effect=lambda asset: self.cache / asset['file']))
        return self.stack.enter_context(patch.object(helper, 'run'))

    def test_normal_entrypoint_preserves_arguments_and_sets_verified_aria2_identity(self):
        run = self.isolate_entrypoint(['--hotel', '新店 A'])
        helper.main()
        self.assertEqual(run.call_args.args[0], ['bash', self.cache / helper.DEPENDENCIES['installer']['file'],
                                               '--profile', 'full', '--resume', '--hotel', '新店 A'])
        self.assertEqual(run.call_args.kwargs['env']['HOTEL_PREBUILT_ARIA2_ID'], helper.DEPENDENCIES['aria2_image_id'])

    def test_cache_only_entrypoint_does_not_execute_the_installer(self):
        run = self.isolate_entrypoint(['--cache-only'])
        helper.main()
        self.assertTrue(run.call_args_list)
        self.assertTrue(all(call.args[0][0] == 'install' for call in run.call_args_list))

    def test_conflicting_install_state_stops_before_docker_or_downloads(self):
        self.isolate_entrypoint([])
        helper.STATE.write_text(json.dumps({'release': 'different-release', 'profile': 'full'}))
        before = helper.STATE.read_bytes()
        with self.assertRaisesRegex(RuntimeError, '不一致'):
            helper.main()
        helper.prepare_docker.assert_not_called()
        helper.download.assert_not_called()
        self.assertEqual(helper.STATE.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
