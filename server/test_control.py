"""A04 unified ad control: limits, one-click commercial stop with house exceptions, delivery and TV acknowledgement."""
import time
import unittest
import test_management as fixture
import test_orders


class ControlE2E(unittest.TestCase):
    setUp = fixture.ManagementE2E.setUp
    tearDown = fixture.ManagementE2E.tearDown
    request = fixture.ManagementE2E.request
    state = fixture.ManagementE2E.state
    mutate = fixture.ManagementE2E.mutate
    campaign = fixture.ManagementE2E.campaign
    op = test_orders.OrdersE2E.op
    login_as = test_orders.OrdersE2E.login_as

    def control(self, headers=None, **body):
        return self.op('ad_control', 'ad_control', headers=headers, **body)

    def status(self):
        return self.request('/api/admin/operations?section=ad_control', auth=True)[2]

    def _tv_fetch(self):
        import http.client, json
        conn = http.client.HTTPConnection('127.0.0.1', self.tv.server_port, timeout=10)
        conn.request('GET', '/api/v1/ads', headers={'X-TV-Token': 'tv-test-token'}); r = conn.getresponse(); body = json.loads(r.read()); conn.close()
        return body

    def setup_ads(self):
        self.assertEqual(self.op('advertiser', 'save', item={'id': 'adv-shop', 'name': '测试商户'})[0], 200)
        self.assertEqual(self.op('advertiser', 'save', item={'id': 'adv-house', 'name': '酒店自营', 'house': True})[0], 200)
        self.assertEqual(self.mutate('/campaigns/ad-shop', self.campaign(start='00:00', end='00:00'))[0], 200)
        self.assertEqual(self.mutate('/campaigns/ad-house', self.campaign(name='酒店公告', merchant='酒店自营', start='00:00', end='00:00'))[0], 200)

    def test_stop_keeps_house_ads_and_tracks_delivery_and_ack(self):
        self.setup_ads()
        base = self._tv_fetch()
        self.assertEqual({a['id'].split('~')[0] for a in base['ads']}, {'ad-shop', 'ad-house'})
        self.assertEqual(base['control']['stopped'], False)
        self.assertEqual(self.status()['devices'][0]['state'], '已拉取未回执')
        result = self.control(stopped=True)[2]
        version = result['control']['version']
        self.assertTrue(result['control']['stopped']); self.assertEqual(result['devices'][0]['state'], '待确认')
        stopped = self._tv_fetch()
        self.assertEqual({a['id'].split('~')[0] for a in stopped['ads']}, {'ad-house'})
        self.assertEqual(stopped['control']['version'], version)
        self.assertEqual(self.status()['devices'][0]['state'], '已拉取未回执')
        self.store.record_events({'id': 'room-101', 'room': '101'}, [{'id': 'ack-1', 'type': 'ad_control_applied', 'version': version, 'at_ms': int(time.time() * 1000)}])
        self.assertEqual(self.status()['devices'][0]['state'], '已执行')
        resumed = self.control(stopped=False)[2]
        self.assertEqual(resumed['control']['version'], version + 1); self.assertEqual(resumed['devices'][0]['state'], '待确认')
        self.assertEqual(len(self._tv_fetch()['ads']), 2)
        audit = self.request('/api/admin/operations?section=audit&q=%E5%B9%BF%E5%91%8A', auth=True)[2]
        self.assertTrue(any('一键停止商业广告' in r.get('action', '') for r in audit['rows']))

    def test_limits_validation_and_permissions(self):
        ok = self.control(max_fullscreen_per_hour=4, max_fullscreen_seconds_per_hour=90, min_fullscreen_gap_seconds=300, text_start='22:00', text_end='02:00')
        self.assertEqual(ok[0], 200); c = ok[2]['control']
        self.assertEqual((c['max_fullscreen_per_hour'], c['text_start'], c['text_end']), (4, '22:00', '02:00'))
        manifest = self._tv_fetch()['control']
        self.assertEqual((manifest['min_fullscreen_gap_seconds'], manifest['timezone']), (300, 'Asia/Singapore'))
        for bad in ({'text_start': '22:00', 'text_end': ''}, {'text_start': '25:00', 'text_end': '02:00'}, {'text_start': '08:00', 'text_end': '08:00'}, {'max_fullscreen_per_hour': -1}):
            self.assertEqual(self.control(**bad)[0], 400, bad)
        operator, viewer = self.login_as('opc', 'operator'), self.login_as('vwc', 'viewer')
        self.assertEqual(self.control(headers=operator, max_fullscreen_per_hour=9)[0], 403)
        self.assertEqual(self.control(headers=operator, stopped=True)[0], 200)
        self.assertEqual(self.control(headers=viewer, stopped=False)[0], 403)
        self.assertEqual(self.op('advertiser', 'save', headers=operator, item={'id': 'adv-x', 'name': '想绕过停播', 'house': True})[0], 403)
        self.assertEqual(self.op('advertiser', 'save', headers=operator, item={'id': 'adv-y', 'name': '普通商户'})[0], 200)
        unchanged = self.status()['control']['version']
        self.assertEqual(self.control(stopped=True)[2]['control']['version'], unchanged)

    def test_probe_fetch_does_not_count_as_delivery(self):
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1', self.tv.server_port, timeout=10)
        conn.request('GET', '/api/v1/ads', headers={'X-TV-Token': 'tv-test-token', 'X-TV-Probe': '1'}); conn.getresponse().read(); conn.close()
        self.assertEqual(self.status()['devices'][0]['state'], '待确认')


if __name__ == '__main__':
    unittest.main()
