"""DL11: public Chinese API pages, mount identities, protected folders and broad result sets."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import downloads_sources as sources
import download_locations_deploy as provision
from downloads_net import NetError
import test_downloads_simple as simple


class BTSearchContract(unittest.TestCase):
    def row(self, n):
        return {'name': "[<span class='highlight'>检索样本</span>%d].2024.1080p" % n, 'hash': '%040x' % (n+1),
                'size': '123456789', 'count': 5, 'hot': 54, 'created_at': '2024-01-01T00:00:00Z'}

    def test_public_pages_and_html_names_are_normalized_without_inventing_seeders(self):
        seen=[]
        def fetch(url, payload=None, headers=None):
            q=parse_qs(urlsplit(url).query,keep_blank_values=True);seen.append(int(q['offset'][0]));self.assertEqual(q['keyword'][0],'检索样本')
            self.assertRegex(headers['x-sign'],r'^[0-9A-F]{32}$');start=int(q['offset'][0]);end=min(279,start+int(q['limit'][0]))
            return json.dumps({'total':279,'data':[self.row(i) for i in range(start,end)]}).encode()
        adapter=sources.build({'id':'bt','type':'btsearch','url':'https://www.btsearch.love'},'')
        with patch.object(adapter,'_fetch',side_effect=fetch):rows=adapter.search('检索样本','',300)
        self.assertEqual(seen,[0,100,200]);self.assertEqual(len(rows),279);self.assertEqual(adapter.total,279)
        self.assertNotIn('<span',rows[0]['title']);self.assertIn('检索样本',rows[0]['title']);self.assertEqual(rows[0]['file_count'],5)
        self.assertIsNone(rows[0]['seeders']);self.assertEqual(rows[0]['popularity'],54);self.assertTrue(rows[0]['magnet'].startswith('magnet:'))

    def test_partial_later_page_keeps_usable_results_and_invalid_hash_is_not_a_download(self):
        adapter=sources.build({'id':'bt','type':'btsearch','url':'https://www.btsearch.love'},'')
        with patch.object(adapter,'_fetch',side_effect=[json.dumps({'total':200,'data':[self.row(i) for i in range(100)]}).encode(),NetError('timeout','timeout')]):
            self.assertEqual(len(adapter.search('检索样本','',300)),100);self.assertIn('后续页',adapter.warning)
        with patch.object(adapter,'_fetch',return_value=json.dumps({'total':1,'data':[dict(self.row(0),hash='not-a-hash')]}).encode()):
            self.assertEqual(adapter.search('检索样本',''),[])
        with patch.object(adapter,'_fetch',return_value=b'<html>not JSON</html>'):
            with self.assertRaises(NetError):adapter.search('检索样本','')

    def test_custom_volume_plan_retains_existing_roots_and_rejects_system_or_wrong_identity(self):
        extra={'id':'disk-e','name':'E盘 · 电影','path':'/E','mount_uuid':'movie-uuid'}
        devices=[{'system':False,'partitions':[{'uuid':'movie-uuid','mounts':['/E'],'readonly':False}]}]
        roots=provision.location_specs([],extra,devices);self.assertEqual(len(roots),6)
        self.assertEqual(provision.location_specs(roots,None,devices),roots)
        config={'services':{'aria2':{'user':'997:984','volumes':[{'source':'/old','target':'/downloads','type':'bind'}]}}}
        plan=provision.mapped_config(config,'aria2',984,[(r['id'],r['name'],r['path']) for r in roots])
        self.assertEqual(plan['services']['aria2']['volumes'][-1]['target'],'/hotel-storage/disk-e')
        for value in ([],[{'system':True,'partitions':devices[0]['partitions']}],[{'system':False,'partitions':[{'uuid':'wrong','mounts':['/E'],'readonly':False}]}]):
            with self.assertRaises(ValueError):provision.location_specs([],extra,value)


class DL11Operations(unittest.TestCase):
    for _name in ('request','state','op','login_as','setUp','tearDown','configure','clip','offer','task','wait','act','create_new'):
        locals()[_name]=getattr(simple.SimpleDownloads,_name)

    def test_custom_mount_disappearing_readonly_or_replaced_never_uses_underlying_directory(self):
        self.configure();disk=self.root/'E';disk.mkdir();registry=self.root/'roots.json'
        registry.write_text(json.dumps({'roots':[{'id':'disk-e','name':'E盘','path':str(disk.resolve()),'mount_uuid':'expected','engine_paths':{'qbittorrent':'/disk-e','aria2':'/disk-e'}}]}));self.store.download_roots_file=registry
        def device(uuid='expected',mounted=True,readonly=False):return {'devices':[{'name':'sda','serial':'e','system':False,'partitions':[{'name':'sda1','label':'电影','uuid':uuid,'readonly':readonly,'mounts':[str(disk.resolve())] if mounted else []}]}]}
        with patch('downloads_storage.snapshot',return_value=device()):
            roots=self.store.download_locations({})['roots'];self.assertEqual(len(roots),1);self.assertTrue(roots[0]['available'])
            frozen=self.store.freeze_download_location(str(disk.resolve()))
        for state in (device(mounted=False),device(uuid='other'),device(readonly=True)):
            with patch('downloads_storage.snapshot',return_value=state):
                self.assertFalse(self.store._location_roots()[0]['available'])
                with self.assertRaises(Exception):self.store.guard_download_location(frozen)
                with self.assertRaises(Exception):self.store.freeze_download_location(str(disk.resolve()))
        self.assertTrue(disk.is_dir())

    def test_protected_lost_found_does_not_break_the_whole_volume_browser(self):
        self.configure();root=Path(self.store.download_settings_view()['settings']['staging_dir']);root.mkdir(exist_ok=True)
        blocked=root/'lost+found';blocked.mkdir();(root/'下载').mkdir();exists=Path.exists
        def checked(path):
            if path==blocked/'.hotel-download-task':raise PermissionError('protected')
            return exists(path)
        with patch.object(Path,'exists',checked):result=self.store.download_locations({'path':[str(root)]})
        self.assertEqual({r['name'] for r in result['folders']},{'lost+found','下载'})

    def test_broad_query_keeps_more_than_sixty_results_and_preserves_criteria_locally(self):
        self.configure();self.store.stop_download_workers();self.store.start_download_workers=lambda:None
        ident=self.create_new(request='检索样本',overrides={'year':2024,'resolution':'2160p'})
        rows=[sources.candidate('mock','[发布]检索样本%d.2024.%s' % (i,'2160p' if i%2 else '1080p'),1024,magnet='magnet:?xt=urn:btih:%040x'%(i+1)) for i in range(125)]
        with patch('operations_downloads.sources.search_all',return_value=(rows,[{'id':'mock','name':'Mock','status':'ok','count':125}] )) as search,patch.object(self.store,'_dl_running',return_value=True):
            self.store._dl_search(self.task(ident),self.store.download_settings_view()['settings'])
        task=self.task(ident);self.assertEqual(len(task['candidates']),125);self.assertEqual(task['state'],'awaiting_choice')
        self.assertEqual({c['match'] for c in task['candidates']},{'match','similar'});self.assertTrue(all(c['can_download'] for c in task['candidates']))
        self.assertEqual(search.call_args.args[1],'检索样本');self.assertEqual(search.call_args.kwargs['limit'],300)


if __name__=='__main__':unittest.main()
