"""Fleet acceptance with many isolated rooms, no physical TVs or private data."""
import hashlib
import json
import unittest
from unittest.mock import patch
from fleet_store import FleetRepository, encoded, now
from software_packages import SoftwareError
from software_agent import Agent
import test_management as management_tests


class FleetTests(unittest.TestCase):
    def setUp(self):
        management_tests.ManagementE2E.setUp(self)
        ready=patch.object(FleetRepository,'executor_ready',return_value=True)
        ready.start();self.addCleanup(ready.stop)
    tearDown = management_tests.ManagementE2E.tearDown
    request = management_tests.ManagementE2E.request

    def repo(self): return FleetRepository(self.data)

    def rooms(self, count=150):
        ids = []
        with self.store.db() as db:
            for n in range(count):
                identity = hashlib.sha256(('fixture-'+str(n)).encode()).hexdigest()
                ident = 'tv-'+hashlib.sha256(('hotel-tv-backend-device:'+identity).encode()).hexdigest()[:16]
                d = {'id':ident,'room':str(100+n),'group':'二楼' if n%2 else '一楼','enabled':True}
                db.execute('INSERT OR REPLACE INTO devices VALUES(?,?,?,?,?,?)',(ident,encoded(d),'fixture-credential-'+str(n),now(),'192.168.20.'+str(10+n),''))
                ep = {'id':ident,'address':'192.168.20.'+str(10+n),'port':5555,'identity_sha256':identity,'enrollment':'verified','reboot_verified':True}
                db.execute('INSERT OR REPLACE INTO software_targets VALUES(?,?)',(ident,encoded(ep)))
                db.execute('INSERT OR REPLACE INTO fleet_status VALUES(?,?)',(ident,encoded({'protocol':1,'at':now(),'sdk':28,'keep_adb':True,'overlay':True})))
                ids.append(ident)
            item = {'id':'pkg-'+'a'*32,'kind':'apk','platform':'tv','software_id':'com.example.guestapp','version':'2.0','name':'测试应用','sha256':'a'*64,'min_sdk':21}
            db.execute('INSERT OR REPLACE INTO software_packages VALUES(?,?)',(item['id'],encoded(item)))
        return ids,item

    def revision(self):
        with self.repo().db() as db:return self.repo().revision(db)

    def plan(self, ids, item, action='install'):
        return self.repo().preflight({'targets':ids,'package_id':item['id'],'action':action},'admin',self.revision())

    def test_150_room_preview_never_executes_and_retry_is_idempotent(self):
        ids,item=self.rooms()
        plan=self.plan(ids,item)
        self.assertEqual(len(plan['rows']),150);self.assertTrue(all(r['ready'] for r in plan['rows']))
        with self.repo().db() as db:self.assertEqual(db.execute('SELECT count(*) FROM software_jobs').fetchone()[0],0)
        result=self.repo().execute(plan['id'],'admin',self.revision(),True)
        self.assertEqual(len(result['jobs']),150)
        again=self.repo().execute(plan['id'],'admin',self.revision(),True)
        self.assertEqual([j['id'] for j in result['jobs']],[j['id'] for j in again['jobs']])

    def test_old_executor_cannot_enqueue_new_maintenance_operations(self):
        ids,item=self.rooms(1)
        with patch.object(FleetRepository,'executor_ready',return_value=False):
            self.assertFalse(self.plan(ids,item)['rows'][0]['ready'])
            with self.assertRaises(SoftwareError):self.repo().probe(ids[0],'admin',self.revision())
            with self.repo().db() as db:self.assertEqual(db.execute('SELECT count(*) FROM software_jobs').fetchone()[0],0)

    def test_offline_unenrolled_and_incompatible_are_explicitly_excluded(self):
        ids,item=self.rooms(4)
        with self.repo().db() as db:
            db.execute('UPDATE devices SET last_seen=0 WHERE id=?',(ids[0],))
            db.execute('DELETE FROM software_targets WHERE id=?',(ids[1],))
            db.execute('UPDATE fleet_status SET body=? WHERE id=?',(encoded({'at':now(),'keep_adb':True,'sdk':19}),ids[2]))
        plan=self.plan(ids,item)
        self.assertEqual([r['ready'] for r in plan['rows']],[False,False,False,True])
        jobs=self.repo().execute(plan['id'],'admin',self.revision(),True)['jobs']
        self.assertEqual([j['target'] for j in jobs],[ids[3]])

    def test_room_or_token_change_and_expiry_refuse_old_plan(self):
        ids,item=self.rooms(1);p=self.plan(ids,item)
        with self.repo().db() as db:db.execute('UPDATE devices SET token=? WHERE id=?',('fixture-rotated',ids[0]))
        with self.assertRaises(SoftwareError):self.repo().execute(p['id'],'admin',self.revision(),True)
        p=self.plan(ids,item)
        with patch('fleet_store.now',return_value=p['expires_at']+1):
            with self.assertRaises(SoftwareError):self.repo().execute(p['id'],'admin',self.revision(),True)

    def test_first_failure_stops_remaining_149_rooms(self):
        ids,item=self.rooms();p=self.plan(ids,item)
        jobs=self.repo().execute(p['id'],'admin',self.revision(),True)['jobs']
        agent=Agent(self.data,self.root/'agent')
        j=jobs[0];j['status']='failed'
        with self.repo().db() as db:self.repo().write_job(db,j)
        agent.fleet_finished(j)
        state=self.repo().state();batch=next(b for b in state['batches'] if b['id']==p['id'])
        self.assertEqual(batch['status'],'paused')
        self.assertEqual(sum(j['status']=='cancelled' for j in batch['jobs']),149)

    def test_worker_crash_stops_the_rest_instead_of_continuing(self):
        ids,item=self.rooms(3);p=self.plan(ids,item)
        jobs=self.repo().execute(p['id'],'admin',self.revision(),True)['jobs']
        with self.repo().db() as db:
            first=jobs[0];first['status']='running';self.repo().write_job(db,first)
        Agent(self.data,self.root/'agent').recover()
        batch=self.repo().state()['batches'][0]
        self.assertEqual(batch['status'],'paused')
        self.assertEqual([j['status'] for j in batch['jobs']],['interrupted','cancelled','cancelled'])

    def test_protected_and_unmanaged_apps_cannot_be_removed(self):
        ids,item=self.rooms(1)
        p=self.plan(ids,item,'uninstall');self.assertFalse(p['rows'][0]['ready'])
        item['software_id']='com.xjg.hoteltv'
        with self.repo().db() as db:db.execute('UPDATE software_packages SET body=? WHERE id=?',(encoded(item),item['id']))
        with self.assertRaises(SoftwareError):self.plan(ids,item,'uninstall')

    def test_wrong_physical_identity_never_changes_an_app(self):
        ids,item=self.rooms(1);p=self.plan(ids,item);j=self.repo().execute(p['id'],'admin',self.revision(),True)['jobs'][0]
        calls=[]
        def runner(args,**kwargs):calls.append(args);return 'another-device-id'
        agent=Agent(self.data,self.root/'agent',runner=runner)
        with self.assertRaises(SoftwareError):agent.fleet_identity(j,['adb'],'192.168.20.10:5555')
        self.assertTrue(all('install' not in args and 'uninstall' not in args for args in calls))

    def publish(self, ids, **values):
        return self.repo().publish({'targets':ids,'title':'酒店通知','body':'早餐时间调整','display_seconds':30,'valid_minutes':10,'confirm':True,'request_id':'notice-fixture-12345',**values},'admin',self.revision())

    def test_notice_is_targeted_expiring_withdrawable_and_idempotent(self):
        ids,_=self.rooms(2);m=self.publish(ids[:1])
        self.assertEqual(self.publish(ids[:1])['id'],m['id'])
        self.assertEqual(len(self.repo().pending_messages({'id':ids[0]})['messages']),1)
        self.assertEqual(self.repo().pending_messages({'id':ids[1]})['messages'],[])
        with self.assertRaises(SoftwareError):self.repo().receipt({'id':ids[1]},{'id':m['id'],'status':'displayed'})
        with patch('fleet_store.now',return_value=m['expires_at']+1):self.assertEqual(self.repo().pending_messages({'id':ids[0]})['messages'],[])
        self.repo().withdraw(m['id'],self.revision())
        self.assertEqual(self.repo().pending_messages({'id':ids[0]})['messages'],[])

    def test_notice_read_receipts_do_not_regress_or_claim_unreceived_as_displayed(self):
        ids,_=self.rooms(1);m=self.publish(ids)
        d={'id':ids[0]}
        state=self.repo().state();self.assertEqual(state['messages'][0]['receipts'][0]['status'],'pending')
        self.repo().receipt(d,{'id':m['id'],'status':'displayed'})
        self.repo().receipt(d,{'id':m['id'],'status':'fetched'})
        self.assertEqual(self.repo().state()['messages'][0]['receipts'][0]['status'],'displayed')
        self.repo().receipt(d,{'id':m['id'],'status':'dismissed'})
        self.assertEqual(self.repo().pending_messages(d)['messages'],[])

    def test_admin_auth_csrf_and_invalid_targets(self):
        self.assertEqual(self.request('/api/admin/fleet',auth=False)[0],401)
        self.assertEqual(self.request('/api/admin/fleet/preflight',{},auth=True,headers={'X-CSRF-Token':'bad'})[0],403)
        ids,item=self.rooms(1)
        for rows in ([],ids*2,['bad/target'],['x']*501):
            with self.assertRaises(SoftwareError):self.plan(rows,item)

    def test_device_notice_http_requires_token_and_records_display_separately(self):
        m=self.publish(['room-101'])
        self.assertEqual(self.request('/api/v1/messages',tv=True,headers={'X-TV-Token':'bad'})[0],401)
        self.assertEqual(self.request('/api/v1/messages',tv=True)[2]['messages'][0]['id'],m['id'])
        self.assertEqual(self.request('/api/v1/messages/receipt',{'id':m['id'],'status':'displayed'},tv=True)[0],200)
        self.assertEqual(self.repo().state()['messages'][0]['receipts'][0]['status'],'displayed')
        self.assertEqual(self.request('/api/v1/managed-status',{'protocol':1,'overlay':True,'screen_awake':False,'keep_adb':True,'version':'0.1.10','version_code':11,'model':'fixture','sdk':28},tv=True)[0],200)
        self.assertEqual(self.request('/api/v1/messages/receipt',{'id':m['id'],'status':'invented'},tv=True)[0],400)

    def test_retained_data_uninstall_is_verified(self):
        ids,item=self.rooms(1)
        with self.repo().db() as db:db.execute('INSERT INTO software_inventory VALUES(?,?,?)',(ids[0],item['software_id'],encoded({**item,'installed':True})))
        plan=self.plan(ids,item,'uninstall');jobs=self.repo().execute(plan['id'],'admin',self.revision(),True)['jobs']
        installed=[True];calls=[]
        def runner(args,**kwargs):
            calls.append(args)
            if 'connect' in args:return 'connected to 192.168.20.10:5555'
            if 'devices' in args:return '192.168.20.10:5555\tdevice\n'
            if 'android_id' in args:return 'fixture-0'
            if 'resolve-activity' in args:return 'com.vendor.launcher/.Home'
            if 'uninstall' in args:
                self.assertIn('shell',args);self.assertIn('pm',args)
                self.assertEqual(args[-5:],['uninstall','-k','--user','0',item['software_id']])
                self.assertTrue(kwargs.get('with_code'));installed[0]=False;return (0,'Success\n')
            if 'packages' in args:return 'package:'+item['software_id']+'\n' if installed[0] else ''
            if 'disconnect' in args:return 'disconnected'
            raise AssertionError(args)
        with patch('fleet_agent.tool',return_value='adb'):
            self.assertTrue(Agent(self.data,self.root/'agent',runner=runner).once())
        j=self.repo().state()['batches'][0]['jobs'][0]
        self.assertEqual(j['status'],'completed');self.assertFalse(installed[0])
        with self.repo().db() as db:self.assertFalse(json.loads(db.execute('SELECT body FROM software_inventory WHERE target=?',(ids[0],)).fetchone()[0])['installed'])

    def test_current_home_or_unknown_home_never_uninstalls(self):
        ids,item=self.rooms(1)
        with self.repo().db() as db:db.execute('INSERT INTO software_inventory VALUES(?,?,?)',(ids[0],item['software_id'],encoded({**item,'installed':True})))
        for home in (item['software_id']+'/.Home','No activity found'):
            plan=self.plan(ids,item,'uninstall')
            self.repo().execute(plan['id'],'admin',self.revision(),True)
            calls=[]
            def runner(args,**kwargs):
                calls.append(args)
                if 'connect' in args:return 'connected'
                if 'devices' in args:return '192.168.20.10:5555\tdevice\n'
                if 'android_id' in args:return 'fixture-0'
                if 'resolve-activity' in args:return home
                if 'packages' in args:return 'package:'+item['software_id']+'\n'
                if 'disconnect' in args:return 'disconnected'
                raise AssertionError(args)
            with patch('fleet_agent.tool',return_value='adb'):self.assertTrue(Agent(self.data,self.root/'agent',runner=runner).once())
            self.assertFalse(any('uninstall' in c for c in calls))
            self.assertEqual(self.repo().state()['batches'][0]['jobs'][0]['status'],'failed')

    def test_native_uninstall_failure_keeps_inventory_and_stops_batch(self):
        ids,item=self.rooms(1)
        with self.repo().db() as db:db.execute('INSERT INTO software_inventory VALUES(?,?,?)',(ids[0],item['software_id'],encoded({**item,'installed':True})))
        plan=self.plan(ids,item,'uninstall');self.repo().execute(plan['id'],'admin',self.revision(),True)
        def runner(args,**kwargs):
            if 'connect' in args:return 'connected'
            if 'devices' in args:return '192.168.20.10:5555\tdevice\n'
            if 'android_id' in args:return 'fixture-0'
            if 'resolve-activity' in args:return 'com.vendor.launcher/.Home'
            if 'uninstall' in args:return (1,'Failure [DELETE_FAILED_INTERNAL_ERROR]\n')
            if 'packages' in args:return 'package:'+item['software_id']+'\n'
            if 'disconnect' in args:return 'disconnected'
            raise AssertionError(args)
        with patch('fleet_agent.tool',return_value='adb'):Agent(self.data,self.root/'agent',runner=runner).once()
        batch=self.repo().state()['batches'][0]
        self.assertEqual(batch['status'],'paused');self.assertIn('DELETE_FAILED_INTERNAL_ERROR',batch['jobs'][0]['message'])
        with self.repo().db() as db:self.assertTrue(json.loads(db.execute('SELECT body FROM software_inventory WHERE target=?',(ids[0],)).fetchone()[0])['installed'])

    def test_reenrollment_cannot_replace_endpoint_while_a_job_is_active(self):
        ids,_=self.rooms(1)
        body={'id':ids[0],'identity_sha256':hashlib.sha256(b'fixture-0').hexdigest(),'address':'192.168.20.10','port':5555,'confirm':True,'online_confirmed':True,'reboot_verified':True}
        first=self.repo().enroll(body,'admin',self.revision())
        with self.assertRaises(SoftwareError):self.repo().enroll({**body,'address':'192.168.20.11'},'admin',self.revision())
        with self.repo().db() as db:
            self.assertEqual(self.repo().read(db,'software_targets',ids[0]),first['target'])
            self.assertEqual(db.execute('SELECT count(*) FROM software_jobs').fetchone()[0],1)

    def test_server_enrollment_only_verifies_same_physical_device_with_fresh_management_report(self):
        ids,_=self.rooms(1)
        identity=hashlib.sha256(b'fixture-0').hexdigest()
        result=self.repo().enroll({'id':ids[0],'identity_sha256':identity,'address':'192.168.20.10','port':5555,'confirm':True,'online_confirmed':True,'reboot_verified':True},'admin',self.revision())
        self.assertEqual(result['target']['enrollment'],'pending')
        def runner(args,**kwargs):
            if 'connect' in args:return 'connected to 192.168.20.10:5555'
            if 'devices' in args:return '192.168.20.10:5555\tdevice\n'
            if 'android_id' in args:return 'fixture-0'
            if 'disconnect' in args:return 'disconnected'
            raise AssertionError(args)
        with patch('fleet_agent.tool',return_value='adb'):self.assertTrue(Agent(self.data,self.root/'agent',runner=runner).once())
        with self.repo().db() as db:self.assertEqual(self.repo().read(db,'software_targets',ids[0])['enrollment'],'verified')


if __name__=='__main__':unittest.main()
