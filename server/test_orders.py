"""A01 advertising packages and insertion orders: registration, permissions and real-event progress."""
import json
import time
import unittest
from datetime import date, timedelta
import test_management as fixture
from management import Store, password_hash


class OrdersE2E(unittest.TestCase):
    setUp = fixture.ManagementE2E.setUp
    tearDown = fixture.ManagementE2E.tearDown
    request = fixture.ManagementE2E.request
    state = fixture.ManagementE2E.state
    mutate = fixture.ManagementE2E.mutate
    campaign = fixture.ManagementE2E.campaign

    def op(self, kind, action, headers=None, **body):
        payload = dict(kind=kind, action=action, revision=self.state()['revision'], **body)
        return self.request('/api/admin/operations', payload, auth=headers is None, headers=headers)

    def login_as(self, name, role):
        with self.store.db() as db: db.execute('INSERT INTO users VALUES(?,?,?,1)', (name, password_hash(name + '-test-password'), role))
        _, headers, result = self.request('/api/admin/login', {'username': name, 'password': name + '-test-password'})
        return {'Cookie': headers['Set-Cookie'].split(';')[0], 'X-CSRF-Token': result['user']['csrf']}

    def orders(self, **query):
        q = '&'.join('%s=%s' % (k, v) for k, v in {'section': 'orders', **query}.items())
        return self.request('/api/admin/operations?' + q, auth=True)[2]

    def base(self, currency='CNY'):
        if currency: self.assertEqual(self.op('order', 'currency_save', currency=currency)[0], 200)
        self.assertEqual(self.op('advertiser', 'save', item={'id': 'adv-1', 'name': '测试商户'})[0], 200)
        self.assertEqual(self.op('package', 'save', item={'id': 'pkg-3', 'name': '夜间文字月包', 'tier': 3, 'placements': ['all'], 'default_days': 30, 'target': 60, 'list_price': '1200.00' if currency else ''})[0], 200)
        self.assertEqual(self.mutate('/campaigns/ad-night', self.campaign())[0], 200)

    def order(self, **changes):
        today = date.today()
        item = {'id': 'ord-1', 'advertiser_id': 'adv-1', 'package_id': 'pkg-3', 'campaign_ids': ['ad-night'], 'group': '全部客房',
                'start': (today - timedelta(days=3)).isoformat(), 'end': (today + timedelta(days=3)).isoformat(), 'target': 60,
                'quoted_price': '1000.00', 'received_amount': '', 'payment_status': '未收款'}
        item.update(changes); return item

    def test_currency_required_and_locked_once_priced(self):
        self.base(currency='')
        status, _, body = self.op('order', 'save', item=self.order())
        self.assertEqual(status, 400); self.assertIn('币种', body['error'])
        self.assertEqual(self.op('order', 'currency_save', currency='usd')[2]['currency'], 'USD')
        self.assertEqual(self.op('order', 'save', item=self.order())[0], 200)
        self.assertEqual(self.op('order', 'currency_save', currency='EUR')[0], 409)
        self.assertEqual(self.op('order', 'currency_save', currency='US1')[0], 400)
        operator = self.login_as('opa', 'operator')
        self.assertEqual(self.op('order', 'currency_save', headers=operator, currency='CNY')[0], 403)

    def test_money_is_exact_decimal_and_rejects_floats(self):
        self.base()
        self.assertEqual(self.op('order', 'save', item=self.order(quoted_price='0.30'))[0], 200)
        stored = next(o for o in self.orders()['rows'] if o['id'] == 'ord-1')
        self.assertEqual((stored['quoted_price'], stored['quoted_text']), (30, '0.30'))
        for bad in ('1.005', '-1', 'abc', 1.5, True):
            self.assertEqual(self.op('order', 'save', item=self.order(id='ord-x', campaign_ids=[], quoted_price=bad))[0], 400, bad)
        pkg = next(p for p in self.state()['ad_packages'] if p['id'] == 'pkg-3')
        self.assertEqual(pkg['list_price'], 120000)

    def test_order_links_validate_tier_uniqueness_and_payment_consistency(self):
        self.base()
        self.assertEqual(self.mutate('/campaigns/ad-full', self.campaign(tier=1, format='图片', path='', placements=['home'], status='草稿'))[0], 200)
        self.assertEqual(self.op('order', 'save', item=self.order(campaign_ids=['ad-full']))[0], 400)
        self.assertEqual(self.op('order', 'save', item=self.order())[0], 200)
        row = self.orders()['rows'][0]
        self.assertRegex(row['code'], r'^PO-\d{8}-[0-9A-F]{4}$')
        self.assertEqual(self.op('order', 'save', item=self.order(id='ord-2'))[0], 409)
        for status, received, ok in [('部分收款', '1000.00', False), ('部分收款', '200.00', True), ('已收款', '999.99', False), ('已收款', '1000.00', True), ('未收款', '1.00', False), ('已退款', '0', True)]:
            code = self.op('order', 'save', item=self.order(payment_status=status, received_amount=received))[0]
            self.assertEqual(code == 200, ok, (status, received, code))
        audit = self.request('/api/admin/operations?section=audit&q=PO-', auth=True)[2]
        self.assertTrue(any('收款状态' in r.get('object', '') for r in audit['rows']))

    def test_operator_cannot_change_money_and_viewer_is_read_only(self):
        self.base()
        operator, viewer = self.login_as('opb', 'operator'), self.login_as('vwb', 'viewer')
        self.assertEqual(self.op('order', 'save', headers=operator, item=self.order(quoted_price=''))[0], 200)
        self.assertEqual(self.op('order', 'save', headers=operator, item=self.order(quoted_price='', notes='运营备注'))[0], 200)
        self.assertEqual(self.op('order', 'save', headers=operator, item=self.order(quoted_price='5.00'))[0], 403)
        self.assertEqual(self.op('package', 'save', headers=operator, item={'id': 'pkg-3', 'name': '夜间文字月包', 'tier': 3, 'placements': ['all'], 'list_price': '1.00'})[0], 403)
        self.assertEqual(self.op('order', 'save', headers=viewer, item=self.order(id='ord-v'))[0], 403)
        self.assertEqual(self.request('/api/admin/operations?section=orders', headers=viewer)[0], 200)
        stale = dict(kind='order', action='save', revision=self.state()['revision'] - 1, item=self.order())
        self.assertEqual(self.request('/api/admin/operations', stale, auth=True)[0], 409)

    def test_progress_counts_real_events_in_window_only(self):
        self.base()
        self.assertEqual(self.op('order', 'save', item=self.order())[0], 200)
        now = int(time.time() * 1000); old = now - 30 * 86400000
        events = [{'id': 'e1', 'type': 'text_started', 'creative_id': 'ad-night~all', 'at_ms': now},
                  {'id': 'e2', 'type': 'text_ended', 'creative_id': 'ad-night~all', 'visible_ms': 90 * 60000, 'at_ms': now},
                  {'id': 'e3', 'type': 'text_ended', 'creative_id': 'ad-night~all', 'visible_ms': 60 * 60000, 'at_ms': old},
                  {'id': 'e4', 'type': 'text_ended', 'creative_id': 'ad-night~all', 'visible_ms': 60 * 60000, 'at_ms': now, 'exclude_from_ad_reports': True}]
        self.store.record_events({'id': 'room-101', 'room': '101', 'group': 'A组'}, events)
        row = self.orders()['rows'][0]
        self.assertEqual(row['progress']['actual'], 150); self.assertEqual(row['progress']['percent'], 100)
        self.assertEqual(row['progress']['starts'], 1); self.assertEqual(row['state'], '执行中'); self.assertTrue(row['expiring'])
        self.assertEqual(row['campaigns'][0]['visible_ms'], 150 * 60000)

    def test_states_reminders_lifecycle_and_dangling_links(self):
        self.base()
        today = date.today()
        self.assertEqual(self.op('order', 'save', item=self.order(end=(today - timedelta(days=1)).isoformat(), start=(today - timedelta(days=5)).isoformat()))[0], 200)
        row = self.orders()['rows'][0]; self.assertEqual(row['state'], '已到期'); self.assertFalse(row['expiring'])
        self.assertEqual(self.op('order', 'save', item=self.order(start=(today + timedelta(days=2)).isoformat(), end=(today + timedelta(days=30)).isoformat()))[0], 200)
        self.assertEqual(self.orders()['rows'][0]['state'], '未开始')
        self.assertEqual(self.op('order', 'disable', ids=['ord-1'])[2]['ok'], ['ord-1'])
        self.assertEqual(self.orders()['rows'][0]['state'], '已停用')
        blocked = self.op('package', 'archive', ids=['pkg-3'])[2]
        self.assertFalse(blocked['ok']); self.assertIn('投放单', blocked['failed'][0]['reason'])
        self.assertEqual(self.op('order', 'archive', ids=['ord-1'])[2]['ok'], ['ord-1'])
        self.assertEqual(self.orders()['total'], 0); self.assertEqual(self.orders(archived='1')['rows'][0]['state'], '已归档')
        self.assertEqual(self.op('order', 'purge', ids=['ord-1'], confirm=True)[2]['ok'], ['ord-1'])
        self.assertEqual(self.op('package', 'archive', ids=['pkg-3'])[2]['ok'], ['pkg-3'])
        # A campaign removed after linking stays visible as a warning instead of breaking the list.
        self.assertEqual(self.op('package', 'restore', ids=['pkg-3'])[0], 200)
        self.assertEqual(self.op('package', 'enable', ids=['pkg-3'])[0], 200)
        self.assertEqual(self.op('order', 'save', item=self.order(id='ord-3'))[0], 200)
        self.assertEqual(self.op('campaign', 'archive', ids=['ad-night'])[0], 200)
        self.assertEqual(self.op('campaign', 'purge', ids=['ad-night'], confirm=True)[2]['ok'], ['ad-night'])
        row = self.orders()['rows'][0]
        self.assertTrue(any('已不存在' in w for w in row['warnings']))
        self.assertEqual(self.op('order', 'save', item=self.order(id='ord-3', notes='仍可编辑'))[0], 200)
        reopened = Store(self.data, self.media, self.config, [self.media, self.external])
        self.assertEqual([p['id'] for p in reopened.state()['ad_packages']], ['pkg-3'])
        self.assertGreaterEqual(self.request('/api/admin/operations?section=overview', auth=True)[2]['expiring_orders'], 1)


    def test_archive_renewal_and_restore_preserve_historical_report(self):
        self.base()
        self.assertEqual(self.op('order', 'save', item=self.order())[0], 200)
        now = int(time.time() * 1000)
        self.store.record_events({'id': 'room-101', 'room': '101'}, [
            {'id': 'history-90', 'type': 'text_ended', 'creative_id': 'ad-night~all', 'at_ms': now, 'visible_ms': 90 * 60000}])
        self.assertEqual(self.op('order', 'archive', ids=['ord-1'])[2]['ok'], ['ord-1'])
        today = date.today()
        renewal = self.order(id='ord-2', start=(today + timedelta(days=4)).isoformat(), end=(today + timedelta(days=10)).isoformat())
        self.assertEqual(self.op('order', 'save', item=renewal)[0], 200)
        query = {'advertiser': 'adv-1', 'from': (today - timedelta(days=4)).isoformat(), 'until': (today + timedelta(days=11)).isoformat()}
        rep = self.report(**query)[2]
        self.assertEqual(rep['totals']['visible_minutes'], 90)
        old = next(o for o in rep['orders'] if o['id'] == 'ord-1')
        self.assertEqual((old['range']['visible_minutes'], old['campaigns'][0]['visible_minutes'], old['target_progress']['actual']), (90, 90, 90))
        self.assertEqual(next(o for o in rep['orders'] if o['id'] == 'ord-2')['range']['visible_minutes'], 0)
        self.assertEqual(self.report(order='ord-1')[2]['totals']['visible_minutes'], 90)
        self.assertEqual(self.op('order', 'restore', ids=['ord-1'])[2]['ok'], ['ord-1'])
        self.assertEqual(self.report(**query)[2]['totals']['visible_minutes'], 90)

    def test_overlapping_windows_are_rejected_even_when_archived(self):
        self.base(); self.op('order', 'save', item=self.order())
        self.op('order', 'archive', ids=['ord-1'])
        self.assertEqual(self.op('order', 'save', item=self.order(id='overlap'))[0], 409)
        today = date.today()
        self.assertEqual(self.op('order', 'save', item=self.order(id='boundary', start=(today+timedelta(days=3)).isoformat(), end=(today+timedelta(days=8)).isoformat()))[0], 409)
        self.assertEqual(self.op('order', 'save', item=self.order(id='next', start=(today+timedelta(days=4)).isoformat(), end=(today+timedelta(days=8)).isoformat()))[0], 200)

    def test_same_campaign_nonoverlapping_periods_have_independent_totals(self):
        self.base(); today = date.today()
        old = self.order(start=(today-timedelta(days=20)).isoformat(), end=(today-timedelta(days=10)).isoformat())
        self.op('order', 'save', item=old)
        self.op('order', 'save', item=self.order(id='new'))
        now = int(time.time() * 1000)
        from unittest.mock import patch
        with patch('management.now_ms',return_value=now-15*86400000):
            self.store.record_events({'id':'room-101','room':'101'},[{'id':'past','type':'text_ended','creative_id':'ad-night~all','at_ms':now-15*86400000,'visible_ms':30*60000}])
        self.store.record_events({'id':'room-101','room':'101'},[{'id':'current','type':'text_ended','creative_id':'ad-night~all','at_ms':now,'visible_ms':60*60000}])
        rep = self.report(advertiser='adv-1', **{'from': (today-timedelta(days=21)).isoformat()})[2]
        self.assertEqual(rep['totals']['visible_minutes'], 90)
        self.assertEqual({o['id']: o['campaigns'][0]['visible_minutes'] for o in rep['orders']}, {'ord-1': 30, 'new': 60})
        self.assertEqual(sum(o['range']['visible_minutes'] for o in rep['orders']), rep['totals']['visible_minutes'])

    def test_legacy_ambiguous_windows_warn_instead_of_double_counting(self):
        self.base(); self.op('order', 'save', item=self.order())
        with self.store.db() as db:
            old = self.store.record(db, 'order', 'ord-1')
            self.store.put_object(db, 'order', {**old, 'id': 'legacy', 'code': 'legacy-overlap'})
        self.store.record_events({'id': 'room-101', 'room': '101'}, [
            {'id': 'ambiguous', 'type': 'text_ended', 'creative_id': 'ad-night~all', 'at_ms': int(time.time()*1000), 'visible_ms': 30*60000}])
        rep = self.report(advertiser='adv-1')[2]
        self.assertEqual(rep['totals']['visible_minutes'], 0)
        self.assertTrue(all(any('重叠' in w for w in o['warnings']) for o in rep['orders']))
        self.assertFalse(self.op('order', 'enable', ids=['legacy'])[2]['ok'])

    def report(self, **query):
        q = '&'.join('%s=%s' % (k, v) for k, v in {'section': 'merchant_report', **query}.items())
        return self.request('/api/admin/operations?' + q, auth=True)

    def test_unpresented_cancellations_do_not_inflate_interruption_reports(self):
        self.base()
        self.op('package', 'save', item={'id':'pkg-3','name':'全屏测试','tier':1,'placements':['home']})
        self.mutate('/campaigns/ad-night', self.campaign(tier=1,format='图片',placements=['home'],status='草稿'))
        self.op('order', 'save', item=self.order())
        now = int(time.time()*1000)
        self.store.record_events({'id': 'room-101', 'room': '101'}, [
            {'id': 'shown', 'type': 'ad_started', 'creative_id': 'ad-night~home', 'at_ms': now},
            {'id': 'shown-cancel', 'type': 'ad_cancelled', 'creative_id': 'ad-night~home', 'at_ms': now, 'presented': True, 'reason': 'channel_changed'},
            {'id': 'not-shown', 'type': 'ad_cancelled', 'creative_id': 'ad-night~home', 'at_ms': now, 'presented': False, 'reason': 'not_presented'}])
        merchant = self.report(advertiser='adv-1')[2]
        self.assertEqual((merchant['totals']['starts'], merchant['totals']['interrupted']), (1, 1))
        general = self.request('/api/admin/reports?days=7', auth=True)[2]
        self.assertEqual(general['rows'][0]['interrupted'], 1)

    def test_merchant_report_uses_only_real_events_in_range(self):
        self.base()
        self.assertEqual(self.mutate('/campaigns/ad-full', self.campaign(name='全屏图片', tier=1, format='图片', path='', placements=['home'], status='草稿'))[0], 200)
        self.assertEqual(self.op('package', 'save', item={'id': 'pkg-1', 'name': '首页全屏周包', 'tier': 1, 'placements': ['home'], 'default_days': 7, 'target': 100})[0], 200)
        self.assertEqual(self.op('order', 'save', item=self.order())[0], 200)
        self.assertEqual(self.op('order', 'save', item=self.order(id='ord-full', package_id='pkg-1', campaign_ids=['ad-full'], target=100, quoted_price=''))[0], 200)
        now = int(time.time() * 1000)
        self.store.record_events({'id': 'room-101', 'room': '101', 'group': 'A组'}, [
            {'id': 'r1', 'type': 'ad_started', 'creative_id': 'ad-full~home', 'at_ms': now},
            {'id': 'r2', 'type': 'ad_completed', 'creative_id': 'ad-full~home', 'at_ms': now},
            {'id': 'r3', 'type': 'text_started', 'creative_id': 'ad-night~all', 'at_ms': now},
            {'id': 'r4', 'type': 'text_ended', 'creative_id': 'ad-night~all', 'visible_ms': 90 * 60000, 'at_ms': now},
            {'id': 'r5', 'type': 'ad_started', 'creative_id': 'ad-full~home', 'at_ms': now - 40 * 86400000},
            {'id': 'r6', 'type': 'ad_started', 'creative_id': 'ad-full~home', 'at_ms': now, 'exclude_from_ad_reports': True}])
        self.store.record_events({'id': 'room-102', 'room': '102'}, [
            {'id': 'r7', 'type': 'ad_started', 'creative_id': 'ad-full~home', 'at_ms': now},
            {'id': 'r8', 'type': 'ad_failed', 'creative_id': 'ad-full~home', 'reason': 'image_load_failed', 'at_ms': now}])
        status, _, rep = self.report(advertiser='adv-1')
        self.assertEqual(status, 200)
        totals = rep['totals']
        self.assertEqual((totals['starts'], totals['completes'], totals['failed'], totals['visible_minutes'], totals['rooms']), (4, 1, 1, 90, 2))
        self.assertEqual(rep['failures'], [{'reason': 'image_load_failed', 'count': 1}])
        self.assertEqual(len(rep['timeline']), 7); self.assertFalse(rep['empty'])
        self.assertNotIn('quoted_price', json.dumps(rep))
        full = next(o for o in rep['orders'] if o['id'] == 'ord-full')
        self.assertEqual((full['range']['starts'], full['range']['rooms'], full['target_progress']['actual']), (3, 2, 3))
        single = self.report(order='ord-full')[2]
        self.assertEqual([o['id'] for o in single['orders']], ['ord-full']); self.assertEqual(single['totals']['visible_minutes'], 0)
        self.assertEqual(self.op('advertiser', 'save', item={'id': 'adv-2', 'name': '无投放商户'})[0], 200)
        self.assertTrue(self.report(advertiser='adv-2')[2]['empty'])
        self.assertEqual(self.report(advertiser='adv-1', **{'from': '2026-10-10', 'until': '2026-10-01'})[0], 400)
        self.assertEqual(self.report(order='missing')[0], 404)
        viewer = self.login_as('vwr', 'viewer')
        self.assertEqual(self.request('/api/admin/operations?section=merchant_report&advertiser=adv-1', headers=viewer)[0], 200)


if __name__ == '__main__':
    unittest.main()
