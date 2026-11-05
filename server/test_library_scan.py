import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch
import test_management as fixture
from management import password_hash, Problem


class MountedLibraryScan(unittest.TestCase):
    setUpFixture=fixture.ManagementE2E.setUp
    tearDownFixture=fixture.ManagementE2E.tearDown
    request=fixture.ManagementE2E.request
    state=fixture.ManagementE2E.state
    mutate=fixture.ManagementE2E.mutate

    def setUp(self):
        self.volumes=[]
        self.patch=patch('library_scan.physical_snapshot',side_effect=lambda:{'status':'ok','devices':self.volumes});self.patch.start()
        self.setUpFixture()
        self.one=(self.root/'new-volume').resolve();self.two=(self.root/'another-volume').resolve()
        for path in (self.one,self.two):path.mkdir()
        self.volumes[:]=[self.volume(self.one,'uuid-one'),self.volume(self.two,'uuid-two',readonly=True)]
        from library_scan import mounted_volumes
        mounted_volumes(refresh=True)

    def tearDown(self):
        self.tearDownFixture();self.patch.stop()

    @staticmethod
    def volume(path,uuid,readonly=False):
        return {'name':'some-disk','path':'/dev/disk','fstype':'ext4','uuid':uuid,'label':'我的盘','mounts':[str(path)],'readonly':readonly,'partitions':[]}

    def start(self,paths):
        return self.request('/api/admin/operations',{'kind':'media','action':'library_scan','paths':paths},auth=True)

    def wait(self,job):
        end=time.monotonic()+8
        while time.monotonic()<end:
            row=next(r for r in self.state()['jobs'] if r['id']==job['id'])
            if row['status'] in ('completed','failed'):return row
            time.sleep(.03)
        self.fail('library scan timeout')

    def test_detection_has_no_index_side_effect_and_selection_controls_scope(self):
        (self.one/'Show.S01E02.mkv').write_bytes(b'keep')
        (self.two/'Other.S02E03.mkv').write_bytes(b'keep')
        code,_,plan=self.request('/api/admin/operations?section=library_scan',auth=True)
        self.assertEqual(code,200);self.assertIn(str(self.one),[r['path'] for r in plan['choices']]);self.assertEqual(len(self.state()['sources']),1)
        code,_,job=self.start([str(self.one)]);self.assertEqual(code,200);self.assertEqual(self.wait(job)['status'],'completed')
        state=self.state();self.assertTrue(any(m['title']=='Show' and not m['published'] for m in state['media']));self.assertFalse(any(m['title']=='Other' for m in state['media']))
        first=next(m['id'] for m in state['media'] if m['title']=='Show')
        code,_,job=self.start([str(self.one),str(self.two)]);self.assertEqual(self.wait(job)['status'],'completed')
        self.assertEqual(next(m['id'] for m in self.state()['media'] if m['title']=='Show'),first)
        self.assertTrue(any(m['title']=='Other' and not m['published'] for m in self.state()['media']))
        self.assertEqual((self.one/'Show.S01E02.mkv').read_bytes(),b'keep')

    def test_mount_change_and_unreviewed_path_are_rejected_before_source_changes(self):
        self.request('/api/admin/operations?section=library_scan',auth=True)
        self.volumes.pop()
        self.assertEqual(self.start([str(self.two)])[0],409)
        self.assertEqual(self.start(['/etc'])[0],409)
        self.assertEqual(len(self.state()['sources']),1)

    def test_system_virtual_mounts_are_not_media_roots_but_new_data_partition_is(self):
        self.volumes.append({'name':'system-disk','fstype':'','mounts':[],'system':True,'partitions':[{'mounts':['/'],'fstype':'ext4','uuid':'os'},{'mounts':['/proc'],'fstype':'proc','uuid':'virtual'},{'mounts':[str(self.two)],'fstype':'ext4','uuid':'uuid-two'}]})
        code,_,plan=self.request('/api/admin/operations?section=library_scan',auth=True)
        paths=[r['path'] for r in plan['choices']]
        self.assertNotIn('/',paths);self.assertNotIn('/proc',paths);self.assertEqual(paths.count(str(self.two)),1)
        self.assertEqual(self.mutate('/sources',{'name':'new folder','path':str(self.two)})[0],200)

    def test_known_subdirectory_is_scanned_once_and_can_be_selected_separately(self):
        child=self.one/'chosen-folder';child.mkdir();(child/'Child.S01E01.mkv').write_bytes(b'child')
        (self.one/'Outside.S01E02.mkv').write_bytes(b'outside')
        code,_,source=self.mutate('/sources',{'name':'指定目录','path':str(child)});self.assertEqual(code,200)
        code,_,job=self.start([str(child)]);self.assertEqual(self.wait(job)['status'],'completed')
        self.assertFalse(any(m['title']=='Outside' for m in self.state()['media']))
        code,_,job=self.start([str(self.one)]);self.assertEqual(self.wait(job)['status'],'completed')
        rows=[m for m in self.state()['media'] if m['title'] in ('Child','Outside')];self.assertEqual(len(rows),2)
        self.assertEqual(next(m['source_id'] for m in rows if m['title']=='Child'),source['id'])
        self.assertEqual(len({e['path'] for m in rows for e in m['episodes']}),2)
        added=self.one/'second-folder';added.mkdir()
        self.assertEqual(self.mutate('/sources',{'name':'另一个指定目录','path':str(added)})[0],200)


    def test_transient_inventory_failure_preserves_existing_playback_and_blocks_new_disk_scan(self):
        (self.one/'Show.S01E01.mkv').write_bytes(b'video')
        code,_,job=self.start([str(self.one)]);self.assertEqual(self.wait(job)['status'],'completed')
        episode=next(m for m in self.state()['media'] if m['title']=='Show')['episodes'][0]
        with patch('library_scan.physical_snapshot',return_value={'status':'unavailable','devices':[],'message':'probe failed'}):
            code,_,plan=self.request('/api/admin/operations?section=library_scan',auth=True)
            self.assertEqual(plan['inventory_status'],'unavailable')
            self.assertEqual(self.start([str(self.one)])[0],503)
            self.assertTrue(self.store.media_path(episode['path']).is_file())


    def test_replaced_volume_cannot_serve_the_old_sources_files(self):
        video=self.one/'Show.S01E01.mkv';video.write_bytes(b'original')
        code,_,source=self.mutate('/sources',{'name':'指定数据盘','path':str(self.one)})
        self.assertEqual(source['_mount_uuid'],'uuid-one')
        code,_,job=self.start([str(self.one)]);self.assertEqual(self.wait(job)['status'],'completed')
        item=next(m for m in self.state()['media'] if m['title']=='Show')
        route=item['episodes'][0]['path']
        if hasattr(super(type(self.store),self.store),'cleanup_files'):
            with self.store.db() as db:self.assertEqual(len(self.store.cleanup_files(db,item)),1)
        self.volumes[0]['uuid']='different-disk'
        self.request('/api/admin/operations?section=library_scan',auth=True)
        with self.assertRaises(Problem):self.store.media_path(route)
        with self.store.db() as db:
            with self.assertRaises(Problem):self.store.cleanup_files(db,item)
        self.assertFalse(next(s for s in self.state()['sources'] if s['id']==source['id'])['readable'])

    def test_operator_and_viewer_cannot_discover_or_enroll_new_disks(self):
        for role in ('operator','viewer'):
            with self.store.db() as db:db.execute('INSERT INTO users VALUES(?,?,?,1)',(role,password_hash('scan-role-test'),role))
            _,headers,_=self.request('/api/admin/login',{'username':role,'password':'scan-role-test'})
            cookie=headers['Set-Cookie'].split(';')[0]
            self.assertEqual(self.request('/api/admin/operations?section=library_scan',headers={'Cookie':cookie})[0],403)
        self.assertEqual(len(self.state()['sources']),1)


if __name__=='__main__':unittest.main()
