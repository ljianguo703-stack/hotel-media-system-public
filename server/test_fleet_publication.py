"""Durable room publications: offline/idle/restart/withdrawal and exact package evidence."""
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from fleet_store import FleetRepository, encoded, now
from software_agent import Agent
from software_packages import SoftwareError
import test_fleet as fleet_tests

class PublicationFixture:
    setUp=fleet_tests.FleetTests.setUp
    tearDown=fleet_tests.FleetTests.tearDown
    request=fleet_tests.FleetTests.request
    repo=fleet_tests.FleetTests.repo
    revision=fleet_tests.FleetTests.revision

    def rooms(self,count=1):
        ids,item=fleet_tests.FleetTests.rooms(self,count)
        item.update(version_code=2,filename='guest.apk',bytes=9,sha256=hashlib.sha256(b'candidate').hexdigest())
        with self.repo().db() as db:
            db.execute('UPDATE software_packages SET body=? WHERE id=?',(encoded(item),item['id']))
            for ident in ids:
                db.execute('UPDATE fleet_status SET body=? WHERE id=?',(encoded(self.status()),ident))
        p=self.repo().packages/(item['id']+'.apk');p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'candidate')
        return ids,item

    def status(self,**changes):
        return dict(protocol=2,at=now(),home_idle=True,idle_ms=65000,maintenance_job='',screen_awake=True,
                    keep_adb=True,overlay=True,version='0.1.11',version_code=12,model='fixture',sdk=28,**changes)

    def seen(self,ident,**changes):
        value=self.status();value.update(changes)
        with self.repo().db() as db:db.execute('UPDATE devices SET last_seen=? WHERE id=?',(now(),ident))
        return self.repo().report({'id':ident},value)

    def offline(self,ident):
        with self.repo().db() as db:db.execute('UPDATE devices SET last_seen=0 WHERE id=?',(ident,))

    def publish(self,ids,item):
        plan=self.repo().preflight({'targets':ids,'package_id':item['id'],'action':'install','delivery':'idle'},'admin',self.revision())
        self.assertTrue(all(r['ready'] for r in plan['rows']),plan)
        return self.repo().execute(plan['id'],'admin',self.revision(),True)

    def jobs(self):
        with self.repo().db() as db:return [json.loads(r[0]) for r in db.execute("SELECT body FROM software_jobs WHERE json_extract(body,'$.delivery')='idle' ORDER BY rowid")]

    def worker(self,installed=False,busy=False,wrong_identity=False,disconnect=False,fail=False,preserved=False):
        state={'installed':installed,'install_calls':0,'released':0,'calls':[]}
        def runner(args,**kwargs):
            state['calls'].append(args)
            if 'connect' in args:return 'connected'
            if 'devices' in args:
                return '' if disconnect else '\n'.join('192.168.20.'+str(n)+':5555\tdevice' for n in range(10,170))
            if 'android_id' in args:
                serial=args[args.index('-s')+1];n=int(serial.split('.')[-1].split(':')[0])-10
                return 'wrong-identity' if wrong_identity else 'fixture-'+str(n)
            if 'getprop' in args:return '28'
            if 'dumpsys' in args:return 'versionCode=2\n' if state['installed'] or preserved else ''
            if 'path' in args:
                if preserved and not state['installed']:
                    if 1 not in kwargs.get('allowed',(0,)):raise SoftwareError('安装工具失败（退出码 1）')
                    return ''
                return 'package:/data/app/guest/base.apk\n'
            if 'pull' in args:Path(args[-1]).write_bytes(b'candidate');return 'pulled'
            if 'broadcast' in args:
                if 'release' in args:state['released']+=1;return 'HOTEL_RELEASED'
                nonce=args[args.index('nonce')+1]
                return 'HOTEL_BUSY' if busy else 'HOTEL_READY:'+nonce
            if 'install' in args:
                state['install_calls']+=1
                if fail:raise SoftwareError('INSTALL_FAILED_UPDATE_INCOMPATIBLE')
                state['installed']=True;return 'Success\n'
            if 'disconnect' in args:return 'disconnected'
            raise AssertionError(args)
        agent=Agent(self.data,self.root/'agent',runner=runner)
        return agent,state

    def run_worker(self,agent):
        with patch('fleet_agent.tool',return_value='adb'),patch('software_agent.inspect',side_effect=lambda p,n:self._metadata):
            return agent.once()


