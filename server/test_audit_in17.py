"""Behavior regressions from the independent IN16-DOC1 review; no live devices/services."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from downloads_engines import QBittorrent
from downloads_net import NetError, bencode, torrent_info
from management import password_hash
from pin_credentials import public_verifier
from test_media_add import MediaAddFixture, movie, GB
import test_management as fixture
from test_fleet_publication import PublicationFixture


class DownloadAudit(MediaAddFixture, unittest.TestCase):
    def prepare_movie(self, suffix='a1'):
        self.apps.search['Inception']=[movie(27205,'盗梦空间',2010,'Inception')]
        self.add(link='magnet:?xt=urn:btih:'+suffix*20+'&dn=Inception.2010')
        return self.task()

    def test_repeated_list_removal_never_deletes_kept_files(self):
        task=self.prepare_movie()
        self.finish(None,{'Inception.mkv':GB})
        self.store.media_add_step();self.store.media_add_step()
        self.assertEqual(self.task()['stage'],'imported')
        self.store.media_add_action({'id':task['id'],'action':'remove'},'admin')
        self.store.media_add_action({'id':task['id'],'action':'remove'},'admin')
        self.assertEqual(self.qb.deleted,[])

    def test_multiple_movie_versions_require_selection_and_import_only_that_file(self):
        task=self.prepare_movie()
        self.finish(None,{'Inception.1080p.mkv':GB,'making-of.avi':2*GB})
        self.store.media_add_step()
        task=self.task();self.assertEqual(task['stage'],'needs_file')
        self.assertFalse(self.apps.posted('radarr','/api/v3/command'))
        self.store.media_add_action({'id':task['id'],'action':'select_files','file':'Inception.1080p.mkv'},'admin')
        self.store.media_add_step()
        commands=self.apps.posted('radarr','/api/v3/command')
        self.assertEqual(len(commands),1)
        self.assertEqual(Path(commands[0]['files'][0]['path']).name,'Inception.1080p.mkv')

    def test_finished_videos_can_import_while_metadata_has_no_peers(self):
        task=self.prepare_movie()
        self.finish(None,{'Inception.mkv':GB})
        self.qb.torrents['a1'*20]['state']='downloading'
        self.qb.files['a1'*20]=[{'name':'Inception.mkv','size':GB,'progress':1},{'name':'_meta.xml','size':100,'progress':0}]
        self.store.media_add_step()
        self.assertEqual(self.task()['stage'],'importing')

    def test_missing_files_are_never_permanently_classified_as_ordinary_file(self):
        self.prepare_movie();self.qb.complete('a1'*20)
        self.store.media_add_step()
        self.assertNotEqual(self.task()['stage'],'downloaded')
        self.assertIn('数据盘',self.task()['error'])

    def test_retry_retains_explicit_ordinary_file_choice(self):
        self.add(link='magnet:?xt=urn:btih:'+'b2'*20+'&dn=Home.2015.mkv',kind='file')
        task=self.store._media_add_get(self.task()['id'])
        task['stage']='failed'
        with self.store.db() as db:self.store._media_add_save(db,task)
        self.store.media_add_action({'id':task['id'],'action':'retry'},'admin')
        self.assertEqual(self.task()['stage'],'file')
        self.assertFalse(self.apps.posted('radarr','/api/v3/command'))

    def test_same_movie_two_downloads_have_one_native_import_in_flight(self):
        self.prepare_movie('a1');self.finish(None,{'Inception.mkv':GB})
        self.prepare_movie('b2');self.finish(None,{'Inception.2160p.mkv':2*GB})
        self.store.media_add_step()
        self.assertEqual(len(self.apps.posted('radarr','/api/v3/command')),1)

    def test_no_downloader_task_does_not_claim_files_were_deleted(self):
        task=self.prepare_movie();self.qb.torrents.clear()
        reply=self.store.media_add_action({'id':task['id'],'action':'remove'},'admin')
        self.assertIn('未删除',reply['message']);self.assertEqual(self.qb.deleted,[])

    def test_low_space_rejects_before_creating_a_torrent(self):
        with patch('shutil.disk_usage',return_value=type('Usage',(),{'free':1})()):
            with self.assertRaisesRegex(Exception,'空间不足'):self.prepare_movie()
        self.assertFalse(self.qb.added)

    def test_no_peer_or_metadata_wait_becomes_attention_and_progress_clears_it(self):
        task=self.store._media_add_get(self.prepare_movie()['id']);task['progress_at']=task['created_at']-3*3600000;task['last_downloaded']=1
        with self.store.db() as db:self.store._media_add_save(db,task)
        self.assertTrue(any('没有进展' in i['title'] for i in self.store.media_add_issues()))
        self.qb.torrents['a1'*20]['downloaded']=2
        self.store._media_add_advance(self.store._media_add_config(),self.qb,self.store._media_add_get(task['id']))
        self.assertFalse(any('没有进展' in i['title'] for i in self.store.media_add_issues()))

    def test_native_low_space_guard_pauses_only_our_downloads_and_resumes_only_its_own(self):
        config=json.loads((self.data/'media-automation.json').read_text())
        root=Path(config['storage_root']);root.mkdir()
        (root/'.hotel-media-install.json').write_text('{}');config['require_storage_marker']=True
        rows=[{'hash':str(i)*40,'state':state,'save_path':str(root),'category':category}
              for i,state,category in [(1,'downloading','radarr'),(2,'downloading','personal'),(3,'pausedDL','sonarr')]]
        calls=[]
        def call(route,body=None):
            if route=='/api/v2/torrents/info':return json.dumps(rows).encode()
            calls.append((route,body));return b''
        self.qb.api=(2,8);self.qb._call=call
        with patch('shutil.disk_usage',return_value=type('Usage',(),{'free':1})()):self.store._native_storage_guard(config)
        self.assertEqual(calls,[('/api/v2/torrents/pause',{'hashes':'1'*40})])
        rows[0]['state']='pausedDL';calls.clear()
        with patch('shutil.disk_usage',return_value=type('Usage',(),{'free':1000*GB})()):self.store._native_storage_guard(config)
        self.assertEqual(calls,[('/api/v2/torrents/resume',{'hashes':'1'*40})])
        rows[0]['state']='downloading';(root/'.hotel-media-install.json').unlink();calls.clear()
        self.store._native_storage_guard(config)
        self.assertEqual(calls,[('/api/v2/torrents/pause',{'hashes':'1'*40})])


class AuthenticationAudit(unittest.TestCase):
    setUp=fixture.ManagementE2E.setUp
    tearDown=fixture.ManagementE2E.tearDown
    request=fixture.ManagementE2E.request
    state=fixture.ManagementE2E.state
    mutate=fixture.ManagementE2E.mutate

    def viewer(self):
        with self.store.db() as db:db.execute('INSERT INTO users VALUES(?,?,?,1)',('reader',password_hash('reader-password'),'viewer'))
        status,headers,result=self.request('/api/admin/login',{'username':'reader','password':'reader-password'})
        self.assertEqual(status,200)
        return {'Cookie':headers['Set-Cookie'].split(';')[0],'X-CSRF-Token':result['user']['csrf']}

    def test_viewer_logout_revokes_session(self):
        headers=self.viewer()
        self.assertEqual(self.request('/api/admin/logout',{},headers=headers)[0],200)
        self.assertEqual(self.request('/api/admin/state',headers=headers)[0],401)

    def test_another_account_success_does_not_clear_admin_failure_bucket(self):
        headers=self.viewer()
        from admin_http import AdminMixin
        AdminMixin.login_attempts.clear()
        try:
            for _ in range(7):self.request('/api/admin/login',{'username':'admin','password':'wrong'})
            self.request('/api/admin/login',{'username':'reader','password':'reader-password'})
            self.request('/api/admin/login',{'username':'admin','password':'wrong'})
            self.assertEqual(self.request('/api/admin/login',{'username':'admin','password':'wrong'})[0],429)
        finally:AdminMixin.login_attempts.clear()

    def test_only_published_media_can_be_fetched_and_unpublishing_revokes_ranges(self):
        self.assertEqual(self.request('/media/clip.mp4',tv=True,raw=True)[0],200)
        (self.media/'private.mp4').write_bytes(b'PRIVATE')
        with self.store.db() as db:
            row=db.execute("SELECT body FROM media WHERE id='existing'").fetchone();body=json.loads(row[0]);body['description']='/media/private.mp4'
            db.execute("UPDATE media SET body=? WHERE id='existing'",(json.dumps(body),))
        self.store.__dict__.pop('_tv_route_cache',None)
        self.assertEqual(self.request('/media/private.mp4',tv=True,raw=True)[0],404)
        self.mutate('/media/batch',{'ids':['existing'],'action':'archive'})
        self.assertEqual(self.request('/media/clip.mp4',headers={'Range':'bytes=0-3'},tv=True,raw=True)[0],404)

    def test_device_tokens_are_one_way_and_a_digest_is_not_a_bearer(self):
        with self.store.db() as db:token=db.execute("SELECT token FROM devices WHERE id='room-101'").fetchone()[0]
        self.assertTrue(token.startswith('sha256:'))
        self.assertIsNone(self.store.device(token))
        self.assertIsNotNone(self.store.device('tv-test-token'))
        backup=self.store.backup('admin')
        self.assertNotIn(b'tv-test-token',backup.read_bytes())

    def test_fast_pin_digest_is_not_published(self):
        pin={'revision':'a'*32,'sha256':hashlib.sha256(b'123456').hexdigest()}
        (self.data/'maintenance-pin.json').write_text(json.dumps(pin))
        for protocol in ('1','2'):
            status,_,catalog=self.request('/api/v1/catalog',tv=True,headers={'X-TV-Pin-Protocol':protocol})
            self.assertEqual(status,200)
            self.assertNotIn(pin['sha256'],json.dumps(catalog))
            if protocol=='2':self.assertEqual(catalog['maintenance']['iterations'],210000)

    def test_unknown_creative_and_client_report_exclusion_cannot_pollute_reports(self):
        self.store.record_events({'id':'room-101','group':'全部客房'},[{'id':'forged','type':'ad_completed','creative_id':'unknown','exclude_from_ad_reports':True}])
        with self.store.db() as db:self.assertIsNone(db.execute("SELECT body FROM events WHERE id='room-101:forged'").fetchone())

    def test_event_rate_limit_does_not_ack_or_drop_new_events(self):
        device={'id':'room-101','group':'全部客房'}
        events=[{'id':str(i),'type':'content_started'} for i in range(300)]
        self.store.record_events(device,events)
        # Replaying accepted IDs is always safe, including at the limit.
        self.store.record_events(device,events[:5])
        with self.assertRaises(Exception) as caught:self.store.record_events(device,[{'id':'later','type':'content_ended'}])
        self.assertEqual(caught.exception.status,429)
        with self.store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],300)

    def test_series_report_aggregates_both_legacy_and_new_episode_events(self):
        series={'id':'series-a','title':'剧集甲','kind':'series','episodes':[{'id':'ep-a'},{'id':'ep-b'}]}
        with self.store.db() as db:
            db.execute('INSERT INTO media VALUES(?,?,?,?,?)',('series-a','main',json.dumps(series),0,1))
            db.execute('INSERT INTO events VALUES(?,?,?,?)',('room-101:old','room-101',__import__('time').time_ns()//1000000,json.dumps({'id':'old','type':'content_started','content_id':'ep-a','client_session_id':'old-session'})))
        self.store.record_events({'id':'room-101'},[{'id':'new','type':'content_started','content_id':'ep-b','client_session_id':'new-session'}])
        row=next(r for r in self.store.reports()['content'] if r['id']=='series-a')
        self.assertEqual((row['title'],row['sessions']),('剧集甲',2))


class UnitAudit(unittest.TestCase):
    def test_incompatible_maintenance_history_does_not_leave_rollback_service_stopped(self):
        from software_agent import Agent
        tables=('software_packages','software_jobs','software_inventory','software_targets','software_meta')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);data=root/'data';data.mkdir();releases=root/'releases';old=releases/'old';new=releases/'new';old.mkdir(parents=True);new.mkdir()
            current=root/'current';current.symlink_to(new);backup=root/'before.sqlite3'
            with sqlite3.connect(data/'events.sqlite3') as db:
                for table in tables:db.execute('CREATE TABLE '+table+' (a,b)');db.execute('INSERT INTO '+table+' VALUES(1,2)')
            with sqlite3.connect(backup) as db:
                for table in tables:db.execute('CREATE TABLE '+table+' (a)')
            calls=[];agent=Agent(data,root/'state',releases,current,runner=lambda command,**kw:calls.append(command))
            agent.wait_health=lambda:None
            job={'previous_release':str(old),'database_backup':str(backup),'backup_ready':True}
            agent.restore_system(job)
            self.assertEqual(current.resolve(),old.resolve());self.assertIn(['systemctl','start','hotel-tv'],calls)
            self.assertTrue(job['warnings'])

    def test_missing_rollback_backup_still_switches_back_and_attempts_start(self):
        from software_agent import Agent
        from software_packages import SoftwareError
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);data=root/'data';data.mkdir();releases=root/'releases';old=releases/'old';new=releases/'new';old.mkdir(parents=True);new.mkdir()
            current=root/'current';current.symlink_to(new);calls=[]
            agent=Agent(data,root/'state',releases,current,runner=lambda command,**kw:calls.append(command))
            with self.assertRaises(SoftwareError):agent.restore_system({'previous_release':str(old),'database_backup':str(root/'absent.sqlite3'),'backup_ready':True})
            self.assertEqual(current.resolve(),old.resolve());self.assertIn(['systemctl','start','hotel-tv'],calls)

    def test_infohash_uses_top_level_info_not_comment_text(self):
        info={'name':'Movie.mkv','length':10,'piece length':16384,'pieces':b'\0'*20}
        for comment in ('see 4:info','information and annotations'):
            self.assertEqual(torrent_info(bencode({'comment':comment,'info':info}))['infohash'],hashlib.sha1(bencode(info)).hexdigest())

    def test_qbittorrent_missing_files_override_full_progress(self):
        qb=QBittorrent({'url':'http://127.0.0.1:1'},'fixture');qb.sid='fixture'
        qb._info=lambda ref:{'state':'missingFiles','progress':1,'size':10,'tags':'hoteltv'}
        self.assertEqual(qb.status('a'*40)['state'],'error')

    def test_qbittorrent_rejection_is_not_a_successful_submission(self):
        qb=QBittorrent({'url':'http://127.0.0.1:1'},'fixture');qb.sid='fixture';qb._info=lambda ref:None;qb._has_category=lambda:True
        qb._call=lambda *a,**kw:b'Fails.'
        with self.assertRaises(NetError):qb.add('own-task',{'kind':'magnet','infohash':'a'*40,'uri':'magnet:?xt=urn:btih:'+'a'*40},'/tmp/fixture')

    def test_pin_verifier_is_salted_per_device_and_still_offline(self):
        pin={'revision':'b'*32,'sha256':hashlib.sha256(b'123456').hexdigest()}
        one=public_verifier(pin,'room-a');two=public_verifier(pin,'room-b')
        self.assertNotEqual(one['salt'],two['salt']);self.assertNotEqual(one['verifier'],two['verifier'])
        self.assertEqual(one['verifier'],hashlib.pbkdf2_hmac('sha1',pin['sha256'].encode(),bytes.fromhex(one['salt']),one['iterations'],32).hex())


class FleetAudit(PublicationFixture,unittest.TestCase):
    def test_real_script_entrypoint_preserves_waiting_idle(self):
        ids,item=self.rooms();self.publish(ids,item)
        adb=self.root/'fake-adb'
        adb.write_text('#!'+sys.executable+'\n'+'''import sys
a=sys.argv[1:]
if 'connect' in a: print('connected')
elif 'devices' in a: print('192.168.20.10:5555\\tdevice')
elif 'android_id' in a: print('fixture-0')
elif 'getprop' in a: print('28')
elif 'dumpsys' in a: pass
elif 'broadcast' in a: print('HOTEL_BUSY')
elif 'disconnect' in a: pass
else: sys.exit(3)
''');adb.chmod(0o755)
        code="""import runpy,sys,json,software_packages,software_agent
