import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit
from management import Store,Problem

class JellyfinIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.media=self.root/'media';self.media.mkdir();(self.media/'film.mp4').write_bytes(b'film-data')
        (self.media/'S01E01.mp4').write_bytes(b'episode-data')
        self.data=self.root/'data';self.data.mkdir();self.config=self.root/'devices.json';self.config.write_text('{"devices":{}}')
        (self.data/'catalog.json').write_text(json.dumps({'items':[{'id':'legacy','kind':'movie','title':'酒店自定片名','path':'/media/film.mp4'}]}))
        self.store=Store(self.data,self.media,self.config)
        self.movie={'Id':'a'*32,'Name':'Jellyfin片名','Type':'Movie','Path':str(self.media/'film.mp4'),'ProductionYear':2026,'Overview':'来自 Jellyfin','ImageTags':{'Primary':'tag1'}}
        self.show={'Id':'b'*32,'Name':'连续剧','Type':'Series','ImageTags':{}}
        self.episode={'Id':'c'*32,'Name':'第一集','Type':'Episode','Path':str(self.media/'S01E01.mp4'),'SeriesId':'b'*32,'ParentIndexNumber':1,'IndexNumber':1}
        self.episodes=[self.episode]
        self.upstream_failed=False;test=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                if 'Token="private-jellyfin-key"' not in self.headers.get('Authorization',''):self.send_error(401);return
                if test.upstream_failed:self.send_error(503);return
                if '/Images/' in self.path:
                    raw=b'\xff\xd8\xff\xd9';self.send_response(200);self.send_header('Content-Type','image/jpeg')
                else:
                    kind=parse_qs(urlsplit(self.path).query).get('IncludeItemTypes',[''])[0]
                    rows={'Movie':[test.movie],'Series':[test.show],'Episode':test.episodes}.get(kind,[])
                    raw=json.dumps({'Items':rows,'TotalRecordCount':len(rows)}).encode();self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        self.http=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=self.http.serve_forever,daemon=True).start()
        (self.data/'nas-integration.json').write_text(json.dumps({'url':f'http://127.0.0.1:{self.http.server_port}','token':'private-jellyfin-key'}))
    def tearDown(self):self.http.shutdown();self.http.server_close();self.tmp.cleanup()
    def sync(self):
        job=self.store.start_jellyfin_sync('tester')
        for _ in range(100):
            state=self.store.state();current=next(j for j in state['jobs'] if j['id']==job['id'])
            if current['status'] in ('completed','failed'):return current
            time.sleep(.02)
        self.fail('sync did not finish')
    def scan(self):
        job=self.store.start_scan('main','tester')
        for _ in range(100):
            current=next(j for j in self.store.state()['jobs'] if j['id']==job['id'])
            if current['status'] in ('completed','failed'):return current
            time.sleep(.02)
        self.fail('scan did not finish')
    def item(self,ident):return next(m for m in self.store.state()['media'] if m['id']==ident)
    def counts(self):
        with self.store.db() as db:
            return (self.store.kv(db,'revision'),db.execute('SELECT count(*) FROM releases').fetchone()[0],db.execute('SELECT count(*) FROM audit').fetchone()[0])
    def second_episode(self):
        (self.media/'S01E02.mp4').write_bytes(b'episode-two')
        self.episodes.append({'Id':'d'*32,'Name':'第二集','Type':'Episode','Path':str(self.media/'S01E02.mp4'),'SeriesId':'b'*32,'ParentIndexNumber':1,'IndexNumber':2})
    def test_manual_episode_edits_survive_sync_rescan_and_restart(self):
        self.second_episode();self.assertEqual(self.sync()['status'],'completed')
        show=next(m for m in self.store.state()['media'] if m['kind']=='series');first_id,second_id='jf-ep-'+'c'*32,'jf-ep-'+'d'*32
        edited=[{**e,'title':'酒店自定义分集名','season':2,'number':5} if e['id']==first_id else e for e in show['episodes']]
        self.store.save_media(show['id'],{'episodes':edited},'admin',self.store.state()['revision'])
        self.episodes[1]['Name']='上游改名的第二集'
        for _ in range(2):self.assertEqual(self.sync()['status'],'completed')
        again=self.item(show['id']);by_id={e['id']:e for e in again['episodes']}
        self.assertEqual((by_id[first_id]['title'],by_id[first_id]['season'],by_id[first_id]['number']),('酒店自定义分集名',2,5))
        self.assertEqual(by_id[second_id]['title'],'上游改名的第二集')
        self.assertEqual([e['id'] for e in again['episodes']],[second_id,first_id])
        self.assertEqual(self.scan()['status'],'completed');self.assertEqual(self.sync()['status'],'completed')
        kept=self.item(show['id']);self.assertTrue(any(e['id']==first_id and e['title']=='酒店自定义分集名' and e['season']==2 for e in kept['episodes']))
        reopened=Store(self.data,self.media,self.config)
        self.assertEqual(next(m for m in reopened.state()['media'] if m['id']==show['id'])['episodes'],kept['episodes'])
        # Setting the fields back to what Jellyfin supplied makes the episode follow Jellyfin again.
        restored=[{**e,'title':'第一集','season':1,'number':1} if e['id']==first_id else e for e in kept['episodes']]
        self.store.save_media(show['id'],{'episodes':restored},'admin',self.store.state()['revision'])
        self.episodes[0]['Name']='上游再次改名';self.assertEqual(self.sync()['status'],'completed')
        self.assertEqual(next(e for e in self.item(show['id'])['episodes'] if e['id']==first_id)['title'],'上游再次改名')
        self.assertNotIn('private-jellyfin-key',json.dumps(self.store.catalog({'id':'test'})))
    def test_missing_files_withdraw_once_without_revision_churn(self):
        self.second_episode()
        for _ in range(2):self.assertEqual(self.sync()['status'],'completed')
        steady=self.counts();self.assertEqual(self.sync()['status'],'completed');self.assertEqual(self.counts(),steady)
        show=next(m for m in self.store.state()['media'] if m['kind']=='series')
        (self.media/'S01E01.mp4').unlink()
        job=self.sync();again=self.item(show['id'])
        self.assertTrue(again['available']);self.assertEqual([e['path'] for e in again['episodes']],['/media/S01E02.mp4']);self.assertEqual(job['missing'],0)
        (self.media/'film.mp4').unlink()  # Jellyfin keeps listing the film until its own scan notices.
        before=self.counts();job=self.sync()
        self.assertEqual((job['missing'],job['still_missing']),(1,0));self.assertFalse(self.item('legacy')['available'])
        bumped=self.counts();self.assertEqual(bumped,(before[0]+1,before[1]+1,before[2]+1))
        for _ in range(3):
            job=self.sync();self.assertEqual((job['missing'],job['still_missing'],job['status']),(0,1,'completed'))
        self.assertEqual(self.counts(),bumped);status=self.store.jellyfin_status()
        self.assertEqual((status['status'],status['still_missing']),('connected',1))
        (self.media/'film.mp4').write_bytes(b'film-data');self.assertEqual(self.sync()['status'],'completed')
        self.assertTrue(self.item('legacy')['available']);self.assertEqual(self.counts()[0],bumped[0]+1)
        self.assertEqual(self.sync()['status'],'completed');self.assertEqual(self.counts()[0],bumped[0]+1)
    def test_kind_conflict_never_replaces_hotel_entry(self):
        show=self.media/'电视剧'/'本地剧'/'Season01';show.mkdir(parents=True)
        for n in (1,2):(show/('S01E%02d.mp4'%n)).write_bytes(b'local')
        self.assertEqual(self.scan()['status'],'completed')
        local=next(m for m in self.store.state()['media'] if m['title']=='本地剧')
        self.store.save_media(local['id'],{'published':True},'admin',self.store.state()['revision'])
        self.movie['Path']=str(show/'S01E01.mp4');self.movie['Name']='被当成电影的分集'
        job=self.sync();self.assertEqual(job['status'],'completed')
        kept=self.item(local['id'])
        self.assertEqual((kept['kind'],len(kept['episodes']),kept['published'],kept['available']),('series',2,True,True))
        self.assertGreaterEqual(job['skipped'],1);self.assertTrue(any('类型' in r['reason'] for r in job['skipped_reasons']))
        self.assertFalse(any(m['title']=='被当成电影的分集' for m in self.store.state()['media']))
        self.assertEqual(self.sync()['status'],'completed');self.assertEqual(self.item(local['id'])['episodes'],kept['episodes'])
    def test_failure_keeps_last_success_and_counts_failures(self):
        self.assertEqual(self.sync()['status'],'completed');ok=self.store.jellyfin_status()
        self.upstream_failed=True
        for n in (1,2):
            self.assertEqual(self.sync()['status'],'failed');bad=self.store.jellyfin_status();self.assertEqual(bad['consecutive_failures'],n)
        self.assertEqual((bad['status'],bad['last_sync'],bad['items']),('error',ok['last_sync'],ok['items']))
        self.assertTrue(bad['error']);self.assertGreaterEqual(bad['last_attempt'],ok['last_sync'])
        self.upstream_failed=False;self.assertEqual(self.sync()['status'],'completed');fine=self.store.jellyfin_status()
        self.assertEqual((fine['status'],fine['consecutive_failures'],fine['error']),('connected',0,''));self.assertGreaterEqual(fine['last_sync'],ok['last_sync'])
    def test_failed_job_and_failure_count_become_visible_together(self):
        from unittest.mock import patch
        entered,release=threading.Event(),threading.Event()
        update=self.store.update_jellyfin_status
        def blocked(db,**fields):
            if fields.get('status')=='error':
                entered.set();release.wait(3)
            return update(db,**fields)
        self.upstream_failed=True
        with patch.object(self.store,'update_jellyfin_status',side_effect=blocked):
            job=self.store.start_jellyfin_sync('tester')
            try:
                self.assertTrue(entered.wait(2))
                with self.store.db() as db:current=json.loads(db.execute('SELECT body FROM jobs WHERE id=?',(job['id'],)).fetchone()[0])
                self.assertNotEqual(current['status'],'failed','failed status must not precede the failure counter transaction')
            finally:release.set()
            for _ in range(100):
                with self.store.db() as db:current=json.loads(db.execute('SELECT body FROM jobs WHERE id=?',(job['id'],)).fetchone()[0])
                if current['status']=='failed':break
                time.sleep(.02)
            self.assertEqual(current['status'],'failed')
            self.assertEqual(self.store.jellyfin_status()['consecutive_failures'],1)
    def test_scheduler_survives_unexpected_errors(self):
        calls=[]
        def start(actor,refresh=False):
            calls.append(actor)
            if len(calls)==1:raise sqlite3.OperationalError('database is locked')
        self.store.start_jellyfin_sync=start;self.store.sync_interval_seconds=0.01
        self.store.run_jellyfin_scheduler()
        deadline=time.monotonic()+5
        while len(calls)<3 and time.monotonic()<deadline:time.sleep(.02)
        self.store.sync_interval_seconds=120
        self.assertGreaterEqual(len(calls),3)
        self.assertTrue(any(t.name=='jellyfin-sync' and t.is_alive() for t in threading.enumerate()))
        self.assertIn('scheduler_alive',self.store.jellyfin_status())
    def test_category_merge_maps_alias_without_revision_churn(self):
        self.movie['Genres']=['Drama'];self.assertEqual(self.sync()['status'],'completed')
        source=next(c for c in self.store.state()['categories'] if c['name']=='Drama')
        self.store.save_category('cn',{'name':'剧情','aliases':['故事']},'test',self.store.state()['revision'])
        self.store.merge_category(source['id'],{'into':'cn'},'test',self.store.state()['revision'])
        self.assertEqual(self.item('legacy')['category'],'剧情')
        self.assertEqual(self.item('legacy')['_jellyfin_meta']['category'],'剧情')
        steady=self.counts()
        for _ in range(3):
            self.assertEqual(self.sync()['status'],'completed');self.assertEqual(self.counts(),steady)
        self.movie['Genres']=['故事'];self.assertEqual(self.sync()['status'],'completed');self.assertEqual(self.counts(),steady)
        self.assertEqual(self.item('legacy')['category'],'剧情')

    def test_archive_survives_scan_sync_restart_and_restore_unpublished(self):
        self.second_episode();self.scan();self.sync();self.sync()
        ids=[m['id'] for m in self.store.state()['media']]
        with self.store.db() as db:
            for row in db.execute('SELECT id,body FROM media').fetchall():
                item=json.loads(row['body']);item['_created_at']=12345;item['_pinned']=True
                db.execute('UPDATE media SET body=? WHERE id=?',(json.dumps(item),row['id']))
        result=self.store.batch_media({'ids':ids,'action':'archive'},'test',self.store.state()['revision'])
        self.assertEqual(result['ok'],ids);self.assertFalse(self.store.catalog({'id':'test'})['items'])
        archived={m['id']:m['_archived_at'] for m in self.store.state()['media']};modified={m['id']:m['_updated_at'] for m in self.store.state()['media']};steady=self.counts()
        self.assertEqual(self.scan()['status'],'completed');self.assertEqual(self.counts(),steady)
        for _ in range(2):self.assertEqual(self.sync()['status'],'completed');self.assertEqual(self.counts(),steady)
        reopened=Store(self.data,self.media,self.config)
        self.assertEqual(reopened.state()['revision'],steady[0])
        for item in reopened.state()['media']:
            self.assertTrue(item['_archived']);self.assertFalse(item['published']);self.assertTrue(item['_pinned'])
            self.assertEqual((item['_created_at'],item['_archived_at'],item['_updated_at']),(12345,archived[item['id']],modified[item['id']]))
        ident=ids[0]
        with self.assertRaises(Problem):self.store.save_media(ident,{'published':True},'test',self.store.state()['revision'])
        failed=self.store.batch_media({'ids':[ident],'action':'publish'},'test',self.store.state()['revision'])
        self.assertFalse(failed['ok']);self.assertIn('归档',failed['failed'][0]['reason'])
        self.store.batch_media({'ids':[ident],'action':'restore'},'test',self.store.state()['revision'])
        self.assertFalse(self.item(ident)['_archived']);self.assertFalse(self.item(ident)['published'])

    def test_sync_creation_modified_times_legacy_and_steady_rounds(self):
        with patch('management.now_ms',return_value=10000):self.assertEqual(self.sync()['status'],'completed')
        show=next(m for m in self.store.state()['media'] if m['kind']=='series');ident=show['id']
        self.assertEqual((show['_created_at'],show['_updated_at']),(10000,10000))
        self.assertNotIn('_created_at',self.item('legacy'));self.assertEqual(self.item('legacy')['_updated_at'],10000)
        steady=self.counts()
        for stamp in (20000,30000,40000):
            with patch('management.now_ms',return_value=stamp):self.assertEqual(self.sync()['status'],'completed')
            self.assertEqual(self.counts(),steady);self.assertEqual(self.item(ident),show)
        self.episode['Name']='来自上游的新分集名称'
        with patch('management.now_ms',return_value=50000):self.assertEqual(self.sync()['status'],'completed')
        edited=self.item(ident);self.assertEqual(edited['episodes'][0]['title'],self.episode['Name'])
        self.assertEqual((edited['_created_at'],edited['_updated_at']),(10000,50000));self.assertEqual(self.item('legacy')['_updated_at'],10000)
        steady=self.counts()
        with patch('management.now_ms',return_value=60000):self.assertEqual(self.sync()['status'],'completed')
        self.assertEqual(self.item(ident),edited);self.assertEqual(self.counts(),steady)
        (self.media/'film.mp4').unlink()
        with patch('management.now_ms',return_value=70000):self.assertEqual(self.sync()['status'],'completed')
        missing=self.item('legacy');self.assertFalse(missing['available']);self.assertEqual(missing['_updated_at'],70000);self.assertNotIn('_created_at',missing)
        steady=self.counts()
        with patch('management.now_ms',return_value=80000):self.assertEqual(self.sync()['status'],'completed')
        self.assertEqual(self.item('legacy'),missing);self.assertEqual(self.counts(),steady)
        (self.media/'film.mp4').write_bytes(b'film-data')
        with patch('management.now_ms',return_value=90000):self.assertEqual(self.sync()['status'],'completed')
        self.assertTrue(self.item('legacy')['available']);self.assertEqual(self.item('legacy')['_updated_at'],90000);self.assertNotIn('_created_at',self.item('legacy'))
        reopened=Store(self.data,self.media,self.config)
        self.assertEqual(reopened.state()['media'],self.store.state()['media']);self.assertEqual(reopened.state()['revision'],self.counts()[0])

    def test_import_enriches_without_duplicate_and_preserves_publication(self):
        self.assertEqual(self.sync()['status'],'completed');state=self.store.state()
        self.assertEqual(len(state['media']),2)
        old=next(m for m in state['media'] if m['id']=='legacy');self.assertTrue(old['published']);self.assertEqual(old['title'],'酒店自定片名')
        self.assertEqual(old['description'],'来自 Jellyfin');self.assertEqual(old['year'],'2026')
        self.assertTrue(self.store.media_path(old['poster']).is_file())
        show=next(m for m in state['media'] if m['kind']=='series');self.assertFalse(show['published']);self.assertEqual(show['episodes'][0]['path'],'/media/S01E01.mp4')
        self.assertNotIn('private-jellyfin-key',json.dumps(state));self.assertNotIn('private-jellyfin-key',json.dumps(self.store.catalog({'id':'test'})))
        self.movie['Overview']='更新后的简介';self.sync();updated=next(m for m in self.store.state()['media'] if m['id']=='legacy')
        self.assertEqual(updated['description'],'更新后的简介');self.assertEqual(updated['title'],'酒店自定片名')
        self.assertEqual(len(self.store.state()['media']),2)
    def test_outage_and_unmapped_path_preserve_existing_catalog(self):
        self.sync();before=self.store.catalog({'id':'test'})
        self.upstream_failed=True;self.assertEqual(self.sync()['status'],'failed');self.assertEqual(self.store.catalog({'id':'test'}),before)
        self.upstream_failed=False;self.movie['Path']='/etc/passwd';job=self.sync();self.assertGreaterEqual(job['skipped'],1)
        self.assertEqual(len(self.store.state()['media']),2)
    def test_connection_is_loopback_only_and_source_escape_rejected(self):
        for url in ['http://example.com:8096','http://127.0.0.1:8096/evil','http://name:secret@127.0.0.1:8096']:
            (self.data/'nas-integration.json').write_text(json.dumps({'url':url,'token':'secret'}))
            with self.assertRaises(Problem):self.store.jellyfin_config()
        with self.assertRaises(Problem):self.store.jellyfin_path('/etc/passwd')
        (self.media/'escape.mp4').symlink_to('/etc/passwd')
        with self.assertRaises(Problem):self.store.jellyfin_path(str(self.media/'escape.mp4'))

    def test_duplicate_upstream_path_uses_first_without_revision_churn(self):
        duplicate={**self.movie,'Id':'e'*32,'Name':'重复文件第二项'}
        self.store.jellyfin_items=lambda kind:{'Movie':[self.movie,duplicate],'Series':[self.show],'Episode':self.episodes}[kind]
        first=self.sync();self.assertEqual(first['status'],'completed');self.assertTrue(any('重复' in r['reason'] for r in first['skipped_reasons']))
        steady=self.counts()
        for _ in range(3):self.assertEqual(self.sync()['status'],'completed');self.assertEqual(self.counts(),steady)
        self.assertEqual(len(self.store.state()['media']),2)

    def test_episode_disappears_rescans_and_returns_with_manual_identity(self):
        self.second_episode();self.sync();show=next(i for i in self.store.state()['media'] if i['kind']=='series');ident=show['id']
        edited=[{**e,'title':'酒店第二集标题','season':3,'number':4} if e['path'].endswith('S01E02.mp4') else e for e in show['episodes']]
        self.store.save_media(ident,{'episodes':edited},'test',self.store.state()['revision']);before=next(e for e in edited if e['path'].endswith('S01E02.mp4'))
        file=self.media/'S01E02.mp4';content=file.read_bytes();file.unlink();self.scan();self.sync();file.write_bytes(content);self.scan()
        for _ in range(2):self.assertEqual(self.sync()['status'],'completed')
        after=next(e for e in self.item(ident)['episodes'] if e['path'].endswith('S01E02.mp4'))
        self.assertEqual(after,before);steady=self.counts();self.sync();self.assertEqual(self.counts(),steady)

if __name__=='__main__':unittest.main()
