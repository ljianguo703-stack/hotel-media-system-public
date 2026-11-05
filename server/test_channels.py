"""TV02: virtual channels on a shared timeline, ad breaks, time slots, guide export and the TV endpoint."""
import http.client
import json
import shutil
import subprocess
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch
import test_management as fixture
import test_orders

FFMPEG = shutil.which('ffmpeg') and shutil.which('ffprobe')


@unittest.skipUnless(FFMPEG, 'ffmpeg/ffprobe not installed')
class ChannelsE2E(unittest.TestCase):
    setUp = fixture.ManagementE2E.setUp
    tearDown = fixture.ManagementE2E.tearDown
    request = fixture.ManagementE2E.request
    state = fixture.ManagementE2E.state
    mutate = fixture.ManagementE2E.mutate
    wait_scan = fixture.ManagementE2E.wait_scan
    campaign = fixture.ManagementE2E.campaign
    op = test_orders.OrdersE2E.op
    login_as = test_orders.OrdersE2E.login_as

    def clip(self, name, seconds):
        (self.media / '电影').mkdir(exist_ok=True)
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc=size=160x120:rate=10', '-f', 'lavfi', '-i', 'sine=sample_rate=8000',
                        '-t', str(seconds), '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(self.media / '电影' / name)], check=True)

    def library(self):
        self.clip('甲.mp4', 4); self.clip('乙.mp4', 2)
        self.wait_scan('main')
        return {m['title']: m['id'] for m in self.state()['media']}

    def channels_api(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.tv.server_port, timeout=10)
        conn.request('GET', '/api/v1/channels', headers={'X-TV-Token': 'tv-test-token'}); r = conn.getresponse(); body = json.loads(r.read()); conn.close()
        return r.status, body

    def save(self, **item):
        base = {'id': 'ch-1', 'number': 1, 'name': '电影台', 'mode': 'loop', 'items': [], 'breaks': {'enabled': True, 'seconds': 10, 'every': 1}}
        return self.op('channel', 'save', item={**base, **item})

    def test_loop_channel_timeline_breaks_and_tv_endpoint(self):
        ids = self.library()
        status, _, saved = self.save(items=[ids['甲'], ids['乙']])
        self.assertEqual(status, 200, saved)
        status, body = self.channels_api()
        self.assertEqual(status, 200)
        entries = body['channels'][0]['entries']
        self.assertTrue(all(a['end_ms'] == b['start_ms'] for a, b in zip(entries, entries[1:])), 'timeline must be contiguous')
        kinds = [e['kind'] for e in entries[:8]]
        self.assertIn('break', kinds); self.assertIn('program', kinds)
        lengths = {e['title']: e['end_ms'] - e['start_ms'] for e in entries[1:9]}
        self.assertAlmostEqual(lengths['甲'], 4000, delta=150); self.assertAlmostEqual(lengths['广告时段'], 10000, delta=1)
        now = body['server_time_ms']
        self.assertTrue(any(e['start_ms'] <= now < e['end_ms'] for e in entries))
        self.assertNotIn('route', json.dumps(body)); self.assertTrue(all('path' in e for e in entries if e['kind'] == 'program'))
        self.assertEqual(self.channels_api()[1]['channels'][0]['entries'][0]['start_ms'], entries[0]['start_ms'], 'every TV and every poll sees the same timeline')
        # Disabled channels disappear for TVs but stay in the admin preview.
        self.assertEqual(self.save(items=[ids['甲'], ids['乙']], enabled=False)[0], 200)
        self.assertEqual(self.channels_api()[1]['channels'], [])
        admin = self.request('/api/admin/operations?section=channels', auth=True)[2]['channels'][0]
        self.assertEqual(admin['programmes'], 2); self.assertTrue(admin['preview'])
        # A missing source file is skipped instead of leaving a black gap.
        self.assertEqual(self.save(items=[ids['甲'], ids['乙']], enabled=True)[0], 200)
        with self.store.db() as db: db.execute('UPDATE media SET available=0 WHERE id=?', (ids['乙'],))
        titles = {e['title'] for e in self.channels_api()[1]['channels'][0]['entries']}
        self.assertNotIn('乙', titles); self.assertIn('甲', titles)

    def test_shuffle_order_is_saved_not_reshuffled_each_poll(self):
        ids = self.library()
        saved = self.save(mode='shuffle', items=[ids['甲'], ids['乙']])[2]
        self.assertEqual(len(saved['order']), 2)
        first = [e['title'] for e in self.channels_api()[1]['channels'][0]['entries'] if e['kind'] == 'program'][:4]
        self.assertEqual(first, [e['title'] for e in self.channels_api()[1]['channels'][0]['entries'] if e['kind'] == 'program'][:4])
        again = self.save(mode='shuffle', items=[ids['甲'], ids['乙']], name='改名不重洗')[2]
        self.assertEqual(again['order'], saved['order'])
        self.assertEqual(again['epoch'], saved['epoch'])

    def test_time_slots_start_at_slot_start_with_off_air_filler(self):
        ids = self.library()
        zone = ZoneInfo('Asia/Singapore'); now = datetime.now(zone)
        start = (now - timedelta(minutes=30)).strftime('%H:%M'); end = (now + timedelta(minutes=30)).strftime('%H:%M')
        if start > end: self.skipTest('slot would cross midnight in this run')
        status, _, saved = self.save(mode='slots', slots=[{'start': start, 'end': end, 'items': [ids['甲']]}])
        self.assertEqual(status, 200, saved)
        entries = self.store.channel_payload(int(now.timestamp() * 1000), 3600000, 3600000)[0]['entries']
        programmes = [e for e in entries if e['kind'] == 'program']
        slot_start = int(now.replace(hour=int(start[:2]), minute=int(start[3:]), second=0, microsecond=0).timestamp() * 1000)
        self.assertEqual(min(p['start_ms'] for p in programmes if p['start_ms'] >= slot_start - 1), slot_start)
        self.assertTrue(any(e['kind'] == 'filler' for e in entries))
        self.assertTrue(all(a['end_ms'] == b['start_ms'] for a, b in zip(entries, entries[1:])))
        self.assertEqual(self.save(mode='slots', slots=[{'start': '08:00', 'end': '12:00', 'items': [ids['甲']]}, {'start': '11:00', 'end': '13:00', 'items': [ids['乙']]}])[0], 400)
        self.assertEqual(self.save(mode='slots', slots=[{'start': '08:00', 'end': '08:00', 'items': [ids['甲']]}])[0], 400)

    def test_validation_permissions_xmltv_and_channel_ad_placement(self):
        ids = self.library()
        self.assertEqual(self.save(items=[ids['甲']])[0], 200)
        self.assertEqual(self.save(id='ch-2', items=[ids['乙']])[0], 409)
        self.assertEqual(self.save(id='ch-3', number=3, items=[])[0], 400)
        self.assertEqual(self.save(id='ch-4', number=4, items=['nope'])[0], 400)
        viewer = self.login_as('vwch', 'viewer')
        self.assertEqual(self.op('channel', 'save', headers=viewer, item={'id': 'ch-v', 'number': 9, 'name': 'x', 'items': [ids['甲']]})[0], 403)
        xml = self.request('/api/admin/operations?section=xmltv', auth=True)[2]['xml']
        root = ET.fromstring(xml.encode())
        self.assertEqual(root.find('channel/display-name').text, '电影台'); self.assertTrue(root.findall('programme'))
        self.assertEqual(self.mutate('/campaigns/ad-channel', self.campaign(tier=1, format='图片', path='', placements=['channel'], status='草稿', start='00:00', end='00:00'))[0], 200)

    def test_before_mid_after_and_ident_share_a_contiguous_timeline(self):
        ids = self.library()
        path = next(m['path'] for m in self.state()['media'] if m['id'] == ids['乙'])
        self.assertEqual(self.op('asset', 'save', item={'id': 'ident', 'name': '本地台标片', 'path': path})[0], 200)
        self.mutate('/campaigns/channel-ad', self.campaign(tier=1, format='图片', placements=['channel'], status='草稿'))
        rules = [
            {'id': 'intro', 'media_id': ids['甲'], 'position': 'before', 'kind': 'ident', 'asset_id': 'ident'},
            {'id': 'middle', 'media_id': ids['甲'], 'position': 'at', 'at_seconds': 2, 'kind': 'ad', 'seconds': 1, 'campaign_id': 'channel-ad'},
            {'id': 'outro', 'media_id': ids['甲'], 'position': 'after', 'kind': 'ad', 'seconds': 3},
        ]
        status, _, channel = self.save(items=[ids['甲']], inserts=rules, breaks={'enabled': False})
        self.assertEqual(status, 200, channel)
        with self.store.db() as db: seq = self.store.channel_sequence(db, channel, channel['items'], set(ids.values()))
        self.assertEqual([e['kind'] for e in seq], ['ident', 'program', 'break', 'program', 'break'])
        self.assertEqual([e['offset_ms'] for e in seq if e['kind'] == 'program'], [0, 2000])
        self.assertEqual(seq[2]['campaign_id'], 'channel-ad')
        self.assertAlmostEqual(sum(e['length'] for e in seq), 10000, delta=150)
        entries = self.channels_api()[1]['channels'][0]['entries']
        self.assertTrue(all(a['end_ms'] == b['start_ms'] for a, b in zip(entries, entries[1:])))
        self.assertTrue(any(e.get('offset_ms') == 2000 for e in entries))
        self.assertNotIn('route', json.dumps(entries))
        # Referenced assets/campaigns cannot be purged behind the schedule; disabled idents keep their slot as filler.
        self.assertFalse(self.op('asset', 'archive', ids=['ident'])[2]['ok'])
        self.assertFalse(self.op('campaign', 'archive', ids=['channel-ad'])[2]['ok'])
        self.op('asset', 'disable', ids=['ident'])
        with self.store.db() as db: fallback = self.store.channel_sequence(db, channel, channel['items'], set(ids.values()))
        self.assertEqual(fallback[0]['kind'], 'filler')
        self.assertEqual(sum(e['length'] for e in fallback), sum(e['length'] for e in seq))

    def test_insert_validation_and_legacy_programme_break_deduplication(self):
        ids = self.library()
        base = {'id': 'rule', 'media_id': ids['甲'], 'position': 'at', 'at_seconds': 2, 'kind': 'ad', 'seconds': 10}
        for change in ({'at_seconds': 4}, {'at_seconds': 0}, {'media_id': 'missing'}, {'kind': 'unknown'}, {'campaign_id': 'missing'}, {'kind': 'ident', 'asset_id': 'missing'}):
            self.assertEqual(self.save(items=[ids['甲']], inserts=[{**base, **change}])[0], 400, change)
        self.assertEqual(self.save(items=[ids['甲']], inserts=[base, base])[0], 400)
        status, _, channel = self.save(items=[ids['甲']], inserts=[{**base, 'position': 'after'}])
        self.assertEqual(status, 200)
        with self.store.db() as db: seq = self.store.channel_sequence(db, channel, channel['items'], set(ids.values()))
        self.assertEqual([e['kind'] for e in seq], ['program', 'break'], 'explicit postroll replaces the automatic programme break')

    def test_stale_save_is_rejected_before_any_probe_side_effect(self):
        with patch.object(self.store, 'ensure_durations') as probe:
            status, _, _ = self.request('/api/admin/operations', {'kind': 'channel', 'action': 'save', 'revision': self.state()['revision']-1,
                'item': {'id': 'stale', 'number': 7, 'name': '过期修改', 'mode': 'loop', 'items': ['existing']}}, auth=True)
        self.assertEqual(status, 409); probe.assert_not_called()


if __name__ == '__main__':
    unittest.main()
