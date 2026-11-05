from datetime import datetime
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from management import Store, Problem, password_hash
from server import make_server


class ManagementE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / 'data'; self.data.mkdir()
        self.media = self.root / 'media'; self.media.mkdir()
        self.external = self.root / 'libraries'; self.external.mkdir()
        self.web = self.root / 'web'; self.web.mkdir(); (self.web / 'index.html').write_text('<h1>管理后台</h1>')
        (self.media / 'clip.mp4').write_bytes(b'0123456789')
        (self.data / 'catalog.json').write_text(json.dumps({'hotel':'测试酒店','items':[{'id':'existing','title':'现有影片','kind':'movie','path':'/media/clip.mp4'}]}))
        (self.data / 'ads.json').write_text(json.dumps({'ads':[]}))
        self.config = self.root / 'devices.json'
        self.config.write_text(json.dumps({'devices':{'room-101':{'token':'tv-test-token','room':'101','group':'A组'}}}))
        self.store = Store(self.data, self.media, self.config, [self.media, self.external])
        self.tv = make_server('127.0.0.1',0,self.config,self.data,self.media,store=self.store)
        self.admin = make_server('127.0.0.1',0,self.config,self.data,self.media,store=self.store,admin_web=self.web)
        for server in (self.tv,self.admin): threading.Thread(target=server.serve_forever,daemon=True).start()
        credentials = dict(line.split('=',1) for line in (self.data/'admin-initial-password.txt').read_text().splitlines())
        status,headers,result=self.request('/api/admin/login',{'username':'admin','password':credentials['password']})
        self.assertEqual(status,200)
        self.cookie=headers['Set-Cookie'].split(';')[0]; self.csrf=result['user']['csrf']

    def tearDown(self):
        for server in (self.tv,self.admin): server.shutdown();server.server_close()
        self.tmp.cleanup()

    def request(self,path,body=None,auth=False,headers=None,raw=False,tv=False):
        target=self.tv if tv else self.admin
        conn=http.client.HTTPConnection('127.0.0.1',target.server_port,timeout=10)
        h={}
        if auth: h.update(Cookie=self.cookie,**{'X-CSRF-Token':self.csrf})
        if tv: h.update({'X-TV-Token':'tv-test-token','X-TV-Probe':'1'})
        h.update(headers or {})
        data=body if isinstance(body,bytes) else json.dumps(body).encode() if body is not None else None
        conn.request('POST' if body is not None else 'GET',path,body=data,headers=h)
        response=conn.getresponse(); result=response.read();hs=dict(response.getheaders());status=response.status;conn.close()
        return status,hs,result if raw else json.loads(result) if result else None

    def state(self):
        return self.request('/api/admin/state',auth=True)[2]

    def mutate(self,path,body):
        return self.request('/api/admin'+path,{**body,'revision':self.state()['revision']},auth=True)

    def campaign(self,**changes):
        body={'name':'真实广告','merchant':'测试商户','tier':3,'priority':50,'duration':5,'gap_seconds':60,'cap':2,'format':'文字','status':'投放中',
              'group':'全部客房','text':'测试文字','path':'','placements':['all'],'start':'22:00','end':'02:00','days':[1,2,3,4,5,6,7],
              'timezone':'Asia/Singapore','from':'2026-10-27','until':'2026-11-30'}
        body.update(changes);return body

    def wait_scan(self,source):
        status,_,job=self.request('/api/admin/scan',{'source_id':source},auth=True);self.assertEqual(status,200)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            current=next(j for j in self.state()['jobs'] if j['id']==job['id'])
            if current['status'] in ('failed','completed'): return current
            time.sleep(.03)
        self.fail('scan did not finish')

    def category(self, ident='drama', **changes):
        body={'name':'剧情','kind':'all','sort':0,'enabled':True,'aliases':[]};body.update(changes)
        return self.mutate('/categories/'+ident,body)

    def test_category_registration_rename_restore_and_delete(self):
        with self.store.db() as db:
            old=json.loads(db.execute("SELECT body FROM media WHERE id='existing'").fetchone()[0]);old['category']='旧分类'
            db.execute("UPDATE media SET body=? WHERE id='existing'",(json.dumps(old),))
        rev=self.state()['revision'];again=Store(self.data,self.media,self.config,[self.media,self.external])
        self.assertEqual(again.state()['revision'],rev)
        category=again.state()['categories'][0];self.assertEqual((category['name'],category['media_count'],category['published_count']),('旧分类',1,1))
        self.assertEqual(self.mutate('/categories/'+category['id'],{**category,'name':'新分类'})[0],200)
        self.assertEqual(self.state()['revision'],rev+1)
        self.assertEqual(self.request('/api/v1/catalog',tv=True)[2]['items'][0]['category'],'新分类')
        code,_,error=self.mutate('/categories/'+category['id']+'/delete',{})
        self.assertEqual(code,409);self.assertIn('1 部',error['error'])
        self.assertEqual(self.category('empty',name='空分类')[0],200)
        self.assertEqual(self.mutate('/categories/empty/delete',{})[0],200)
        self.assertEqual(self.category('duplicate',name=' 新分类 ')[0],409)
        # The baseline release still has no category: restoring must register new legacy names too.
        with self.store.db() as db:
            self.store.snapshot(db,'test','restore-seed');release=db.execute('SELECT max(id) FROM releases').fetchone()[0]
            snap=json.loads(db.execute('SELECT body FROM releases WHERE id=?',(release,)).fetchone()[0]);item=json.loads(snap['media'][0]['body']);item['category']='恢复分类';snap['media'][0]['body']=json.dumps(item)
            db.execute('UPDATE releases SET body=? WHERE id=?',(json.dumps(snap),release))
        self.store.restore_release(release,'test',self.state()['revision'])
        self.assertIn('恢复分类',[c['name'] for c in self.state()['categories']])

    def test_disabled_category_and_roles(self):
        self.assertEqual(self.category()[0],200)
        self.assertEqual(self.mutate('/media/existing',{'category':'剧情'})[0],200)
        self.assertEqual(self.category(enabled=False)[0],200)
        self.assertEqual(self.mutate('/media/existing',{'title':'保留停用分类'})[0],200)
        self.assertEqual(self.category('other',name='纪录')[0],200)
        self.assertEqual(self.mutate('/media/existing',{'category':'纪录'})[0],200)
        self.assertEqual(self.mutate('/media/existing',{'category':'剧情'})[0],400)
        self.assertEqual(self.mutate('/media/existing',{'category':'不存在'})[0],400)
        for role in ('viewer','operator'):
            with self.store.db() as db:db.execute('INSERT INTO users VALUES(?,?,?,1)',(role,password_hash('role-test-password'),role))
            _,headers,result=self.request('/api/admin/login',{'username':role,'password':'role-test-password'})
            h={'Cookie':headers['Set-Cookie'].split(';')[0],'X-CSRF-Token':result['user']['csrf']}
            body={'name':'权限分类','revision':self.state()['revision']}
            self.assertEqual(self.request('/api/admin/categories/role-'+role,body,headers=h)[0],403 if role=='viewer' else 200)
            for suffix in ('merge','delete'):
                self.assertEqual(self.request('/api/admin/categories/drama/'+suffix,{'into':'other','revision':self.state()['revision']},headers=h)[0],403)

    def test_batch_partial_success_revision_details_and_limits(self):
        self.category()
        self.mutate('/media/existing',{'category':'剧情','published':False})
        with self.store.db() as db:
            item={'id':'bad','kind':'series','title':'缺集剧','category':'剧情','episodes':[{'id':'ep1','title':'第 2 集','path':'/media/missing.mp4'}]}
            db.execute('INSERT INTO media VALUES(?,?,?,?,?)',('bad','main',json.dumps(item),0,1))
        before=self.state()['revision']
        code,_,result=self.mutate('/media/batch',{'ids':['existing','bad','absent'],'action':'publish'})
        self.assertEqual(code,200);self.assertEqual(result['ok'],['existing']);self.assertEqual(len(result['failed']),2)
        self.assertIn('第 2 集',result['failed'][0]['reason']);self.assertIn('missing.mp4',result['failed'][0]['reason'])
        self.assertEqual(self.state()['revision'],before+1)
        self.assertIn('批量上架 1 部（失败 2）',self.state()['audit'][0]['action'])
        self.assertTrue(next(m for m in self.state()['media'] if m['id']=='existing')['published'])
        self.assertEqual(self.mutate('/media/batch',{'ids':[str(i) for i in range(501)],'action':'pin'})[0],400)
        for body in ({'ids':[],'action':'pin'},{'ids':['existing']*2,'action':'pin'},{'ids':['existing'],'action':'delete'}):
            self.assertEqual(self.mutate('/media/batch',body)[0],400)
        self.assertEqual(self.request('/api/admin/media/batch',{'ids':['existing'],'action':'pin','revision':before},auth=True)[0],409)
        for action,key,value in [('pin','_pinned',True),('unpin','_pinned',False),('archive','_archived',True),('restore','_archived',False)]:
            self.assertEqual(self.mutate('/media/batch',{'ids':['existing'],'action':action})[2]['ok'],['existing'])
            item=next(m for m in self.state()['media'] if m['id']=='existing');self.assertEqual(item[key],value)
        self.assertFalse(item['published'])
        self.category('docs',name='纪录')
        self.assertEqual(self.mutate('/media/batch',{'ids':['existing'],'action':'set_category','value':'纪录'})[2]['ok'],['existing'])
        self.assertEqual(self.mutate('/media/batch',{'ids':['existing'],'action':'unpublish'})[0],200)
        with self.store.db() as db: db.execute('INSERT INTO users VALUES(?,?,?,1)',('batchviewer',password_hash('viewer-test-password'),'viewer'))
        _,headers,result=self.request('/api/admin/login',{'username':'batchviewer','password':'viewer-test-password'})
        self.assertEqual(self.request('/api/admin/media/batch',{'ids':['existing'],'action':'pin','revision':self.state()['revision']},headers={'Cookie':headers['Set-Cookie'].split(';')[0],'X-CSRF-Token':result['user']['csrf']})[0],403)
        revision=self.state()['revision']
        self.assertEqual(self.mutate('/media/batch',{'ids':['absent'],'action':'publish'})[2]['ok'],[])
        self.assertEqual(self.state()['revision'],revision)

    def test_catalog_category_pin_date_name_order_and_recursive_public_fields(self):
        self.category('later',name='剧情',sort=20);self.category('first',name='纪录',sort=0)
        rows=[('drama','剧情',False,99,'剧情片'),('pin','纪录',True,1,'置顶片'),('new','纪录',False,20,'新片'),('a','纪录',False,10,'A片'),('b','纪录',False,10,'B片')]
        with self.store.db() as db:
            db.execute("UPDATE media SET published=0 WHERE id='existing'")
            for ident,category,pinned,created,title in rows:
                item={'id':ident,'kind':'series','title':title,'category':category,'poster':'/media/poster.jpg','episodes':[{'id':'ep-'+ident,'path':'/media/clip.mp4','title':'分集','_internal':'hidden','jellyfin_id':'private'}], '_pinned':pinned,'_created_at':created,'_future':{'secret':True},'jellyfin_id':'private'}
                db.execute('INSERT INTO media VALUES(?,?,?,?,?)',(ident,'main',json.dumps(item),1,1))
        items=self.request('/api/v1/catalog',tv=True)[2]['items'];self.assertEqual([m['id'] for m in items],['pin','new','a','b','drama'])
        def check(value):
            if isinstance(value,dict):
                self.assertTrue(all(not k.startswith('_') and k!='jellyfin_id' for k in value))
                for v in value.values():check(v)
            elif isinstance(value,list):
                for v in value:check(v)
        check(items)
        self.assertTrue(all(m['poster']=='/media/poster.jpg' and m['episodes'][0]['path']=='/media/clip.mp4' for m in items))

    def scan_fixture(self):
        path=self.media/'电影'/'纪录'/'时间验证片.mp4';path.parent.mkdir(parents=True);path.write_bytes(b'technical-fixture')
        self.assertEqual(self.wait_scan('main')['status'],'completed')
        return path,next(m for m in self.state()['media'] if m['title']=='时间验证片')

    def test_scan_creation_time_legacy_no_backfill_and_noop_stability(self):
        self.assertNotIn('_created_at',self.state()['media'][0])
        with patch('management.now_ms',return_value=10000):path,item=self.scan_fixture()
        self.assertEqual((item['_created_at'],item['_updated_at']),(10000,10000))
        legacy=next(m for m in self.state()['media'] if m['id']=='existing');self.assertNotIn('_created_at',legacy)
        before=self.state()
        with patch('management.now_ms',return_value=20000):self.assertEqual(self.wait_scan('main')['status'],'completed')
        after=self.state();self.assertEqual(after['revision'],before['revision']);self.assertEqual(after['media'],before['media'])
        reopened=Store(self.data,self.media,self.config,[self.media,self.external])
        self.assertEqual(reopened.state()['revision'],before['revision']);self.assertEqual(reopened.state()['media'],before['media'])

    def test_edit_batch_and_category_changes_update_only_modified_time(self):
        with patch('management.now_ms',return_value=10000):_,item=self.scan_fixture()
        ident=item['id']
        with patch('management.now_ms',return_value=20000):
            self.assertEqual(self.mutate('/media/'+ident,{'title':'新片名','published':True,'_created_at':1,'_updated_at':1})[0],200)
        item=next(m for m in self.state()['media'] if m['id']==ident)
        self.assertEqual((item['_created_at'],item['_updated_at']),(10000,20000))
        with patch('management.now_ms',return_value=30000):self.assertEqual(self.mutate('/media/'+ident,item)[0],200)
        self.assertEqual(next(m for m in self.state()['media'] if m['id']==ident)['_updated_at'],20000)
        self.category('time-category',name='时间分类')
        for stamp,action,value in [(40000,'unpublish',None),(50000,'pin',None),(60000,'set_category','时间分类'),(70000,'archive',None),(80000,'restore',None),(90000,'unpin',None)]:
            with patch('management.now_ms',return_value=stamp):
                result=self.mutate('/media/batch',{'ids':[ident],'action':action,'value':value})[2];self.assertEqual(result['ok'],[ident])
            item=next(m for m in self.state()['media'] if m['id']==ident)
            self.assertEqual((item['_created_at'],item['_updated_at']),(10000,stamp))
        with patch('management.now_ms',return_value=100000):self.mutate('/media/batch',{'ids':[ident],'action':'unpin'})
        self.assertEqual(next(m for m in self.state()['media'] if m['id']==ident)['_updated_at'],90000)
        with patch('management.now_ms',return_value=110000):self.assertEqual(self.category('time-category',name='改名分类')[0],200)
        item=next(m for m in self.state()['media'] if m['id']==ident)
        self.assertEqual((item['category'],item['_created_at'],item['_updated_at']),('改名分类',10000,110000))
        rev=self.state()['revision']
        with patch('management.now_ms',return_value=120000):self.assertEqual(self.wait_scan('main')['status'],'completed')
        self.assertEqual(next(m for m in self.state()['media'] if m['id']==ident),item);self.assertEqual(self.state()['revision'],rev)

    def test_scan_availability_change_updates_once(self):
        with patch('management.now_ms',return_value=10000):path,item=self.scan_fixture()
        ident=item['id'];path.unlink()
        with patch('management.now_ms',return_value=20000):self.assertEqual(self.wait_scan('main')['status'],'completed')
        missing=next(m for m in self.state()['media'] if m['id']==ident)
        self.assertFalse(missing['available']);self.assertEqual((missing['_created_at'],missing['_updated_at']),(10000,20000))
        revision=self.state()['revision']
        with patch('management.now_ms',return_value=30000):self.assertEqual(self.wait_scan('main')['status'],'completed')
        self.assertEqual(next(m for m in self.state()['media'] if m['id']==ident),missing);self.assertEqual(self.state()['revision'],revision)
        path.write_bytes(b'restored')
        with patch('management.now_ms',return_value=40000):self.assertEqual(self.wait_scan('main')['status'],'completed')
        restored=next(m for m in self.state()['media'] if m['id']==ident)
        self.assertTrue(restored['available']);self.assertEqual((restored['_created_at'],restored['_updated_at']),(10000,40000))

    def test_login_csrf_role_and_static_isolation(self):
        self.assertEqual(self.request('/api/admin/state')[0],401)
        self.assertEqual(self.request('/api/admin/settings',{},headers={'Cookie':self.cookie})[0],403)
        self.assertEqual(self.request('/api/admin/settings',{},auth=True,headers={'Origin':'http://evil.example'})[0],403)
        self.assertEqual(self.request('/api/admin/state',tv=True)[0],404)
        self.assertEqual(self.request('/../data/admin-initial-password.txt')[0],404)
        with self.store.db() as db: db.execute('INSERT INTO users VALUES(?,?,?,1)',('reader',password_hash('read-only-test-password'),'viewer'))
        _,headers,login=self.request('/api/admin/login',{'username':'reader','password':'read-only-test-password'})
        cookie=headers['Set-Cookie'].split(';')[0]
        self.assertEqual(self.request('/api/admin/settings',{},headers={'Cookie':cookie,'X-CSRF-Token':login['user']['csrf']})[0],403)
        self.assertNotIn('token',json.dumps(self.state()['devices']))

    def test_source_scan_publish_stable_identity_and_read_only_files(self):
        folder=self.external/'电影库';(folder/'电影'/'自然').mkdir(parents=True)
        clip=folder/'电影'/'自然'/'片名 空格.mp4';clip.write_bytes(b'abcdefghij')
        status,_,source=self.mutate('/sources',{'name':'本地片库','path':str(folder)})
        self.assertEqual(status,200);self.assertEqual(self.wait_scan(source['id'])['status'],'completed')
        item=next(m for m in self.state()['media'] if m['source_id']==source['id'])
        self.assertFalse(item['published']);self.assertEqual(item['category'],'自然')
        self.assertEqual(len(self.request('/api/v1/catalog',tv=True)[2]['items']),1)
        self.assertEqual(self.mutate('/media/'+item['id'],{'title':'手动编辑片名','published':True})[0],200)
        url=next(i['path'] for i in self.request('/api/v1/catalog',tv=True)[2]['items'] if i['id']==item['id'])
        status,headers,raw=self.request(url,headers={'Range':'bytes=2-5'},raw=True,tv=True)
        self.assertEqual((status,raw),(206,b'cdef'));self.assertEqual(headers['Content-Range'],'bytes 2-5/10')
        self.assertEqual(self.wait_scan(source['id'])['status'],'completed')
        updated=next(m for m in self.state()['media'] if m['id']==item['id'])
        self.assertEqual(updated['title'],'手动编辑片名');self.assertTrue(updated['published'])
        self.assertEqual(clip.read_bytes(),b'abcdefghij')

    def test_source_boundaries_symlinks_and_empty_disk_do_not_erase_catalog(self):
        self.assertEqual(self.mutate('/sources',{'name':'不允许','path':str(self.root)})[0],400)
        self.assertEqual(self.mutate('/sources',{'name':'重复','path':str(self.media)})[0],400)
        secret=self.root/'private.mp4';secret.write_bytes(b'secret')
        (self.media/'escape.mp4').symlink_to(secret)
        self.assertEqual(self.request('/media/escape.mp4',tv=True)[0],404)
        (self.media/'clip.mp4').rename(self.media/'clip.offline')
        job=self.wait_scan('main');self.assertEqual(job['status'],'failed')
        self.assertEqual(len(self.request('/api/v1/catalog',tv=True)[2]['items']),1)

    def test_group_targeting_publishing_pause_and_multiple_placements(self):
        status,_,device=self.mutate('/devices/room-202',{'room':'202','group':'B组','enabled':True})
        self.assertEqual(status,200);self.assertTrue(device['token'])
        ad=self.campaign(tier=1,format='视频',path='/media/clip.mp4',placements=['preroll','midroll'],group='B组')
        self.assertEqual(self.mutate('/campaigns/campaign-test',ad)[0],200)
        self.assertEqual(self.request('/api/v1/ads',tv=True)[2]['ads'],[])
        request_headers={'X-TV-Token':device['token'],'X-TV-Probe':'1'}
        manifest=self.request('/api/v1/ads',headers=request_headers,tv=True)[2]
        self.assertEqual({a['placement'] for a in manifest['ads']},{'preroll','midroll'})
        self.assertTrue(all(a['campaign_id']=='campaign-test' for a in manifest['ads']))
        ad['status']='已暂停';self.assertEqual(self.mutate('/campaigns/campaign-test',ad)[0],200)
        self.assertFalse(self.request('/api/v1/ads',headers=request_headers,tv=True)[2]['ads'])

    def test_dates_overnight_tiers_and_media_validation(self):
        for tier in (2,3):
            self.assertEqual(self.mutate('/campaigns/tier-'+str(tier),self.campaign(tier=tier))[0],200)
        ads=self.request('/api/v1/ads',tv=True)[2]['ads']
        full=next(a for a in ads if a['tier']==2);timed=next(a for a in ads if a['tier']==3)
        self.assertEqual((full['start_time'],full['end_time']),('',''))
        self.assertEqual((timed['start_time'],timed['end_time']),('22:00','02:00'))
        self.assertEqual(timed['weekdays'],[1,2,3,4,5,6,7]);self.assertGreater(timed['valid_until_ms'],timed['valid_from_ms'])
        self.assertEqual(self.mutate('/campaigns/bad',self.campaign(tier=1,format='视频',path='/etc/passwd',placements=['preroll']))[0],404)
        self.assertEqual(self.mutate('/campaigns/bad',self.campaign(start='25:00'))[0],400)

    def test_event_dedup_real_reports_exclude_probes_and_resume_count(self):
        self.assertEqual(self.mutate('/campaigns/real-ad',self.campaign())[0],200)
        events=[{'id':'start','type':'text_started','creative_id':'real-ad','tier':3,'_campaign_id':'forged'},
                {'id':'end','type':'text_ended','creative_id':'real-ad','visible_ms':60000},
                {'id':'probe','type':'deployment_probe','exclude_from_ad_reports':True},
                {'id':'view1','type':'content_started','client_session_id':'session-1','content_id':'movie1'},
                {'id':'view2','type':'content_started','client_session_id':'session-1','content_id':'movie1'}]
        for _ in range(2): self.assertEqual(self.request('/api/v1/events',{'events':events},tv=True)[0],200)
        reports=self.request('/api/admin/reports?days=7',auth=True)[2]
        self.assertEqual(reports['content_sessions'],1)
        row=reports['rows'][0];self.assertEqual((row['id'],row['starts'],row['visible_ms']),('real-ad',1,60000))
        self.assertEqual(len(reports['rows']),1)

    def test_report_timeline_keeps_calendar_order_across_new_year(self):
        zone=ZoneInfo('Asia/Singapore')
        def ms(y,m,d):return int(datetime(y,m,d,12,tzinfo=zone).timestamp()*1000)
        with self.store.db() as db:
            for n,when in enumerate([ms(2026,1,5),ms(2025,12,30),ms(2026,1,5)]):
                db.execute('INSERT INTO events VALUES(?,?,?,?)',('room-101:ny%d'%n,'room-101',when,json.dumps({'id':'ny%d'%n,'type':'ad_started','creative_id':'real-ad'})))
        with patch('management.now_ms',return_value=ms(2026,1,7)):
            timeline=self.store.reports(30)['timeline']
        self.assertEqual([(d['date'],d['day'],d['ad_starts']) for d in timeline],[('2025-12-30','12-30',1),('2026-01-05','01-05',2)])
        self.assertEqual(self.request('/api/admin/reports?days=7',auth=True)[0],200)

    def test_device_revocation_is_immediate_and_probes_do_not_fake_online(self):
        self.request('/api/v1/catalog',tv=True)
        self.assertFalse(self.state()['devices'][0]['online'])
        self.request('/api/v1/catalog',headers={'X-TV-Probe':'0'},tv=True)
        self.assertTrue(self.state()['devices'][0]['online'])
        self.assertEqual(self.mutate('/devices/room-101',{'room':'101','group':'A组','enabled':False})[0],200)
        self.assertEqual(self.request('/api/v1/catalog',tv=True)[0],401)

    def test_revision_conflict_restore_and_database_backup(self):
        before=self.state();release=before['releases'][0]['id'];settings={**before['settings'],'hotel':'改过的酒店'}
        self.assertEqual(self.mutate('/settings',settings)[0],200)
        self.assertEqual(self.request('/api/admin/settings',{**settings,'revision':before['revision']},auth=True)[0],409)
        self.assertEqual(self.mutate('/releases/'+str(release),{'confirm':True})[0],200)
        self.assertEqual(self.state()['settings']['hotel'],'测试酒店')
        status,_,raw=self.request('/api/admin/backups',{},auth=True,raw=True)
        self.assertEqual(status,200);self.assertTrue(raw.startswith(b'SQLite format 3'))
        backup=self.root/'backup.sqlite3';backup.write_bytes(raw)
        with sqlite3.connect(backup) as db:self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_upload_asset_stream_and_private_paths(self):
        raw=b'\xff\xd8\xff'+b'example-image-payload'
        status,_,result=self.request('/api/admin/uploads?kind=posters',raw,auth=True,headers={'X-Filename':'poster.jpg'})
        self.assertEqual(status,201)
        self.assertEqual(self.request(result['path'],auth=True,raw=True)[2],raw)
        self.assertEqual(self.request(result['path'],raw=True)[0],401)
        self.assertEqual(self.request('/api/admin/uploads?kind=posters',b'bad',auth=True,headers={'X-Filename':'evil.html'})[0],400)

    def test_rescan_preserves_edited_episodes_and_restart_preserves_database(self):
        show=self.media/'电视剧'/'测试剧'/'Season01';show.mkdir(parents=True)
        for n in (1,2):(show/('S01E%02d.mp4'%n)).write_bytes(b'episode')
        self.assertEqual(self.wait_scan('main')['status'],'completed')
        series=next(m for m in self.state()['media'] if m.get('kind')=='series')
        episodes=series['episodes'];episodes[0].update(title='编辑后的第一集',number=8)
        self.assertEqual(self.mutate('/media/'+series['id'],{'episodes':episodes,'title':'编辑后的剧名'})[0],200)
        self.assertEqual(self.wait_scan('main')['status'],'completed')
        again=next(m for m in self.state()['media'] if m['id']==series['id'])
        self.assertEqual(again['title'],'编辑后的剧名')
        self.assertTrue(any(e['title']=='编辑后的第一集' and e['number']==8 for e in again['episodes']))
        reopened=Store(self.data,self.media,self.config,[self.media,self.external])
        persisted=next(m for m in reopened.state()['media'] if m['id']==series['id'])
        self.assertEqual(persisted['episodes'],again['episodes'])

    def test_account_disable_invalidates_existing_session(self):
        self.assertEqual(self.mutate('/users',{'name':'operator','password':'operator-test-password','role':'operator','enabled':True})[0],200)
        _,headers,body=self.request('/api/admin/login',{'username':'operator','password':'operator-test-password'})
        cookie=headers['Set-Cookie'].split(';')[0]
        self.assertEqual(self.request('/api/admin/state',headers={'Cookie':cookie})[0],200)
        self.assertEqual(self.mutate('/users',{'name':'operator','role':'operator','enabled':False})[0],200)
        self.assertEqual(self.request('/api/admin/state',headers={'Cookie':cookie})[0],401)

    def test_eight_character_password_change_revokes_old_login(self):
        self.assertEqual(self.mutate('/users',{'name':'admin','password':'admin123','role':'admin','enabled':True})[0],200)
        self.assertEqual(self.request('/api/admin/state',auth=True)[0],401)
        status,headers,body=self.request('/api/admin/login',{'username':'admin','password':'admin123'})
        self.assertEqual(status,200)
        self.assertEqual(body['user']['role'],'admin')
        self.assertTrue(headers['Set-Cookie'].startswith('hotel_session='))
        with self.assertRaises(Problem): password_hash('short')


if __name__=='__main__': unittest.main()