metadata=json.loads(sys.argv[1])
software_packages.inspect=lambda p,n:metadata
software_agent.inspect=software_packages.inspect
# Linux/root are provided by systemd in production; only those platform guards
# are replaced on macOS. The actual __main__ and exception handlers run unchanged.
import platform,os
platform.system=lambda:'Linux'
os.geteuid=lambda:0
os.seteuid=lambda uid:None
os.setegid=lambda gid:None
sys.argv=['software_agent.py','--data',sys.argv[2],'--state',sys.argv[3],'--once']
runpy.run_path('software_agent.py',run_name='__main__')
"""
        result=subprocess.run([sys.executable,'-c',code,json.dumps(item),str(self.data),str(self.root/'script-agent')],cwd=Path(__file__).parent,env={**os.environ,'HOTEL_TV_ADB':str(adb)},capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.jobs()[0]['status'],'waiting_idle',self.jobs()[0])

    def test_single_target_cannot_install_at_a_reused_ip(self):
        ids,item=self.rooms();self._metadata=item
        queued=self.repo().queue(item['id'],ids,'admin',self.revision(),'single-audit-request')
        agent,state=self.worker(wrong_identity=True);self.run_worker(agent)
        self.assertEqual(self.jobs()[0]['status'],'failed');self.assertEqual(state['install_calls'],0)

    def test_reprobe_retains_pending_publication(self):
        ids,item=self.rooms();self.seen(ids[0],home_idle=False);self.publish(ids,item)
        self.repo().probe(ids[0],'admin',self.revision());self.repo().reconcile(ids[0])
        publication=next(j for j in self.jobs() if j.get('delivery')=='idle')
        self.assertNotEqual(publication['status'],'failed')

