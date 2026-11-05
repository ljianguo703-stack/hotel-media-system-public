#!/usr/bin/env python3
"""Exercise the installer against the actual Python backend in a temporary loopback fixture."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from management import Store, password_hash
from server import make_server


def main():
    with tempfile.TemporaryDirectory(prefix='hotel-installer-integration-') as folder:
        root = Path(folder)
        data, media, config = root / 'data', root / 'media', root / 'devices.json'
        data.mkdir(); media.mkdir(); config.write_text('{"devices":{}}')
        (data / 'catalog.json').write_text(json.dumps({'hotel': 'Isolated integration', 'items': []}))
        (data / 'ads.json').write_text('{"ads":[]}')
        store = Store(data, media, config, [media])
        with store.db() as db:
            db.execute('UPDATE users SET password=? WHERE name=?', (password_hash('test-only-integration'), 'admin'))
        servers = []
        try:
            servers.append(make_server('127.0.0.1', 28790, config, data, media, store=store))
            threading.Thread(target=servers[0].serve_forever, daemon=True).start()
            # Match server.main's publication of its actual listening port.
            store.tv_port = servers[0].server_port
            servers.append(make_server('127.0.0.1', 28791, config, data, media, store=store, admin_web=ROOT / 'admin'))
            threading.Thread(target=servers[1].serve_forever, daemon=True).start()
            return subprocess.call(['go', 'test', '-race', '-run',
                                    'TestBackendAgainstRealServer|TestInstallRegistersWithTheRealServer', '-v'],
                                   cwd=ROOT / 'tv-installer', env={**os.environ, 'TI_REAL_BACKEND': '127.0.0.1:28791',
                                   'TI_REAL_USER': 'admin', 'TI_REAL_PASS': 'test-only-integration'})
        finally:
            for server in servers:
                server.shutdown(); server.server_close()


if __name__ == '__main__':
    sys.exit(main())