class PublicationTests(PublicationFixture,unittest.TestCase):
    def test_offline_publication_survives_restart_and_installs_after_heartbeat_once(self):
        ids,item=self.rooms();self._metadata=item;self.offline(ids[0]);self.publish(ids,item)
        self.assertEqual(self.jobs()[0]['status'],'waiting_online')
        Agent(self.data,self.root/'restarted').recover()
        self.seen(ids[0]);self.assertEqual(self.jobs()[0]['status'],'queued')
        agent,state=self.worker();self.assertTrue(self.run_worker(agent))
        self.assertEqual(self.jobs()[0]['status'],'completed');self.assertEqual(state['install_calls'],1);self.assertEqual(state['released'],1)
        self.seen(ids[0]);self.assertFalse(self.run_worker(agent));self.assertEqual(state['install_calls'],1)

    def test_retained_metadata_without_apk_is_installed_then_bytes_are_verified(self):
        ids,item=self.rooms();self._metadata=item;self.publish(ids,item)
        agent,state=self.worker(preserved=True);self.run_worker(agent)
        self.assertEqual(self.jobs()[0]['status'],'completed')
        self.assertEqual(state['install_calls'],1);self.assertEqual(state['released'],1)

    def test_busy_pause_ad_external_or_recent_input_waits(self):
        ids,item=self.rooms()
        for changes in ({'home_idle':False},{'idle_ms':100},{'screen_awake':False}):
            self.seen(ids[0],**changes);result=self.publish(ids,item)
            self.assertEqual(self.jobs()[-1]['status'],'waiting_idle')
            self.repo().cancel_batch(result['batch']['id'],'admin',self.revision())
        self.assertFalse(any(j['status']=='queued' for j in self.jobs()))

    def test_device_rechecks_idle_and_closes_race_without_installing(self):
        ids,item=self.rooms();self._metadata=item;self.publish(ids,item)
        agent,state=self.worker(busy=True);self.run_worker(agent)
        self.assertEqual(self.jobs()[0]['status'],'waiting_idle');self.assertEqual(state['install_calls'],0)
        self.assertGreater(self.jobs()[0]['next_attempt_at'],now())

    def test_offline_room_does_not_block_other_room(self):
        ids,item=self.rooms(2);self._metadata=item;self.offline(ids[0]);self.publish(ids,item)
        self.assertEqual([j['status'] for j in self.jobs()],['waiting_online','queued'])
        agent,state=self.worker();self.run_worker(agent)
        self.assertEqual([j['status'] for j in self.jobs()],['waiting_online','completed'])

    def test_interrupted_install_checks_actual_bytes_without_reinstall(self):
        ids,item=self.rooms();self._metadata=item;self.publish(ids,item)
        with self.repo().db() as db:
            j=self.jobs()[0];j['status']='running';self.repo().write_job(db,j)
        agent,state=self.worker(installed=True);agent.recover()
        self.assertEqual(self.jobs()[0]['status'],'waiting_online')
        self.seen(ids[0]);self.run_worker(agent)
        self.assertEqual(self.jobs()[0]['status'],'completed');self.assertEqual(state['install_calls'],0)
        self.assertEqual(state['released'],1)

    def test_disconnect_defers_and_does_not_require_guest_authorization(self):
        ids,item=self.rooms();self._metadata=item;self.publish(ids,item)
        agent,state=self.worker(disconnect=True);self.run_worker(agent)
        self.assertEqual(self.jobs()[0]['status'],'waiting_online');self.assertEqual(state['install_calls'],0)

    def test_withdrawn_offline_publication_never_runs(self):
        ids,item=self.rooms();self._metadata=item;self.offline(ids[0]);r=self.publish(ids,item)
        self.repo().cancel_batch(r['batch']['id'],'admin',self.revision());self.seen(ids[0])
        agent,state=self.worker();self.assertFalse(self.run_worker(agent));self.assertEqual(self.jobs()[0]['status'],'cancelled')

    def test_new_publication_supersedes_pending_same_app(self):
        ids,item=self.rooms();self.offline(ids[0]);self.publish(ids,item);self.publish(ids,item)
        self.assertEqual([j['status'] for j in self.jobs()],['superseded','waiting_online'])

    def test_room_or_credential_change_blocks_pending_target(self):
        ids,item=self.rooms();self.offline(ids[0]);self.publish(ids,item)
        with self.repo().db() as db:db.execute('UPDATE devices SET token=? WHERE id=?',('new-token',ids[0]))
        self.seen(ids[0]);self.assertEqual(self.jobs()[0]['status'],'failed')

    def test_new_ip_uses_authenticated_address_and_rechecks_physical_identity(self):
        ids,item=self.rooms();self.offline(ids[0]);self.publish(ids,item)
        with self.repo().db() as db:db.execute('UPDATE devices SET ip=? WHERE id=?',('192.168.20.21',ids[0]))
        self.seen(ids[0]);self.assertEqual(self.jobs()[0]['endpoint']['address'],'192.168.20.21')
        self._metadata=item;agent,state=self.worker(wrong_identity=True);self.run_worker(agent)
        self.assertEqual(self.jobs()[0]['status'],'failed');self.assertEqual(state['install_calls'],0)

    def test_install_failure_releases_idle_lock_and_keeps_other_rooms(self):
        ids,item=self.rooms(2);self._metadata=item;self.publish(ids,item)
        agent,state=self.worker(fail=True);self.run_worker(agent)
        self.assertEqual(sorted(j['status'] for j in self.jobs()),['failed','queued']);self.assertEqual(state['released'],1)

    def test_power_loss_after_waiting_idle_is_displayed_as_waiting_online(self):
        ids,item=self.rooms();self.seen(ids[0],home_idle=False);self.publish(ids,item)
        self.assertEqual(self.jobs()[0]['status'],'waiting_idle')
        self.offline(ids[0])
        self.assertEqual(self.repo().state()['batches'][0]['jobs'][0]['status'],'waiting_online')

    def test_individual_task_cancel_withdraws_waiting_job_without_replaying(self):
        ids,item=self.rooms();self.offline(ids[0]);self.publish(ids,item)
        self.repo().control(self.jobs()[0]['id'],'cancel','admin',self.revision())
        self.seen(ids[0]);self.assertEqual(self.jobs()[0]['status'],'cancelled')

    def test_withdrawal_is_not_blocked_by_other_rooms_progress(self):
        ids,item=self.rooms();self.offline(ids[0]);result=self.publish(ids,item)
        self.repo().cancel_batch(result['batch']['id'],'admin',0)
        self.assertEqual(self.jobs()[0]['status'],'cancelled')

    def test_old_client_or_worker_cannot_schedule_idle_install(self):
        ids,item=self.rooms()
        with self.repo().db() as db:db.execute('UPDATE fleet_status SET body=? WHERE id=?',(encoded({'protocol':1,'at':now(),'sdk':28,'keep_adb':True}),ids[0]))
        plan=self.repo().preflight({'targets':ids,'package_id':item['id'],'action':'install','delivery':'idle'},'admin',self.revision())
        self.assertFalse(plan['rows'][0]['ready'])
        self.seen(ids[0]);self.offline(ids[0]);self.publish(ids,item)
        with patch.object(FleetRepository,'executor_ready',return_value=False):self.seen(ids[0])
        self.assertEqual(self.jobs()[0]['status'],'waiting_online')

if __name__=='__main__':unittest.main()
