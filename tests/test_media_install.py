import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('media_install',ROOT/'deploy/nas/media_automation.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class MediaInstallTests(unittest.TestCase):
    def test_native_form_repeats_language_values_instead_of_sending_a_json_string(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs
        import threading
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode()))
                self.send_response(204); self.end_headers()
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            client = m.Native('http://127.0.0.1:%d' % server.server_port)
            client.call('/settings', {'languages-enabled': ['zh', 'zt'], 'languages-profiles': '[{"profileId":1}]'}, 'POST', form=True)
            self.assertEqual(received, [{'languages-enabled': ['zh', 'zt'], 'languages-profiles': ['[{"profileId":1}]']}])
        finally: server.shutdown(); server.server_close(); thread.join()

    def test_hardlink_probe_keeps_existing_files_and_cleans_only_own_probes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old=root/'owner-media.mkv';old.write_bytes(b'keep')
            before=old.stat();result=m.hardlink_probe(root)
            self.assertTrue(result['hardlink']);self.assertEqual(old.read_bytes(),b'keep')
            self.assertEqual(old.stat().st_ino,before.st_ino);self.assertEqual(list(root.iterdir()),[old])
            with patch.object(m.os,'link',side_effect=OSError('no hardlinks')):
                with self.assertRaises(OSError):m.hardlink_probe(root)
            self.assertEqual(list(root.iterdir()),[old])

    def test_foreign_storage_stops_before_permissions_or_existing_data_are_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'movie.mkv').write_bytes(b'original')
            s=object.__new__(m.Setup);s.storage=root;s.owner='new-install'
            with patch.object(m.os,'chown') as chown:
                with self.assertRaises(RuntimeError):s.storage_prepare()
                chown.assert_not_called()
            self.assertEqual((root/'movie.mkv').read_bytes(),b'original')
            self.assertEqual(len(list(root.iterdir())),1)

    def test_storage_paths_reject_system_root_and_configuration_injection(self):
        for path in ['/', '/etc','relative','/mnt/../etc','/mnt/a\nExecStart=x','/mnt/%n','/mnt/"x']:
            with self.subTest(path=path),self.assertRaises(ValueError):m.validate_storage(path)
        self.assertEqual(m.validate_storage('/mnt/电影/data'),'/mnt/电影/data')
        self.assertEqual(m.validate_storage('/srv/hotel-tv/media/.automation'),'/srv/hotel-tv/media/.automation')
        for path in ['/srv/media','/data/hotel','/mnt']:   # the cinema service could not read media there
            with self.subTest(path=path),self.assertRaises(ValueError):m.validate_storage(path)

    def test_resuming_with_missing_storage_marker_does_not_recreate_unmounted_media_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=object.__new__(m.Setup);s.storage=Path(tmp)/'missing-disk/data';s.status={'completed':['storage']};s.owner='test'
            with self.assertRaises(RuntimeError):s.execute()
            self.assertFalse(s.storage.exists())

    def test_first_scan_waits_for_new_completion_and_failure_is_not_marked_complete(self):
        s=object.__new__(m.Setup);client=Mock();s.jellyfin=lambda:client
        old=[{'Key':'RefreshLibrary','State':'Idle','LastExecutionResult':{'Status':'Completed','EndTimeUtc':'old'}}]
        new=[{'Key':'RefreshLibrary','State':'Idle','LastExecutionResult':{'Status':'Completed','EndTimeUtc':'new'}}]
        client.call.side_effect=[old,None,old,new]
        with patch.object(m.time,'sleep') as sleep:s.scan_libraries()
        sleep.assert_called_once_with(1)
        self.assertEqual(client.call.call_args_list[1].args,('/Library/Refresh',{},'POST'))
        s.status={'completed':[],'external':{}};s.track=Path('/unused')
        client.call.side_effect=None;client.call.return_value=old
        with patch.object(m.time,'sleep'),patch.object(m,'private_json') as save:
            with self.assertRaises(RuntimeError):s.phase('library-scan',s.scan_libraries)
            self.assertEqual(s.status['completed'],[]);save.assert_not_called()

    def test_empty_library_marker_is_idempotent_and_never_overwrites_foreign_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=object.__new__(m.Setup);s.storage=Path(tmp);s.uid=os.getuid();s.gid=os.getgid()
            for name in ('电影','电视剧'):(s.storage/'media'/name).mkdir(parents=True)
            s.watch_roots();s.watch_roots()
            files=list(s.storage.rglob('*.txt'));self.assertEqual(len(files),2)
            self.assertTrue(all(p.read_text()==m.WATCH_CONTENT for p in files))
            files[0].write_text('owner data')
            with self.assertRaises(RuntimeError):s.watch_roots()
            self.assertEqual(files[0].read_text(),'owner data')

    def test_single_login_writes_external_auth_once_and_keeps_owner_and_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            for name in ('radarr','prowlarr'):
                (base/name).mkdir(); (base/name/'config.xml').write_text('<Config><ApiKey>%s-test-key</ApiKey><Port>1</Port></Config>' % name)
            real=Path
            def fake_path(value,*rest):
                text=str(value)
                return real(text.replace('/srv/nas/appdata',tmp),*rest) if text.startswith('/srv/nas/appdata') else real(value,*rest)
            s=object.__new__(m.Setup)
            with patch.object(m,'Path',side_effect=fake_path),patch.object(m,'run') as run,patch.object(m,'Native') as native,patch.object(m,'app_key',return_value='k'):
                apps=(('radarr','nas-radarr',7878,'v3'),('prowlarr','hotel-prowlarr',9696,'v1'))
                s.single_login(apps)
                self.assertEqual([c.args[0][:2] for c in run.call_args_list],[['docker','stop'],['docker','start'],['docker','stop'],['docker','start']])
                for name in ('radarr','prowlarr'):
                    root=m.ET.fromstring((base/name/'config.xml').read_text())
                    self.assertEqual(root.findtext('AuthenticationMethod'),'External');self.assertEqual(root.findtext('ApiKey'),name+'-test-key')
                run.reset_mock(); s.single_login(apps)   # --resume: no edit, but a stopped container is started again
                self.assertEqual([c.args[0][:2] for c in run.call_args_list],[['docker','start'],['docker','start']])
                self.assertEqual(native.call_count,4)

    def test_hevc_policies_allow_by_default_and_offer_two_stricter_choices(self):
        self.assertEqual(m.DEFAULT_HEVC_POLICY,'allow');self.assertEqual(m.HEVC_POLICIES['allow'],(0,0))
        score,minimum=m.HEVC_POLICIES['prefer_h264'];self.assertLess(score,0);self.assertLessEqual(minimum,score)   # allowed, ranked lower
        score,minimum=m.HEVC_POLICIES['h264_only'];self.assertLess(score,minimum)                               # rejected

    def test_data_disk_outside_the_service_sandbox_gets_a_write_drop_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            dropin=Path(tmp)/'hotel-tv.service.d'/'50-media-storage.conf'
            s=m.Setup.__new__(m.Setup)
            with patch.object(m,'SERVICE_DROPIN',dropin),patch.object(m,'run') as run:
                s.storage=Path('/srv/hotel-tv/media/.automation');self.assertFalse(s.storage_access());self.assertFalse(dropin.exists())
                s.storage=Path('/mnt/media disk/data');self.assertTrue(s.storage_access())
                self.assertEqual(dropin.read_text(),'[Service]\nReadWritePaths="/mnt/media disk/data"\n')
                self.assertEqual(run.call_args[0][0],['systemctl','daemon-reload'])
                s.storage_access();self.assertEqual(len(list(dropin.parent.iterdir())),1)   # idempotent, no temp files left

    def test_qbittorrent_writes_group_writable_files(self):
        spec2=importlib.util.spec_from_file_location('smart_download',ROOT/'deploy/nas/smart_download.py');sd=importlib.util.module_from_spec(spec2);spec2.loader.exec_module(sd)
        args=type('A',(),{'uid':1000,'gid':1001,'root':'/srv/nas','media':'/srv/hotel-tv/media','timezone':'Asia/Singapore'})()
        self.assertIn('UMASK: "002"',sd.compose_qb(args))

if __name__=='__main__':unittest.main()
