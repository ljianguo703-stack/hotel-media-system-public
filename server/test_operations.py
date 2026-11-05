import json
import unittest
from unittest.mock import patch, MagicMock
import test_management as fixture
from management import Store,password_hash

class OperationsE2E(unittest.TestCase):
    setUp=fixture.ManagementE2E.setUp
    tearDown=fixture.ManagementE2E.tearDown
    request=fixture.ManagementE2E.request
    state=fixture.ManagementE2E.state
    mutate=fixture.ManagementE2E.mutate
    wait_scan=fixture.ManagementE2E.wait_scan
    campaign=fixture.ManagementE2E.campaign

    def op(self,kind,action,**body):
        return self.mutate('/operations',dict(kind=kind,action=action,**body))

    def test_health_checks_lan_bound_nas_services_after_loopback(self):
        local='192.168.4.185'
        def connect(address,timeout):
            if address==('127.0.0.1',8096) or address in [(local,8095),(local,5001)]:
                return MagicMock()
            raise ConnectionRefusedError()
        with patch('operations_system.socket.create_connection',side_effect=connect) as probe:
            result=self.store.server_health(local)
        self.assertTrue(all(s['reachable'] for s in result['services'] if s['port']!=9090))
        self.assertFalse(next(s for s in result['services'] if s['port']==9090)['reachable'])
        self.assertIn(((local,8095),),[c.args for c in probe.call_args_list])
        self.assertIn(((local,5001),),[c.args for c in probe.call_args_list])

    def test_health_does_not_mark_closed_services_as_reachable(self):
        with patch('operations_system.socket.create_connection',side_effect=ConnectionRefusedError):
            result=self.store.server_health('192.168.4.185')
        self.assertTrue(all(not s['reachable'] for s in result['services']))

    def test_health_target_comes_from_socket_not_untrusted_host_header(self):
        with patch.object(self.store,'server_health',return_value={'services':[]}) as health:
            status,_,_=self.request('/api/admin/operations?section=health',auth=True,headers={'Host':'192.0.2.123:8095'})
        self.assertEqual(status,200)
        health.assert_called_once_with('127.0.0.1')

    def test_entity_lifecycle_duplicate_name_and_restart(self):
        item={'id':'merchant-1','name':'测试商户','contact':'联系人','phone':'123','notes':'备注'}
        self.assertEqual(self.op('advertiser','save',item=item)[0],200)
        self.assertEqual(self.op('advertiser','save',item={**item,'id':'merchant-2'})[0],409)
        for action,archived in [('archive',True),('restore',False)]:
            self.assertEqual(self.op('advertiser',action,ids=['merchant-1'])[2]['ok'],['merchant-1'])
            record=next(i for i in self.state()['advertisers'] if i['id']=='merchant-1')
            self.assertEqual(record['_archived'],archived);self.assertFalse(record['enabled'])
        reopened=Store(self.data,self.media,self.config,[self.media,self.external])
        self.assertEqual(reopened.state()['advertisers'],self.state()['advertisers'])
        self.assertEqual(reopened.state()['revision'],self.state()['revision'])

    def test_group_rename_links_devices_and_blocks_archive_while_used(self):
        group=next(g for g in self.state()['device_groups'] if g['name']=='A组')
        before=self.state()['revision']
        self.assertEqual(self.op('group','save',item={**group,'name':'B组'})[0],200)
        state=self.state();self.assertEqual(state['devices'][0]['group'],'B组');self.assertEqual(state['revision'],before+1)
        result=self.op('group','archive',ids=[group['id']])[2]
        self.assertFalse(result['ok']);self.assertIn('引用',result['failed'][0]['reason'])
        self.assertEqual(self.state()['revision'],before+1)
        self.assertFalse(self.op('source','disable',ids=['main'])[2]['ok'])

    def test_expiry_purges_index_preserves_file_and_blocks_scan_until_allowed(self):
        with patch('management.now_ms',return_value=100000):
            self.assertEqual(self.mutate('/media/batch',{'ids':['existing'],'action':'archive'})[2]['ok'],['existing'])
        row=self.request('/api/admin/operations?section=trash',auth=True)[2]['rows'][0]
        self.assertEqual(row['expires_at'],100000+30*86400000)
        self.assertEqual(self.op('media','purge',ids=['existing'])[0],400)
        with patch('management.now_ms',return_value=200000):self.assertFalse(self.store.cleanup_expired()['ok'])
        with patch('management.now_ms',return_value=row['expires_at']+1):result=self.store.cleanup_expired()
        self.assertEqual(result['ok'],['existing']);self.assertFalse(self.state()['media'])
        self.assertEqual((self.media/'clip.mp4').read_bytes(),b'0123456789')
        before=self.state()['revision'];job=self.wait_scan('main')
        self.assertEqual((job['status'],job['added'],job['skipped']),('completed',0,1));self.assertEqual(self.state()['revision'],before)
        reopened=Store(self.data,self.media,self.config,[self.media,self.external])
        with reopened.db() as db:self.assertEqual(len(reopened.objects(db,'media_tombstone')),1)
        self.assertEqual(self.op('media','allow_reimport',id='existing')[0],200)
        self.assertEqual(self.wait_scan('main')['status'],'completed');self.assertEqual(len(self.state()['media']),1)

    def test_policy_permissions_and_partial_success(self):
        self.assertEqual(self.op(None,'policy',trash_days=7,cleanup_enabled=True)[0],200)
        self.assertEqual(self.state()['management_policy']['trash_days'],7)
        for days in (0,366,'30'):self.assertEqual(self.op(None,'policy',trash_days=days,cleanup_enabled=True)[0],400)
        item={'id':'a1','name':'广告主'};self.op('advertiser','save',item=item)
        before=self.state()['revision'];result=self.op('advertiser','archive',ids=['a1','missing'])[2]
        self.assertEqual((result['ok'],len(result['failed'])),(['a1'],1));self.assertEqual(self.state()['revision'],before+1)
        for role in ('operator','viewer'):
            with self.store.db() as db:db.execute('INSERT INTO users VALUES(?,?,?,1)',('ops-'+role,password_hash('operations-test'),role))
            _,headers,login=self.request('/api/admin/login',{'username':'ops-'+role,'password':'operations-test'})
            h={'Cookie':headers['Set-Cookie'].split(';')[0],'X-CSRF-Token':login['user']['csrf']}
            for kind,action in [('group','save'),('media','purge'),(None,'policy')]:
                self.assertEqual(self.request('/api/admin/operations',{'kind':kind,'action':action,'revision':self.state()['revision']},headers=h)[0],403)
            expected=403 if role=='viewer' else 200
            self.assertEqual(self.request('/api/admin/operations',{'kind':'advertiser','action':'save','item':{'id':'role-'+role,'name':role},'revision':self.state()['revision']},headers=h)[0],expected)

    def test_asset_references_and_category_guards(self):
        (self.media/'asset.mp4').write_bytes(b'ad-asset')
        self.assertEqual(self.op('asset','save',item={'id':'asset-1','name':'欢迎片','path':'/media/asset.mp4'})[0],200)
        self.assertEqual(self.mutate('/campaigns/test-ad',self.campaign(tier=1,format='视频',path='/media/asset.mp4',placements=['preroll']))[0],200)
        result=self.op('asset','archive',ids=['asset-1'])[2]
        self.assertIn('引用',result['failed'][0]['reason']);self.assertTrue((self.media/'asset.mp4').is_file())
        self.assertEqual(self.op('campaign','archive',ids=['test-ad'])[2]['ok'],['test-ad'])
        self.assertFalse(self.store.ad_manifest({'id':'room-101','group':'A组'})['ads'])
        self.assertEqual(self.op('campaign','restore',ids=['test-ad'])[2]['ok'],['test-ad'])
        self.assertEqual(next(c for c in self.state()['campaigns'] if c['id']=='test-ad')['status'],'草稿')

    def test_campaign_batch_publish_copy_pause_and_failed_asset(self):
        self.assertEqual(self.mutate('/campaigns/first',self.campaign(tier=1,format='视频',path='/media/clip.mp4',placements=['preroll']))[0],200)
        self.assertEqual(self.mutate('/campaigns/second',self.campaign(tier=1,format='视频',status='草稿',path='/media/missing.mp4',placements=['preroll']))[0],200)
        before=self.state()['revision']
        result=self.op('campaign','campaign_batch',operation='publish',ids=['first','second'])[2]
        self.assertEqual(result['ok'],['first']);self.assertEqual(result['failed'][0]['id'],'second');self.assertEqual(self.state()['revision'],before+1)
        copied=self.op('campaign','campaign_batch',operation='copy',ids=['first'])[2]
        clone=next(c for c in self.state()['campaigns'] if c['id']==copied['created'][0]);self.assertEqual(clone['status'],'草稿');self.assertIn('副本',clone['name'])
        self.assertEqual(self.op('campaign','campaign_batch',operation='pause',ids=['first'])[2]['ok'],['first'])
        self.assertFalse(self.store.ad_manifest({'id':'room-101','group':'A组'})['ads'])
        self.assertEqual(self.op('campaign','campaign_batch',operation='archive',ids=['first'])[2]['ok'],['first'])
        self.assertEqual(self.mutate('/campaigns/first',{**clone,'status':'投放中'})[0],400)

    def test_schedule_preview_overnight_conflicts_matches_tv_weekday(self):
        self.assertEqual(self.mutate('/campaigns/night',self.campaign(start='22:00',end='02:00',days=[2],priority=80))[0],200)
        self.assertEqual(self.mutate('/campaigns/day',self.campaign(tier=2,days=[1,2,3,4,5,6,7],priority=50))[0],200)
        code,_,preview=self.request('/api/admin/operations?section=schedule&at=2026-10-28T01%3A00',auth=True)
        self.assertEqual(code,200);self.assertEqual({c['id'] for c in preview['eligible']},{'night','day'})
        self.assertTrue(preview['conflicts']);self.assertTrue(all(c['preferred']=='真实广告' for c in preview['conflicts']))
        at_noon=self.request('/api/admin/operations?section=schedule&at=2026-10-28T12%3A00',auth=True)[2]
        self.assertEqual([c['id'] for c in at_noon['eligible']],['day']);self.assertTrue(next(c for c in at_noon['excluded'] if c['id']=='night')['reasons'])

    def test_asset_discovery_is_idempotent_and_honors_cleared_markers(self):
        folder=self.media/'广告';folder.mkdir();file=folder/'欢迎.mp4';file.write_bytes(b'asset')
        result=self.op('asset','discover_assets')[2];self.assertEqual(result['added'],1)
        asset=self.state()['assets'][0];self.assertEqual(asset['name'],'欢迎.mp4')
        revision=self.state()['revision'];self.assertEqual(self.op('asset','discover_assets')[2]['added'],0);self.assertEqual(self.state()['revision'],revision)
        self.op('asset','archive',ids=[asset['id']]);self.op('asset','purge',ids=[asset['id']],confirm=True)
        self.assertEqual(self.op('asset','discover_assets')[2]['added'],0);self.assertTrue(file.is_file())

    def test_csv_import_partial_update_and_one_time_credentials(self):
        raw='id,room,group,enabled,model,version\nroom-202,202,二楼,true,Android,1\nroom-202,203,二楼,true,,\nbad space,204,二楼,true,,\nroom-205,205,二楼,false,,\n'
        before=self.state()['revision'];result=self.op('device','devices_import',csv=raw)[2]
        self.assertEqual(result['ok'],['room-202','room-205']);self.assertEqual(len(result['failed']),2);self.assertEqual(len(result['credentials']),2);self.assertEqual(self.state()['revision'],before+1)
        token=result['credentials'][0]['token'];device=self.store.device(token);self.assertEqual(device['room'],'202')
        update='id,room,group,enabled,model,version\nroom-202,220,二楼,true,new,2\n'
        self.assertFalse(self.op('device','devices_import',csv=update)[2]['ok'])
        changed=self.op('device','devices_import',csv=update,overwrite=True)[2];self.assertEqual(changed['ok'],['room-202']);self.assertFalse(changed['credentials'])
        self.assertEqual(self.store.device(token)['room'],'220');self.assertNotIn(token,json.dumps(self.state()))
        self.assertIn('room-202',self.request('/api/admin/operations?section=devices_csv',auth=True)[2]['csv'])

    def test_device_rotation_rejects_old_token_and_bulk_restore_stays_disabled(self):
        self.assertEqual(self.op('device','devices_batch',operation='rotate',ids=['room-101'])[0],400)
        result=self.op('device','devices_batch',operation='rotate',ids=['room-101'],confirm=True)[2]
        self.assertIsNone(self.store.device('tv-test-token'));self.assertIsNotNone(self.store.device(result['credentials'][0]['token']))
        self.assertEqual(self.op('device','devices_batch',operation='disable',ids=['room-101'])[2]['ok'],['room-101'])
        self.assertIsNone(self.store.device(result['credentials'][0]['token']))
        group=self.state()['device_groups'][0]
        self.assertEqual(self.op('device','devices_batch',operation='set_group',ids=['room-101'],value=group['name'])[2]['ok'],['room-101'])
        self.op('device','devices_batch',operation='archive',ids=['room-101'])
        self.assertEqual(self.mutate('/devices/room-101',{'room':'101','enabled':True})[0],400)
        self.op('device','restore',ids=['room-101']);device=next(d for d in self.state()['devices'] if d['id']=='room-101')
        self.assertFalse(device['enabled']);self.assertFalse(device['_archived'])
        details=self.request('/api/admin/operations?section=device&id=room-101',auth=True)[2]
        self.assertNotIn('token',json.dumps(details));self.assertEqual(details['event_count'],0)

    def test_management_dispatch_cannot_bypass_admin_with_fake_kind(self):
        with self.store.db() as db:db.execute('INSERT INTO users VALUES(?,?,?,1)',('limited',password_hash('operations-test'),'operator'))
        _,headers,login=self.request('/api/admin/login',{'username':'limited','password':'operations-test'})
        h={'Cookie':headers['Set-Cookie'].split(';')[0],'X-CSRF-Token':login['user']['csrf']}
        for action in ('devices_import','devices_batch','sync_control','retry_task','purge'):
            self.assertEqual(self.request('/api/admin/operations',{'kind':'campaign','action':action,'revision':self.state()['revision']},headers=h)[0],403)
        self.assertEqual(self.op('campaign','devices_import',csv='id,room\na,1')[0],400)

    def test_tasks_sync_settings_one_click_and_audit_search(self):
        self.assertEqual(self.op('system','sync_control',interval_seconds=60,paused=True)[0],200)
        info=self.request('/api/admin/operations?section=sync',auth=True)[2]
        self.assertEqual((info['interval_seconds'],info['paused'],info['configured']),(60,True,False));self.assertNotIn('token',info)
        self.assertEqual(self.op('system','sync_test')[2]['connected'],False)
        job=self.op('media','library_update')[2];finished=self.store.wait_task(job['id'],timeout=5)
        self.assertEqual(finished['status'],'completed');self.assertTrue(finished['steps'])
        tasks=self.request('/api/admin/operations?section=tasks&status=completed&limit=1',auth=True)[2]
        self.assertEqual(len(tasks['rows']),1);self.assertGreater(tasks['total'],0)
        self.assertEqual(self.op('system','retry_task',id=job['id'])[0],400)
        rows=self.request('/api/admin/operations?section=audit&q=CSV',auth=True)[2]
        self.assertIn('total',rows);self.assertEqual(self.request('/api/admin/operations?section=tasks&limit=9999',auth=True)[0],400)
        self.assertEqual(self.op('system','sync_connection',url='http://example.com:8096',token='test')[0],400)

    def test_release_preview_requires_confirmation_and_stale_revision_is_rejected(self):
        self.mutate('/campaigns/older',self.campaign());release=self.state()['releases'][0]['id']
        self.mutate('/campaigns/later',self.campaign(name='后来新增'))
        preview=self.request('/api/admin/operations?section=release_preview&id='+str(release),auth=True)[2]
        self.assertEqual(preview['campaigns']['removed_count'],1);self.assertEqual(preview['campaigns']['removed'][0]['title'],'后来新增')
        self.assertEqual(self.mutate('/releases/'+str(release),{})[0],400)
        self.op('advertiser','save',item={'id':'rev-change','name':'版本变化'})
        self.assertEqual(self.request('/api/admin/releases/'+str(release),{'revision':preview['revision'],'confirm':True},auth=True)[0],409)
        self.assertEqual(self.mutate('/releases/'+str(release),{'confirm':True})[0],200)

    def test_backup_lifecycle_and_account_archive_preserve_files_and_sessions(self):
        backup=self.op('backup','backup_create')[2];path=self.data/'backups'/backup['id'];self.assertTrue(path.is_file())
        rows=self.request('/api/admin/operations?section=backups',auth=True)[2]['rows'];self.assertEqual(rows[0]['id'],backup['id'])
        self.assertEqual(self.request('/api/admin/backups/'+backup['id'],auth=True,raw=True)[0],200)
        self.op('backup','archive',ids=[backup['id']]);self.op('backup','purge',ids=[backup['id']],confirm=True)
        self.assertTrue(path.is_file());self.assertFalse(self.request('/api/admin/operations?section=backups',auth=True)[2]['rows'])
        self.assertEqual(self.mutate('/users',{'name':'spare','password':'operations-test','role':'viewer','enabled':True})[0],200)
        _,headers,_=self.request('/api/admin/login',{'username':'spare','password':'operations-test'});cookie=headers['Set-Cookie'].split(';')[0]
        self.assertEqual(self.op('user','archive',ids=['spare'])[2]['ok'],['spare'])
        self.assertEqual(self.request('/api/admin/state',headers={'Cookie':cookie})[0],401)
        self.assertFalse(self.op('user','archive',ids=['admin'])[2]['ok'])
        self.op('user','restore',ids=['spare']);self.assertFalse(next(u for u in self.state()['users'] if u['name']=='spare')['enabled'])

    def test_source_disable_hides_catalog_files_and_sync_but_preserves_index(self):
        folder=self.external/'library';folder.mkdir();(folder/'film.mp4').write_bytes(b'film')
        source=self.mutate('/sources',{'name':'可停用片库','path':str(folder)})[2];self.wait_scan(source['id'])
        item=next(m for m in self.state()['media'] if m['source_id']==source['id']);self.mutate('/media/'+item['id'],{'published':True})
        body={'id':source['id'],'name':'改名片库','enabled':False,'notes':'临时停用'}
        self.assertEqual(self.op('source','source_save',item=body)[0],409)
        self.assertEqual(self.op('source','source_save',item=body,confirm=True)[0],200)
        self.assertEqual(self.request(item['path'],tv=True,raw=True)[0],404)
        self.assertNotIn(item['id'],[i['id'] for i in self.request('/api/v1/catalog',tv=True)[2]['items']])
        self.assertEqual(self.request('/api/admin/scan',{'source_id':source['id']},auth=True)[0],409)
        self.assertEqual(self.op('source','source_save',item={**body,'enabled':True})[0],200)
        self.assertIn(item['id'],[i['id'] for i in self.request('/api/v1/catalog',tv=True)[2]['items']])
        self.assertEqual(self.op('source','source_save',item={**body,'enabled':True,'path':'/etc'})[0],400)

    def test_published_series_can_edit_missing_episode_but_new_publish_fails(self):
        folder=self.media/'电视剧'/'片库剧';folder.mkdir(parents=True)
        for n in (1,2):(folder/('S01E0'+str(n)+'.mp4')).write_bytes(b'episode')
        self.wait_scan('main');item=next(m for m in self.state()['media'] if m['kind']=='series')
        self.assertEqual(self.mutate('/media/'+item['id'],{'published':True})[0],200)
        (folder/'S01E02.mp4').unlink()
        status,_,result=self.mutate('/media/'+item['id'],{'title':'保留上架改名'})
        self.assertEqual(status,200);self.assertTrue(result['warnings'])
        files=self.request('/api/admin/operations?section=media_files&id='+item['id'],auth=True)[2]
        self.assertEqual(files['missing'],1);self.assertIn('第 2 集',next(f['title'] for f in files['files'] if not f['readable']))
        self.mutate('/media/'+item['id'],{'published':False})
        status,_,result=self.mutate('/media/'+item['id'],{'published':True})
        self.assertEqual(status,404);self.assertIn('S01E02.mp4',result['error'])

    def test_report_event_time_filters_content_and_frozen_attribution(self):
        self.mutate('/campaigns/frozen',self.campaign(name='原广告名',merchant='原商户',tier=2))
        from datetime import datetime
        from zoneinfo import ZoneInfo
        def stamp(day):return int(datetime(2026,10,day,12,tzinfo=ZoneInfo('Asia/Singapore')).timestamp()*1000)
        device=self.store.device('tv-test-token')
        with patch('management.now_ms',return_value=stamp(28)):
            self.store.record_events(device,[{'id':'delayed','type':'text_started','creative_id':'frozen','at_ms':stamp(26)},{'id':'view','type':'content_started','content_id':'existing','client_session_id':'s1','at_ms':stamp(26)}])
        campaign=next(c for c in self.state()['campaigns'] if c['id']=='frozen')
        self.mutate('/campaigns/frozen',{**campaign,'name':'新广告名','merchant':'新商户'})
        with patch('management.now_ms',return_value=stamp(28)):
            report=self.store.reports(7,{'from':'2026-10-26','until':'2026-10-26'})
        self.assertEqual(report['rows'][0]['name'],'原广告名');self.assertEqual(report['rows'][0]['merchant'],'原商户');self.assertEqual(report['timeline'][0]['date'],'2026-10-26');self.assertEqual(report['content_sessions'],1)
        filtered=self.store.reports(7,{'from':'2026-10-26','until':'2026-10-26','merchant':'新商户'});self.assertFalse(filtered['rows'])
        grouped=self.store.reports(7,{'from':'2026-10-26','until':'2026-10-26','dimension':'merchant'});self.assertEqual(grouped['rows'][0]['name'],'原商户')
        self.assertEqual(self.request('/api/admin/reports?from=2026-12-31&until=2026-01-01',auth=True)[0],400)
        self.assertIn('unpublished',self.request('/api/admin/operations?section=overview',auth=True)[2])

    def test_report_aggregate_does_not_claim_first_attribution(self):
        self.mutate('/campaigns/one',self.campaign(name='一',merchant='甲',tier=2))
        self.mutate('/campaigns/two',self.campaign(name='二',merchant='乙',tier=3))
        device=self.store.device('tv-test-token')
        self.store.record_events(device,[{'id':'one','type':'text_started','creative_id':'one'}, {'id':'two','type':'text_started','creative_id':'two'}])
        report=self.store.reports(7,{'dimension':'device'})
        self.assertEqual(len(report['rows']),1)
        self.assertEqual(report['rows'][0]['merchant'],'多个广告主')
        self.assertEqual(report['rows'][0]['tier'],0)
        self.assertEqual(report['rows'][0]['starts'],2)

    def test_unicode_asset_references_and_clear_trash_keep_files(self):
        folder=self.media/'广告';folder.mkdir();file=folder/'中文素材.mp4';file.write_bytes(b'asset')
        self.op('asset','discover_assets');asset=self.state()['assets'][0]
        self.mutate('/campaigns/chinese',self.campaign(tier=1,format='视频',placements=['preroll'],path='/media/广告/中文素材.mp4'))
        blocked=self.op('asset','archive',ids=[asset['id']])[2];self.assertFalse(blocked['ok']);self.assertIn('引用',blocked['failed'][0]['reason'])
        self.op('campaign','archive',ids=['chinese'])
        self.op('advertiser','save',item={'id':'unused','name':'无引用广告主'});self.op('advertiser','archive',ids=['unused'])
        before=self.state()['revision'];out=self.op('system','trash_clear',mode='all',confirm=True)[2]
        self.assertIn('campaign:chinese',out['ok']);self.assertIn('advertiser:unused',out['ok']);self.assertEqual(self.state()['revision'],before+1);self.assertTrue(file.is_file())
        self.assertIn('csv',self.request('/api/admin/operations?section=audit_csv',auth=True)[2])
