"""Real HTTP and disposable-file checks: popularity, protection and permanent cleanup."""
import json
import os
from unittest import TestCase
from unittest.mock import patch
import test_management as fixture
from management import now_ms, password_hash
from media_cleanup import DAY


class MediaCleanupE2E(TestCase):
    setUp = fixture.ManagementE2E.setUp
    tearDown = fixture.ManagementE2E.tearDown
    request = fixture.ManagementE2E.request
    state = fixture.ManagementE2E.state
    mutate = fixture.ManagementE2E.mutate

    def old(self, ident='existing', **extra):
        with self.store.db() as db:
            row = db.execute('SELECT body FROM media WHERE id=?', (ident,)).fetchone()
            item = json.loads(row[0]); item.update(_created_at=now_ms()-40*DAY, **extra)
            db.execute('UPDATE media SET body=? WHERE id=?', (json.dumps(item), ident))
        return item

    def event(self, ident, ago, session='session', device='room-101', **extra):
        e = {'id': ident, 'type': 'content_started', 'content_id': 'existing', 'client_session_id': session, 'at_ms': now_ms()-ago*DAY, **extra}
        with self.store.db() as db: db.execute('INSERT INTO events VALUES(?,?,?,?)', (ident, device, now_ms()-ago*DAY, json.dumps(e)))

    def insights(self): return self.request('/api/admin/media-insights', auth=True)[2]
    def preview(self, ids=None): return self.mutate('/media-cleanup/preview', {'ids': ids or ['existing']})
    def execute(self, plan, **extra): return self.mutate('/media-cleanup/execute', {'plan_id': plan['id'], 'confirm': True, **extra})

    def test_30_day_dedup_window_series_aggregate_and_recent_time(self):
        self.old(kind='series', path='', episodes=[{'id':'ep1','path':'/media/clip.mp4'}, {'id':'ep2','path':'/media/clip.mp4'}])
        for ident, ago, session, content in [('older',35,'old','ep1'),('a',4,'one','ep1'),('restore',4,'one','ep1'),('b',3,'two','ep1'),('c',2,'one','ep2')]:
            self.event(ident, ago, session, content_id=content)
        self.event('probe',1,'probe', content_id='ep2', exclude_from_ad_reports=True)
        row = self.insights()['rows'][0]
        self.assertEqual(row['plays_30'],3); self.assertTrue(row['eligible']); self.assertGreater(row['last_played_at'], now_ms()-3*DAY)

    def test_new_unknown_pinned_recent_and_references_are_protected(self):
        self.assertFalse(self.insights()['rows'][0]['eligible'])
        self.old(_pinned=True); self.assertIn('置顶保护',self.insights()['rows'][0]['protected_reasons'])
        self.old(_pinned=False); self.event('recent',0)
        self.assertFalse(self.insights()['rows'][0]['eligible'])
        with self.store.db() as db: db.execute('DELETE FROM events')
        with self.store.db() as db:
            item=self.store.record(db,'media','existing');item['_created_at']=now_ms()-2*DAY;self.store.write_record(db,'media',item)
        self.assertIn('新内容保护：入库不足7天',self.insights()['rows'][0]['protected_reasons'])
        self.old()
        with self.store.db() as db:self.store.put_object(db,'topic',{'id':'topic','name':'精选','media_ids':['existing']})
        self.assertFalse(self.insights()['rows'][0]['eligible']);self.assertEqual(self.preview()[2]['rows'],[])

    def test_preview_is_nondestructive_execute_removes_only_bound_file_retains_events_tombstone(self):
        self.old();self.event('history',10);(self.media/'poster.jpg').write_bytes(b'keep')
        before=self.state()['revision'];code,_,p=self.preview();self.assertEqual(code,200,p)
        self.assertEqual(self.state()['revision'],before);self.assertTrue((self.media/'clip.mp4').exists());self.assertEqual(p['size_bytes'],10)
        code,_,r=self.execute(p);self.assertEqual(code,200,r);self.assertEqual(r['status'],'completed');self.assertEqual(r['deleted_files'],1)
        self.assertFalse((self.media/'clip.mp4').exists());self.assertTrue((self.media/'poster.jpg').exists());self.assertEqual(self.state()['media'],[])
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],1)
            self.assertTrue(self.store.objects(db,'media_tombstone'))
        self.assertEqual(self.execute(p)[0],409)

    def test_revision_expiry_confirmation_recent_play_and_file_replacement_block_execution(self):
        self.old();p=self.preview()[2]
        self.assertEqual(self.execute(p,confirm=False)[0],400)
        (self.media/'clip.mp4').write_bytes(b'new bytes, do not delete')
        self.assertEqual(self.execute(p)[0],409);self.assertTrue((self.media/'clip.mp4').exists())
        p=self.preview()[2];self.event('new-watch',0);self.assertEqual(self.execute(p)[0],409)
        with self.store.db() as db:db.execute('DELETE FROM events')
        p=self.preview()[2]
        with self.store.db() as db:
            plan=self.store.record(db,'media_cleanup_plan',p['id']);plan['expires_at']=1;self.store.put_object(db,'media_cleanup_plan',plan)
        self.assertEqual(self.execute(p)[0],409)
        p=self.preview()[2];self.assertEqual(self.request('/api/admin/media-cleanup/execute',{'plan_id':p['id'],'confirm':True,'revision':self.state()['revision']-1},auth=True)[0],409)

    def test_symlink_and_same_file_via_different_source_are_protected(self):
        self.old();file=self.media/'clip.mp4';file.unlink();file.symlink_to(self.external/'safe.mp4');(self.external/'safe.mp4').write_bytes(b'keep')
        self.assertEqual(self.preview()[2]['rows'],[]);self.assertTrue((self.external/'safe.mp4').exists())
        file.unlink();file.write_bytes(b'original')
        with self.store.db() as db:
            item=self.store.record(db,'media','existing');item.update(id='alias',path='/media/@sources/alias/clip.mp4')
            db.execute('INSERT INTO sources VALUES(?,?)',('alias',json.dumps({'id':'alias','path':str(self.media)})))
            db.execute('INSERT INTO media VALUES(?,?,?,?,?)',('alias','alias',json.dumps(item),1,1))
        self.assertTrue(all(not r['eligible'] for r in self.insights()['rows']))

    def test_hardlink_reclaim_estimate_zero_and_ordinary_archive_keeps_file(self):
        self.old();os.link(self.media/'clip.mp4',self.media/'another.mp4')
        self.assertEqual(self.insights()['rows'][0]['reclaimable_bytes'],0)
        r=self.mutate('/media/batch',{'ids':['existing'],'action':'archive'})
        self.assertEqual(r[0],200);self.assertTrue((self.media/'clip.mp4').exists());self.assertEqual(self.preview()[2]['rows'],[])

    def test_unlink_failure_is_reported_and_journal_retained_without_claiming_space(self):
        self.old();p=self.preview()[2];real=os.unlink
        def fail(name,*args,**kwargs):
            if str(name).startswith('.hotel-cleanup-'): raise PermissionError('simulated')
            return real(name,*args,**kwargs)
        with patch('media_cleanup.os.unlink',side_effect=fail):code,_,r=self.execute(p)
        self.assertEqual(code,200,r);self.assertEqual(r['status'],'partial');self.assertEqual(r['deleted_bytes'],0);self.assertTrue(r['failed'])
        self.assertTrue((self.media/'clip.mp4').exists());self.assertTrue(self.state()['media'][0]['_archived']);self.assertEqual(self.insights()['history'][0]['status'],'partial')

    def test_permissions_csrf_plan_ownership_and_limits(self):
        self.old();self.assertEqual(self.request('/api/admin/media-insights')[0],401)
        self.assertEqual(self.request('/api/admin/media-cleanup/preview',{'ids':['existing'],'revision':self.state()['revision']},headers={'Cookie':self.cookie})[0],403)
        self.assertEqual(self.preview(['existing']*51)[0],400)
        with self.store.db() as db:db.execute('INSERT INTO users VALUES(?,?,?,1)',('op',password_hash('test-only-password'),'operator'))
        _,h,r=self.request('/api/admin/login',{'username':'op','password':'test-only-password'})
        self.assertEqual(self.request('/api/admin/media-cleanup/preview',{'ids':['existing'],'revision':self.state()['revision']},headers={'Cookie':h['Set-Cookie'].split(';')[0],'X-CSRF-Token':r['user']['csrf']})[0],403)

    def test_replacement_between_stat_and_rename_is_restored_not_deleted(self):
        self.old();p=self.preview()[2];real=os.rename
        def replace(src,dst,*args,**kwargs):
            if src=='clip.mp4':
                (self.media/'clip.mp4').unlink();(self.media/'clip.mp4').write_bytes(b'new replacement must survive')
            return real(src,dst,*args,**kwargs)
        with patch('media_cleanup.os.rename',side_effect=replace):code,_,r=self.execute(p)
        self.assertEqual(code,200,r);self.assertEqual(r['status'],'partial');self.assertEqual(r['deleted_files'],0)
        self.assertEqual((self.media/'clip.mp4').read_bytes(),b'new replacement must survive')

    def test_full_disk_before_durable_intent_prevents_any_unlink(self):
        import sqlite3
        self.old();p=self.preview()[2]
        with patch.object(self.store,'changed',side_effect=sqlite3.OperationalError('disk full')):
            self.assertEqual(self.execute(p)[0],503)
        self.assertTrue((self.media/'clip.mp4').exists());self.assertTrue(self.state()['media'][0]['published'])

    def test_parent_symlink_traversal_and_plan_ownership_are_rejected(self):
        self.old();(self.external/'keep.mp4').write_bytes(b'keep');(self.media/'link').symlink_to(self.external,target_is_directory=True)
        self.old(path='/media/link/keep.mp4');self.assertEqual(self.preview()[2]['rows'],[])
        self.old(path='/media/../libraries/keep.mp4');self.assertEqual(self.preview()[2]['rows'],[])
        self.old(path='/media/clip.mp4');p=self.preview()[2]
        with self.store.db() as db:
            plan=self.store.record(db,'media_cleanup_plan',p['id']);plan['actor']='another';self.store.put_object(db,'media_cleanup_plan',plan)
        self.assertEqual(self.execute(p)[0],403);self.assertTrue((self.media/'clip.mp4').exists())
