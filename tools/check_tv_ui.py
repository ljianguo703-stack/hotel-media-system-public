#!/usr/bin/env python3
"""Start a fresh in-memory ADB fixture, run the browser acceptance, then stop the fixture."""
import os
from pathlib import Path
import signal
import socket
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    evidence = ROOT / 'local/evidence/IN02'
    evidence.mkdir(parents=True, exist_ok=True)
    with (evidence / 'tv-installer-fixture.log').open('w') as log:
        fixture = subprocess.Popen(['go', 'test', '-run', '^TestUIFixture$', '-timeout', '20m', '-v'],
                                   cwd=ROOT / 'tv-installer', env={**os.environ, 'TI_UI_FIXTURE': '1'},
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            for _ in range(300):
                if fixture.poll() is not None:
                    raise RuntimeError('UI fixture exited before ready')
                if 'UI fixture only at' not in (evidence / 'tv-installer-fixture.log').read_text():
                    time.sleep(.1)
                    continue
                try:
                    with socket.create_connection(('127.0.0.1', 18947), timeout=.1):
                        break
                except OSError:
                    time.sleep(.1)
            else:
                raise RuntimeError('UI fixture did not start')
            return subprocess.call(['node', 'tv-installer/tools/browser-e2e.cjs'], cwd=ROOT)
        finally:
            try:
                os.killpg(fixture.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            fixture.wait(timeout=10)


if __name__ == '__main__':
    raise SystemExit(main())
