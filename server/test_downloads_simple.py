"""DL10: actual files through HTTP doubles; download-only, multiple results and safe folder selection."""
import base64
import json
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

import test_downloads as old
from downloads_net import make_torrent, torrent_info
from import_media import scan


class SimpleDownloads(unittest.TestCase):
    for _name in ('request','state','op','login_as','setUp','tearDown','configure','clip','offer','task','wait','act'):
        locals()[_name]=getattr(old.DownloadE2E,_name)

    def setUp(self):
        old.DownloadE2E.setUp(self)
        # Production may already have a root registry; isolated tests must never read it.
        self.store.download_roots_file=self.root/'download-roots.json'

    def create_new(self, **body):
        status,_,result=self.op('download','download_create',**body)
        self.assertEqual(status,200,result)
        return result['id']

    def test_plain_chinese_keywords_and_filenames_keep_their_original_text(self):
        self.configure();self.store.stop_download_workers();self.store.start_download_workers=lambda:None
        for query in ('不一样的天空','资源下载清单2024.txt','电影的未来.txt'):
            ident=self.create_new(request=query)
            task=self.task(ident);self.assertEqual(task['parsed']['title'],query);self.assertEqual(task['parsed']['kind'],'')
            self.assertEqual(task['state'],'searching')

    def test_plain_file_default_needs_no_media_fields_and_never_calls_ffprobe(self):
        self.configure();blob=self.src/'manual.zip';blob.write_bytes(b'zip-file-content' * 1024)
        url='https://files.example.invalid/package.zip';self.hub.payloads[url]=str(blob)
        self.store._ffprobe=Mock(side_effect=AssertionError('download-only must not probe video'))
        before=[m['id'] for m in self.state()['media']]
        ident=self.create_new(manual={'uri':url})
        task=self.wait(ident,('downloaded','failed'),20)
        self.assertEqual(task['state'],'downloaded',task['note']);self.assertEqual(task['engine'],'aria2')
        self.assertFalse(task['organize']);self.assertFalse(task['publish_after']);self.assertNotIn('media_id',task)
        self.assertEqual(Path(task['saved_files'][0]['path']).read_bytes(),blob.read_bytes())
        self.assertEqual([m['id'] for m in self.state()['media']],before);self.store._ffprobe.assert_not_called()
        self.assertTrue((Path(task['final_path'])/'.ignore').exists())

    def test_uploaded_torrent_and_magnet_download_any_file(self):
        self.configure();blob=self.src/'data.bin';blob.write_bytes(b'raw-torrent-data' * 2000)
        raw=make_torrent(blob);info=torrent_info(raw);self.hub.payloads[info['infohash']]=str(blob)
        ident=self.create_new(manual={'torrent_b64':base64.b64encode(raw).decode()})
        task=self.wait(ident,('downloaded','failed'),20)
        self.assertEqual(task['state'],'downloaded',task['note']);self.assertEqual(task['engine'],'qbittorrent')
        self.assertEqual(Path(task['saved_files'][0]['path']).read_bytes(),blob.read_bytes())
        # A different raw file via magnet also needs neither a film title nor a media type.
        blob2=self.src/'notes.txt';blob2.write_bytes(b'notes' * 1000);h=torrent_info(make_torrent(blob2))['infohash'];self.hub.payloads[h]=str(blob2)
        ident2=self.create_new(manual={'uri':'magnet:?xt=urn:btih:'+h})
        t2=self.wait(ident2,('downloaded','failed'),20)
        self.assertEqual(t2['state'],'downloaded',t2['note']);self.assertEqual(Path(t2['saved_files'][0]['path']).read_bytes(),blob2.read_bytes())

    def test_search_without_kind_has_similar_results_and_each_selection_is_a_new_download(self):
        self.configure();one=self.src/'one.bin';one.write_bytes(b'one'*1000);two=self.src/'two.bin';two.write_bytes(b'two'*1000)
        self.offer('Useful.Package.2024.1080p',one);self.offer('Useful.Package.2019.720p',two)
        ident=self.create_new(request='Useful Package',overrides={'year':2024,'resolution':'1080p'})
        result=self.wait(ident,('awaiting_choice','failed'),20)
        self.assertEqual(result['state'],'awaiting_choice',result['note']);self.assertFalse(result['parsed']['questions'])
        self.assertEqual({c['match'] for c in result['candidates']},{'match','similar'})
        self.assertEqual(result['candidates'][0]['attrs']['year'],2024)
        ids=[]
        for c in result['candidates']:
            self.assertTrue(c['can_download'])
            response=self.act(ident,'download_candidate',key=c['key'])
            self.assertEqual(response[0],200,response[2]);ids.append(response[2]['id'])
        self.assertEqual(self.task(ident)['state'],'awaiting_choice');self.assertEqual(len(set(ids)),2)
        for child in ids:self.assertEqual(self.wait(child,('downloaded','failed'),20)['state'],'downloaded')
        self.assertEqual(self.hub.calls['qb_add'],2)

    def test_new_folder_default_and_task_frozen_mapping_survive_default_change(self):
        self.configure();root=Path(self.store.download_settings_view()['settings']['staging_dir']);root.mkdir(exist_ok=True)
        locations=self.request('/api/admin/operations?section=download_locations',auth=True)[2]
        self.assertTrue(locations['writable']);self.assertEqual(locations['root'],'default')
        status,_,folder=self.op('download','download_mkdir',root='default',path=str(root),name='资料下载')
        self.assertEqual(status,200,folder);selected=folder['path'];self.assertTrue(Path(selected).is_dir())
        self.assertEqual(Path(selected).stat().st_mode & 0o6000,0,'service forbids set-ID permission bits')
        self.assertEqual(self.op('download','download_settings',settings={'save_dir':selected})[0],200)
        blob=self.src/'first.txt';blob.write_bytes(b'first'*2000);uri='https://files.example.invalid/first.txt';self.hub.payloads[uri]=str(blob);self.hub.seconds=2
        ident=self.create_new(manual={'uri':uri});self.wait(ident,('downloading',),15)
        second=self.op('download','download_mkdir',root='default',path=str(root),name='以后下载')[2]['path']
        self.assertEqual(self.op('download','download_settings',settings={'save_dir':second})[0],200)
        task=self.wait(ident,('downloaded','failed'),20)
        self.assertEqual(task['state'],'downloaded',task['note']);self.assertTrue(Path(selected) in Path(task['final_path']).parents)
        self.assertEqual(Path(task['final_path']).stat().st_mode & 0o7000,0o1000,'sticky task folder, no set-ID bits')
        self.assertFalse(list(Path(second).iterdir()))
        view=self.request('/api/admin/operations?section=downloads',auth=True)[2]
        self.assertEqual(view['default_dir'],second);self.assertNotIn('engine_paths',json.dumps(view))

    def test_path_escape_symlinks_and_folder_creation_permissions_are_rejected(self):
        self.configure();root=Path(self.store.download_settings_view()['settings']['staging_dir']);root.mkdir(exist_ok=True)
        outside=self.root/'outside';outside.mkdir();(root/'escape').symlink_to(outside,target_is_directory=True)
        for value in (str(root/'..'/'outside'),str(root/'escape'),str(outside)):
            self.assertEqual(self.request('/api/admin/operations?section=download_locations&path='+value,auth=True)[0],400,value)
        for name in ('..','../outside','a/b'):
            self.assertEqual(self.op('download','download_mkdir',root='default',path=str(root),name=name)[0],400)
        operator=self.login_as('folderop','operator')
        self.assertEqual(self.op('download','download_mkdir',headers=operator,root='default',path=str(root),name='forbidden')[0],403)
        self.assertFalse((root/'forbidden').exists())

    def test_existing_foreign_directory_is_not_claimed_and_changed_disk_never_starts(self):
        self.configure();self.store.stop_download_workers();self.store.start_download_workers=lambda:None
        blob=self.src/'data.zip';blob.write_bytes(b'a'*2048);uri='https://files.example.invalid/data.zip';self.hub.payloads[uri]=str(blob)
        ident=self.create_new(manual={'uri':uri});task=self.task(ident)
        path=Path(task['storage']['path'])/task['folder_name'];path.mkdir(parents=True);(path/'keep.txt').write_text('keep')
        settings=self.store.task_download_settings(task,self.store.download_settings_view()['settings'])
        with self.assertRaisesRegex(Exception,'不属于'):self.store._dl_start(task,settings)
        self.assertFalse((path/'.hotel-download-task').exists());self.assertEqual((path/'keep.txt').read_text(),'keep')
        (path/'keep.txt').unlink();path.rmdir();task['storage']['device']+=1
        with self.assertRaisesRegex(Exception,'硬盘'):self.store._dl_start(task,settings)
        self.assertEqual(self.hub.calls['aria_add'],0)

    def test_registered_disk_mapping_and_unmounted_disk_options(self):
        self.configure();mount=self.root/'disk';mount.mkdir();child=mount/'保存';child.mkdir()
        config=self.root/'download-roots.json';config.write_text(json.dumps({'roots':[{'id':'mapped','name':'可选硬盘','path':str(mount.resolve()),'engine_paths':{'qbittorrent':'/disk','aria2':'/disk'}}]}))
        self.store.download_roots_file=config
        with patch('downloads_storage.snapshot',return_value={'devices':[{'name':'sdb','serial':'unmounted','system':False,'partitions':[],'mounts':[],'size':16000}]}):
            result=self.store.download_locations({'path':[str(child.resolve())]})
            self.assertTrue(any(not r['available'] and '未挂载' in r['name'] for r in result['roots']))
            storage=self.store.freeze_download_location(str(child.resolve()))
            self.assertEqual(storage['engine_paths']['qbittorrent'],'/disk/保存')
            settings=self.store.task_download_settings({'storage':storage,'folder_name':'job'},self.store.download_settings_view()['settings'])
            self.assertEqual(self.store.engine_dir(settings,'qbittorrent','unused'),'/disk/保存/job')
            self.assertEqual(self.store.host_path(settings,'qbittorrent','/disk/保存/job/a.zip'),child.resolve()/'job/a.zip')

    def test_two_mount_paths_for_same_volume_keep_distinct_picker_ids(self):
        self.configure();root=self.root/'mounts';root.mkdir();one=root/'one';two=root/'two';one.mkdir();two.mkdir()
        config=self.root/'roots.json';config.write_text(json.dumps({'roots':[{'id':'mnt','path':str(root.resolve()),'engine_paths':{'qbittorrent':'/mnt','aria2':'/mnt'}}]}));self.store.download_roots_file=config
        disk={'name':'sdb','serial':'disk','system':False,'partitions':[{'name':'sdb1','uuid':'same','label':'Data','readonly':False,'mounts':[str(one.resolve()),str(two.resolve())]}]}
        with patch('downloads_storage.snapshot',return_value={'devices':[disk]}):
            result=self.store.download_locations({'path':[str(root.resolve())]})
            aliases=[r for r in result['roots'] if r['id'].startswith('volume-')]
            self.assertEqual(len({r['id'] for r in aliases}),2)
            storage=self.store.freeze_download_location(str(two.resolve()))
            self.assertEqual(storage['engine_paths']['qbittorrent'],'/mnt/two')

    @unittest.skipUnless(old.FFMPEG,'ffmpeg required')
    def test_explicit_import_and_publish_do_not_depend_on_legacy_auto_mode(self):
        self.configure(auto=False);clip=self.clip('chosen.mp4',2,'1920x1080')
        uri='https://files.example.invalid/chosen.mp4';self.hub.payloads[uri]=str(clip)
        ident=self.create_new(manual={'uri':uri,'title':'Chosen Film','kind':'movie'},organize=True,publish=True)
        task=self.wait(ident,('ready','failed'),40)
        self.assertEqual(task['state'],'ready',task['note']);self.assertTrue(task['published']);self.assertFalse(task['auto'])

    @unittest.skipUnless(old.FFMPEG,'ffmpeg required')
    def test_downloaded_video_is_not_scanned_until_opt_in_import(self):
        self.configure(extra={'min_video_seconds':1});clip=self.clip('downloaded.mp4',2,'1920x1080')
        uri='https://files.example.invalid/Optional.Video.mp4';self.hub.payloads[uri]=str(clip)
        ident=self.create_new(manual={'uri':uri,'title':'Optional Video'})
        task=self.wait(ident,('downloaded','failed'),20);self.assertEqual(task['state'],'downloaded',task['note'])
        self.assertFalse(any('Optional' in m['title'] for m in scan(self.store.media)['items']))
        # The folder marker is honoured even if someone explicitly scans that download folder.
        self.assertEqual(scan(Path(task['final_path']))['items'],[])
        self.assertEqual(self.act(ident,'organize',overrides={'kind':'movie','title':'Optional Video'},publish=False)[0],200)
        task=self.wait(ident,('ready','failed'),40)
        self.assertEqual(task['state'],'ready',task['note']);self.assertFalse(task['published']);self.assertTrue(task['media_id'])


if __name__=='__main__':unittest.main()
