import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class OnlineInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name in ('bash', 'python3', 'sha256sum', 'shasum', 'mkdir', 'mktemp', 'rm', 'mv'):
            executable = shutil.which(name)
            if executable:
                (self.bin / name).symlink_to(executable)
        self.fixture = self.root / 'fixture.run'
        self.fixture.write_text(
            '#!/usr/bin/env bash\n'
            ': > "$HOTEL_TEST_ARGUMENTS"\n'
            'for argument in "$@"; do\n'
            '  printf "%s\\n" "$argument" >> "$HOTEL_TEST_ARGUMENTS"\n'
            'done\n'
            'exit "${HOTEL_TEST_INSTALL_EXIT:-0}"\n'
        )
        expected = hashlib.sha256(self.fixture.read_bytes()).hexdigest()
        source = (ROOT / 'install-online.sh').read_text()
        source = re.sub(
            r"(readonly HOTEL_PACKAGE_SHA256=)'[a-f0-9]+'",
            lambda match: match[1] + repr(expected), source,
        )
        self.script = self.root / 'install-online.sh'
        self.script.write_text(source)
        downloader = '''#!/usr/bin/env python3
import os
from pathlib import Path
import sys
arguments = sys.argv[1:]
Path(os.environ['HOTEL_TEST_DOWNLOAD_LOG']).write_text('called')
option = '--output-document' if '--output-document' in arguments else '--output'
output = Path(arguments[arguments.index(option) + 1])
mode = os.environ.get('HOTEL_TEST_DOWNLOAD_MODE', 'valid')
output.write_bytes(Path(os.environ['HOTEL_TEST_FIXTURE']).read_bytes()
                   if mode == 'valid' else b'partial or corrupt download')
sys.exit(22 if mode == 'network-failure' else 0)
'''
        self.write_program('curl', downloader)
        self.write_program('wget', downloader)
        self.write_program('sudo', '''#!/usr/bin/env bash
printf 'called' > "$HOTEL_TEST_SUDO_LOG"
exec "$@"
''')
        self.environment = dict(os.environ)
        self.environment.update({
            'PATH': str(self.bin),
            'HOTEL_DOWNLOAD_DIR': str(self.root / 'download with spaces'),
            'HOTEL_TEST_FIXTURE': str(self.fixture),
            'HOTEL_TEST_ARGUMENTS': str(self.root / 'arguments.txt'),
            'HOTEL_TEST_DOWNLOAD_LOG': str(self.root / 'download.log'),
            'HOTEL_TEST_SUDO_LOG': str(self.root / 'sudo.log'),
        })

    def write_program(self, name, content):
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)

    def run_installer(self, *arguments):
        return subprocess.run(
            ['bash', str(self.script), *arguments],
            env=self.environment, text=True, capture_output=True,
        )

    def test_verified_download_forwards_quoted_parameters(self):
        arguments = ['--hotel', '本店 影院', '--storage-root', '/mnt/movie disk/data']
        result = self.run_installer(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'arguments.txt').read_text().splitlines(), arguments)

    def test_no_arguments_are_required_or_injected(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'arguments.txt').read_text(), '')

    def test_wget_only_host_can_download_and_run_preview(self):
        (self.bin / 'curl').unlink()
        result = self.run_installer('--plan')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_curl_only_host_can_download_and_run_preview(self):
        (self.bin / 'wget').unlink()
        result = self.run_installer('--plan')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_corrupt_download_stops_before_running_installer(self):
        self.environment['HOTEL_TEST_DOWNLOAD_MODE'] = 'corrupt'
        result = self.run_installer('--profile', 'full')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'arguments.txt').exists())
        self.assertFalse((self.root / 'sudo.log').exists())
        self.assertEqual(list(Path(self.environment['HOTEL_DOWNLOAD_DIR']).iterdir()), [])

    def test_failed_download_can_be_retried(self):
        self.environment['HOTEL_TEST_DOWNLOAD_MODE'] = 'network-failure'
        result = self.run_installer('--plan')
        self.assertEqual(result.returncode, 22)
        self.assertFalse((self.root / 'arguments.txt').exists())
        self.assertEqual(list(Path(self.environment['HOTEL_DOWNLOAD_DIR']).iterdir()), [])
        self.environment['HOTEL_TEST_DOWNLOAD_MODE'] = 'valid'
        result = self.run_installer('--plan')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_verified_cache_supports_resume_without_network(self):
        result = self.run_installer('--plan')
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.root / 'download.log').unlink()
        self.environment['HOTEL_TEST_DOWNLOAD_MODE'] = 'network-failure'
        result = self.run_installer('--resume', '--profile', 'full')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / 'download.log').exists())
        self.assertEqual((self.root / 'arguments.txt').read_text().splitlines(),
                         ['--resume', '--profile', 'full'])

    def test_preview_avoids_privileged_execution_and_preserves_exit_status(self):
        self.environment['HOTEL_TEST_INSTALL_EXIT'] = '37'
        result = self.run_installer('--plan', '--hotel', '本店影院')
        self.assertEqual(result.returncode, 37)
        self.assertFalse((self.root / 'sudo.log').exists())


if __name__ == '__main__':
    unittest.main()
