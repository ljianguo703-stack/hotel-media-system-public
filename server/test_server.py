import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import sqlite3
from server import make_server


class ServerE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'media').mkdir()
        (self.root / 'media/clip.mp4').write_bytes(b'0123456789')
        (self.root / 'secret.txt').write_text('private')
        (self.root / 'devices.json').write_text(json.dumps({'devices': {'room-1': {'token': 'test-token', 'room': '101'}}}))
        (self.root / 'catalog.json').write_text(json.dumps({'items': [{'id':'movie-1','title':'测试影片','path':'/media/clip.mp4'}]}))
        (self.root / 'ads.json').write_text(json.dumps({'ads': []}))
        self.server = make_server('127.0.0.1', 0, self.root / 'devices.json', self.root, self.root / 'media')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, path, method='GET', data=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        h = {'X-TV-Token': 'test-token'}
        h.update(headers or {})
        connection.request(method, path, body=json.dumps(data) if data is not None else None, headers=h)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_catalog_auth_and_time(self):
        self.assertEqual(self.request('/api/v1/catalog', headers={'X-TV-Token': 'bad'})[0], 401)
        status, _, raw = self.request('/api/v1/catalog')
        content = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(content['room'], '101')
        self.assertEqual(content['items'][0]['id'], 'movie-1')
        self.assertGreater(content['server_time_ms'], 0)
        self.assertTrue(content['version'])

    def test_catalog_removes_private_fields_without_rewriting_paths(self):
        with sqlite3.connect(self.root/'events.sqlite3') as db:
            item={'id':'movie-1','title':'片名','path':'/media/clip.mp4','poster':'/media/cover.jpg','_pinned':True,'_archived':False,'jellyfin_id':'hidden','extra':{'_secret':'hidden','ok':'kept'}}
            db.execute("UPDATE media SET body=? WHERE id='movie-1'",(json.dumps(item),))
        status,_,raw=self.request('/api/v1/catalog');self.assertEqual(status,200)
        item=json.loads(raw)['items'][0];self.assertEqual(item['path'],'/media/clip.mp4');self.assertEqual(item['poster'],'/media/cover.jpg')
        self.assertEqual(item['extra'],{'ok':'kept'});self.assertNotIn('jellyfin_id',item);self.assertFalse(any(k.startswith('_') for k in item))

    def test_byte_ranges_seek_head_and_invalid_ranges(self):
        status, headers, raw = self.request('/media/clip.mp4', headers={'Range': 'bytes=3-6'})
        self.assertEqual((status, raw), (206, b'3456'))
        self.assertEqual(headers['Content-Range'], 'bytes 3-6/10')
        self.assertEqual(self.request('/media/clip.mp4', headers={'Range': 'bytes=-3'})[2], b'789')
        self.assertEqual(self.request('/media/clip.mp4', headers={'Range': 'bytes=10-'})[0], 416)
        self.assertEqual(self.request('/media/clip.mp4', headers={'Range': 'bytes=2-1'})[0], 416)
        self.assertEqual(self.request('/media/clip.mp4', method='HEAD')[2], b'')

    def test_media_traversal_and_symlink_do_not_escape(self):
        (self.root / 'media/escape.mp4').symlink_to(self.root / 'secret.txt')
        for path in ['/media/../secret.txt', '/media/%2e%2e/secret.txt', '/media/escape.mp4']:
            self.assertEqual(self.request(path)[0], 404)

    def test_event_retries_are_idempotent_and_device_is_trusted(self):
        payload = {'events': [{'id': 'uuid-1', 'type': 'content_completed', 'device': 'forged-room'}]}
        for _ in range(2):
            self.assertEqual(self.request('/api/v1/events', 'POST', payload)[0], 200)
        with sqlite3.connect(self.root / 'events.sqlite3') as conn:
            self.assertEqual(conn.execute('SELECT count(*),device FROM events').fetchone(), (1, 'room-1'))
        self.assertEqual(self.request('/api/v1/events', 'POST', {'events': [{}]})[0], 400)


if __name__ == '__main__':
    unittest.main()
