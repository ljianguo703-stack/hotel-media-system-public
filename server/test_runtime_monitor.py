import copy
import json
import tempfile
from pathlib import Path
import unittest
from runtime_monitor import RuntimeMonitor
import test_management as fixture


class Clock:
    def __init__(self):self.now=1800000000.0
    def __call__(self):return self.now


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.clock=Clock()
        self.raw={'available':True,'hostname':'test','system':'Linux','boot_id':'boot-a','cpu_ticks':[100,60],
                  'interfaces':[{'id':'eth0','ip':'192.0.2.10','rx_bytes':1000,'tx_bytes':2000}]}
        self.monitor=RuntimeMonitor(self.temp.name,'UTC',lambda:copy.deepcopy(self.raw),self.clock,self.clock)
    def tearDown(self):self.monitor.close();self.temp.cleanup()
    def step(self,seconds=2):self.clock.now+=seconds;self.monitor.sample()

    def test_first_sample_unknown_then_real_delta_and_cpu(self):
        self.assertIsNone(self.monitor.interfaces[0]['rx_bps'])
        self.raw['interfaces'][0]['rx_bytes']+=200;self.raw['interfaces'][0]['tx_bytes']+=400
        self.raw['cpu_ticks']=[200,100];self.step()
        self.assertEqual(self.monitor.interfaces[0]['rx_bps'],100)
        self.assertEqual(self.monitor.interfaces[0]['today_tx'],400)
        self.assertEqual(self.monitor.system['cpu_percent'],60)

    def test_kernel_counter_reset_or_boot_does_not_create_false_traffic(self):
        self.raw['interfaces'][0]['tx_bytes']=30;self.step()
        self.assertIsNone(self.monitor.interfaces[0]['tx_bps'])
        self.raw['boot_id']='boot-b';self.raw['interfaces'][0]['tx_bytes']=9999999;self.step()
        self.assertIsNone(self.monitor.interfaces[0]['tx_bps']);self.assertEqual(self.monitor.interfaces[0]['today_tx'],0)

    def test_session_reconnect_and_ip_changes_have_new_start(self):
        self.monitor.touch('one','192.0.2.101');start=self.monitor.sessions['one']['since']
        self.clock.now+=30;self.monitor.touch('one','192.0.2.101');self.assertEqual(self.monitor.sessions['one']['since'],start)
        self.clock.now+=121;self.monitor.touch('one','192.0.2.101');self.assertEqual(self.monitor.sessions['one']['since'],self.clock.now)
        self.clock.now+=1;self.monitor.touch('one','192.0.2.102');self.assertEqual(self.monitor.sessions['one']['since'],self.clock.now)

    def test_payload_direction_filter_and_restart_persistence(self):
        self.monitor.touch('one','192.0.2.101');self.monitor.add('one',rx=20,tx=1000);self.step()
        rows=[{'id':'one','room':'101','online':True,'ip':'192.0.2.101'}, {'id':'off','online':False}, {'id':'disabled','online':True,'enabled':False}]
        result=self.monitor.snapshot(rows);self.assertEqual(len(result['devices']),1)
        self.assertEqual(result['devices'][0]['today_rx'],1000);self.assertEqual(result['devices'][0]['today_tx'],20)
        self.monitor.flush()
        reopened=RuntimeMonitor(self.temp.name,'UTC',lambda:copy.deepcopy(self.raw),self.clock,self.clock)
        try:self.assertEqual(reopened.snapshot(rows)['devices'][0]['today_rx'],1000)
        finally:reopened.close()

    def test_new_day_does_not_reuse_yesterday_totals(self):
        self.monitor.add('one',tx=4096);self.step();self.clock.now+=86400
        rows=[{'id':'one','online':True,'ip':'192.0.2.1'}]
        self.assertIsNone(self.monitor.snapshot(rows)['devices'][0]['today_rx'])

    def test_absent_session_is_unknown_not_last_seen(self):
        result=self.monitor.snapshot([{'id':'one','online':True,'last_seen':int(self.clock()*1000),'ip':'192.0.2.1'}])
        self.assertIsNone(result['devices'][0]['online_since'])

    def test_timezone_change_does_not_mix_daily_counters(self):
        self.monitor.add('one',tx=1024)
        rows=[{'id':'one','online':True,'ip':'192.0.2.1'}]
        result=self.monitor.snapshot(rows,'Asia/Singapore')
        self.assertIsNone(result['devices'][0]['today_rx'])
        self.monitor.add('one',tx=9)
        self.assertEqual(self.monitor.snapshot(rows)['devices'][0]['today_rx'],9)

    def test_corrupt_optional_statistics_do_not_stop_cinema_service(self):
        root=Path(self.temp.name)/'corrupt';root.mkdir();(root/'runtime-metrics.sqlite3').write_bytes(b'bad db')
        bad=RuntimeMonitor(root,reader=lambda:copy.deepcopy(self.raw),clock=self.clock,monotonic=self.clock)
        try:
            self.assertTrue(bad.snapshot([])['server']['available'])
            self.assertTrue(bad.snapshot([])['error'])
        finally:bad.close()


class DashboardHTTPTests(unittest.TestCase):
    setUp=fixture.ManagementE2E.setUp
    tearDown=fixture.ManagementE2E.tearDown
    request=fixture.ManagementE2E.request
    state=fixture.ManagementE2E.state
    def test_dashboard_auth_real_body_bytes_and_online_session(self):
        self.assertEqual(self.request('/api/admin/dashboard')[0],401)
        self.request('/api/v1/catalog',tv=True,headers={'X-TV-Probe':'0'})
        monitor=self.store.runtime_monitor
        before=monitor.snapshot(self.state()['devices'])['devices'][0]['today_rx']
        status,_,payload=self.request('/media/clip.mp4',headers={'Range':'bytes=2-5','X-TV-Probe':'0'},raw=True,tv=True)
        self.assertEqual((status,payload),(206,b'2345'))
        self.assertEqual(monitor.snapshot(self.state()['devices'])['devices'][0]['today_rx']-before,4)
        status,_,result=self.request('/api/admin/dashboard',auth=True)
        self.assertEqual(status,200);self.assertTrue(result['devices'][0]['online_since'])
        self.assertNotIn('token',json.dumps(result));self.assertNotIn('csrf',json.dumps(result))
        with self.store.db() as db:db.execute('UPDATE devices SET last_seen=1')
        self.assertEqual(self.request('/api/admin/dashboard',auth=True)[2]['devices'],[])


if __name__=='__main__':unittest.main()
