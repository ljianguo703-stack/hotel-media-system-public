"""Exercise the real curl transport with a deliberately truncated local response."""
import hashlib
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('nas_portable_download', ROOT / 'deploy/nas/portable.py')
nas = importlib.util.module_from_spec(spec); spec.loader.exec_module(nas)


class InstallerDownloadTests(unittest.TestCase):
    def test_truncated_transfer_retries_and_only_verified_bytes_replace_cache(self):
        body = b'fixed installer resource'
        attempts = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                attempts.append(self.path)
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body[:3] if len(attempts) == 1 else body)
                self.close_connection = True

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.05), daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / 'asset.zip'; target.write_bytes(b'previous cache')
                url = 'http://127.0.0.1:%d/asset' % server.server_port
                expected = hashlib.sha256(body).hexdigest()
                nas.download(url, target, expected)
                self.assertEqual(len(attempts), 2)
                self.assertEqual(target.read_bytes(), body)
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                nas.download(url, target, expected)
                self.assertEqual(len(attempts), 2)
                with self.assertRaises(RuntimeError): nas.download(url, target, '0' * 64)
                self.assertEqual(target.read_bytes(), body)
                self.assertEqual(list(Path(tmp).iterdir()), [target])
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == '__main__': unittest.main()
