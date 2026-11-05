"""Exercise installer pull failures at the Docker CLI boundary, without a daemon or registry."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import call, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('installer_images_test', ROOT / 'deploy/docker_images.py')
images = importlib.util.module_from_spec(spec); spec.loader.exec_module(images)
IMAGE = 'registry.example:5000/test/image@sha256:' + 'a' * 64


class ImagePullTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'calls.json'
        executable = self.root / 'docker'
        executable.write_text('#!' + sys.executable + '\n' + '''
import json, os, sys, time
from pathlib import Path
path = Path(os.environ['FAKE_DOCKER_STATE'])
calls = json.loads(path.read_text()) if path.exists() else []
calls.append(sys.argv[1:]); path.write_text(json.dumps(calls))
if sys.argv[1:3] == ['image', 'inspect']:
    cache = os.environ.get('FAKE_DOCKER_CACHE', '')
    print(cache); sys.exit(0 if cache else 1)
if sys.argv[1] == 'pull':
    time.sleep(float(os.environ.get('FAKE_DOCKER_SLEEP', '0')))
    count = sum(c[0] == 'pull' for c in calls)
    if count <= int(os.environ.get('FAKE_DOCKER_FAILURES', '0')):
        print('https://registry.example/blob?token=private-upstream-test-token', file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
sys.exit('unexpected Docker operation')
''')
        executable.chmod(0o755)
        environment = {'PATH': str(self.root), 'FAKE_DOCKER_STATE': str(self.state),
                       'FAKE_DOCKER_FAILURES': '0', 'FAKE_DOCKER_CACHE': '', 'FAKE_DOCKER_SLEEP': '0',
                       'DOCKER_DEFAULT_PLATFORM': 'linux/arm64'}
        env = patch.dict(os.environ, environment); env.start(); self.addCleanup(env.stop)
        native = patch.object(images.platform, 'machine', return_value='x86_64')
        native.start(); self.addCleanup(native.stop)
        self.output = io.StringIO()
        capture = contextlib.redirect_stdout(self.output); capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def calls(self):
        return json.loads(self.state.read_text()) if self.state.exists() else []

    def pulls(self):
        return [c for c in self.calls() if c[0] == 'pull']

    def test_transient_failures_retry_identical_digest_and_native_architecture(self):
        os.environ['FAKE_DOCKER_FAILURES'] = '2'
        # Isolate retry waits from subprocess's own time.sleep polling.
        with patch.object(images, 'time') as clock:
            sleep = clock.sleep
            images.pull_image(IMAGE)
        self.assertEqual(self.pulls(), [['pull', '--platform', 'linux/amd64', IMAGE]] * 3)
        self.assertEqual(sleep.call_args_list, [call(10), call(30)])
        self.assertNotIn('private-upstream-test-token', self.output.getvalue())

    def test_exhausted_retries_stop_and_do_not_leak_registry_diagnostics(self):
        os.environ['FAKE_DOCKER_FAILURES'] = '100'
        # Isolate retry waits from subprocess's own time.sleep polling.
        with patch.object(images, 'time') as clock:
            sleep = clock.sleep
            with self.assertRaisesRegex(RuntimeError, '已用尽 4 次尝试') as caught:
                images.pull_image(IMAGE)
        self.assertEqual(len(self.pulls()), 4)
        self.assertEqual(sleep.call_args_list, [call(10), call(30), call(60)])
        self.assertNotIn('private-upstream-test-token', str(caught.exception) + self.output.getvalue())
        self.assertTrue(all(c[:2] == ['image', 'inspect'] or c[0] == 'pull' for c in self.calls()))

    def test_hanging_cli_is_killed_at_timeout_and_retried_with_a_limit(self):
        os.environ['FAKE_DOCKER_SLEEP'] = '10'
        # Isolate retry waits from subprocess's own time.sleep polling.
        with patch.object(images, 'time') as clock:
            sleep = clock.sleep
            with self.assertRaisesRegex(RuntimeError, '已用尽 2 次尝试'):
                images.pull_image(IMAGE, delays=(0,), timeout=.3)
        self.assertEqual(len(self.pulls()), 2)
        sleep.assert_called_once_with(0)

    def test_matching_digest_cache_is_reused_but_foreign_architecture_is_not(self):
        os.environ['FAKE_DOCKER_CACHE'] = 'linux/amd64'
        images.pull_image(IMAGE)
        self.assertEqual(self.pulls(), [])
        os.environ['FAKE_DOCKER_CACHE'] = 'linux/arm64'
        images.pull_image(IMAGE)
        self.assertEqual(self.pulls(), [['pull', '--platform', 'linux/amd64', IMAGE]])

    def test_missing_docker_stops_before_retrying(self):
        (self.root / 'docker').unlink()
        # Isolate retry waits from subprocess's own time.sleep polling.
        with patch.object(images, 'time') as clock:
            sleep = clock.sleep
            with self.assertRaisesRegex(RuntimeError, '尚未安装 Docker'):
                images.pull_image(IMAGE)
        self.assertEqual(self.calls(), [])
        sleep.assert_not_called()

    def test_mutable_or_invalid_references_never_reach_docker(self):
        for value in ['nginx:latest', '--help', IMAGE + '\n', IMAGE[:-1], IMAGE + ';id', None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                images.pull_image(value)
        self.assertEqual(self.calls(), [])

    def test_installed_native_tools_import_from_adjacent_helper_without_source_checkout(self):
        target = self.root / 'isolated' / 'stacks' / 'native-tool'
        target.mkdir(parents=True)
        images.copy_helper(target)
        images.copy_helper(target)
        self.assertEqual((target / 'docker_images.py').stat().st_mode & 0o777, 0o644)
        for name in ('install.py', 'jackett.py'):
            shutil.copyfile(ROOT / 'deploy/nas/download-tools' / name, target / name)
            result = subprocess.run([sys.executable, str(target / name), '--help'], cwd=target,
                                    env={**os.environ, 'PYTHONPATH': ''}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('usage:', result.stdout)
        self.assertEqual(self.calls(), [])

    def test_helper_copy_does_not_follow_or_replace_foreign_symlink(self):
        original = self.root / 'foreign.py'; original.write_text('preserve')
        target = self.root / 'installed'; target.mkdir()
        (target / 'docker_images.py').symlink_to(original)
        with self.assertRaises(RuntimeError): images.copy_helper(target)
        self.assertEqual(original.read_text(), 'preserve')
        self.assertTrue((target / 'docker_images.py').is_symlink())


if __name__ == '__main__': unittest.main()
