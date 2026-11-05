"""MD01: TV home background, contact QR and generated Wi-Fi QR; read-only guide cards; timed topics."""
import http.client
import json
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import test_management as fixture
import test_orders
import qr_image


class HomeE2E(unittest.TestCase):
    setUp = fixture.ManagementE2E.setUp
    tearDown = fixture.ManagementE2E.tearDown
    request = fixture.ManagementE2E.request
    state = fixture.ManagementE2E.state
    mutate = fixture.ManagementE2E.mutate
    op = test_orders.OrdersE2E.op
    login_as = test_orders.OrdersE2E.login_as

    def tv_get(self, path):
        conn = http.client.HTTPConnection('127.0.0.1', self.tv.server_port, timeout=10)
        conn.request('GET', path, headers={'X-TV-Token': 'tv-test-token'}); r = conn.getresponse(); body = r.read(); conn.close()
        return r.status, body

    def catalog(self):
        return json.loads(self.tv_get('/api/v1/catalog')[1])

    def upload(self, name, data):
        h = {'X-Filename': name}
        return self.request('/api/admin/uploads?kind=home', data, auth=True, headers=h)

    def test_home_background_contact_and_wifi_qr(self):
        png = qr_image.qr_png('https://example.com/contact')
        status, _, up = self.upload('contact.png', png)
        self.assertEqual(status, 201); self.assertTrue(up['path'].startswith('/media/_home/'))
        status, _, bg = self.upload('lobby.png', png)
        self.assertEqual(status, 201)
        result = self.op('home', 'home_save', background=bg['path'], contact_qr=up['path'], contact_label='扫码联系前台',
                         wifi_ssid='Hotel Guest', wifi_password='guest-pass-1', wifi_security='WPA', wifi_label='扫码连 Wi-Fi')
        self.assertEqual(result[0], 200)
        home = self.catalog()['home']
        self.assertEqual((home['background'], home['contact_qr'], home['wifi_ssid'], home['wifi_password']), (bg['path'], up['path'], 'Hotel Guest', ''))
        status, body = self.tv_get(home['wifi_qr'])
        self.assertEqual(status, 200); self.assertTrue(body.startswith(b'\x89PNG'))
        self.assertEqual(self.op('home', 'home_save', wifi_ssid='Hotel Guest', wifi_password='guest-pass-1', wifi_show_password=True)[0], 200)
        self.assertEqual(self.catalog()['home']['wifi_password'], 'guest-pass-1')
        for bad in ({'wifi_ssid': 'A', 'wifi_password': 'short'}, {'wifi_security': 'XYZ'}, {'background': '/media/_home/missing.png'}):
            self.assertIn(self.op('home', 'home_save', **bad)[0], (400, 404), bad)
        self.assertEqual(self.request('/api/admin/uploads?kind=home', b'not an image', auth=True, headers={'X-Filename': 'x.exe'})[0], 400)
        cleared = self.op('home', 'home_save', wifi_ssid='')[2]['home']
        self.assertEqual((cleared['wifi_qr'], cleared['wifi_password']), ('', ''))

    def test_guide_cards_are_ordered_and_read_only_content(self):
        self.assertEqual(self.op('guide', 'save', item={'id': 'g-breakfast', 'name': '早餐', 'body': '06:30–10:00 二楼餐厅', 'sort': 20})[0], 200)
        self.assertEqual(self.op('guide', 'save', item={'id': 'g-wifi', 'name': 'Wi-Fi', 'body': '扫码即可连接', 'sort': 10})[0], 200)
        self.assertEqual(self.op('guide', 'save', item={'id': 'g-off', 'name': '停用卡片', 'body': '不显示', 'enabled': False})[0], 200)
        self.assertEqual([g['title'] for g in self.catalog()['home']['guide']], ['Wi-Fi', '早餐'])
        self.assertEqual(self.op('guide', 'archive', ids=['g-wifi'])[2]['ok'], ['g-wifi'])
        self.assertEqual([g['id'] for g in self.catalog()['home']['guide']], ['g-breakfast'])
        self.assertEqual(self.op('guide', 'save', item={'id': 'g-x', 'name': '空内容', 'body': ''})[0], 400)
        operator, viewer = self.login_as('opg', 'operator'), self.login_as('vwg', 'viewer')
        self.assertEqual(self.op('guide', 'save', headers=operator, item={'id': 'g-op', 'name': '退房', 'body': '中午 12 点前'})[0], 200)
        self.assertEqual(self.op('guide', 'save', headers=viewer, item={'id': 'g-v', 'name': '不行', 'body': '只读'})[0], 403)

    def test_topics_follow_schedule_and_only_list_playable_items(self):
        with self.store.db() as db: db.execute("UPDATE media SET published=1,available=1 WHERE id='existing'")
        zone = ZoneInfo('Asia/Singapore'); now = datetime.now(zone)
        fmt = lambda d: d.strftime('%Y-%m-%dT%H:%M')
        self.assertEqual(self.op('topic', 'save', item={'id': 't-now', 'name': '本周推荐', 'item_ids': ['existing'], 'start': fmt(now - timedelta(hours=1)), 'end': fmt(now + timedelta(days=1))})[0], 200)
        self.assertEqual(self.op('topic', 'save', item={'id': 't-later', 'name': '下周专题', 'item_ids': ['existing'], 'start': fmt(now + timedelta(days=2))})[0], 200)
        self.assertEqual(self.op('topic', 'save', item={'id': 't-ended', 'name': '已结束', 'item_ids': ['existing'], 'end': fmt(now - timedelta(minutes=1))})[0], 200)
        topics = self.catalog()['home']['topics']
        self.assertEqual([(t['id'], t['item_ids']) for t in topics], [('t-now', ['existing'])])
        self.assertEqual(self.op('topic', 'save', item={'id': 't-bad', 'name': '错误时间', 'item_ids': [], 'start': fmt(now), 'end': fmt(now - timedelta(hours=1))})[0], 400)
        self.assertEqual(self.op('topic', 'save', item={'id': 't-missing', 'name': '缺片', 'item_ids': ['nope']})[0], 400)
        self.assertEqual(self.op('topic', 'save', item={'id': 't-dup', 'name': '本周推荐', 'item_ids': []})[0], 409)
        with self.store.db() as db: db.execute("UPDATE media SET published=0 WHERE id='existing'")
        self.assertEqual(self.catalog()['home']['topics'], [])
        admin_view = self.request('/api/admin/operations?section=home', auth=True)[2]
        self.assertEqual({t['id']: t['live'] for t in admin_view['topics']}, {'t-now': True, 't-later': False, 't-ended': False})


if __name__ == '__main__':
    unittest.main()
