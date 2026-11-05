"""TV01 / I04: compatibility rules, real ffprobe/ffmpeg jobs, source never modified, catalog switch, pause/cancel/retry/restart."""
import hashlib
import json
import shutil
import subprocess
import time
import unittest
import inspect
import threading
from unittest.mock import patch
from pathlib import Path
from urllib.parse import unquote
import test_management as fixture
import test_orders
from operations_compat import judge, VERDICTS
from operations_compat import CompatOperations

FFMPEG = shutil.which('ffmpeg') and shutil.which('ffprobe')


def stream(kind, codec, **extra):
    return {'codec_type': kind, 'codec_name': codec, **extra}


class RulesTest(unittest.TestCase):
    def verdict(self, *streams, fmt='matroska,webm'):
        return judge({'streams': list(streams), 'format': {'format_name': fmt}})

    def test_dolby_is_not_assumed_audible_on_legacy_tv(self):
        for codec in ('ac3', 'eac3'):
            for channels in (2, 6):
                verdict, reasons, _ = self.verdict(stream('video', 'hevc', height=1080), stream('audio', codec, channels=channels))
                self.assertEqual(verdict, 'audio'); self.assertTrue(any('AAC' in r for r in reasons))

    def test_multichannel_needs_stereo_but_mono_and_stereo_aac_stay_direct(self):
        for channels, expected in ((1, 'ok'), (2, 'ok'), (6, 'audio'), (8, 'audio')):
            self.assertEqual(self.verdict(stream('video', 'h264', height=1080), stream('audio', 'aac', channels=channels))[0], expected)

    def test_rules(self):
        self.assertEqual(self.verdict(stream('video', 'h264', profile='High', height=1080), stream('audio', 'aac'))[0], 'ok')
        self.assertEqual(self.verdict(stream('video', 'hevc', height=2160), stream('audio', 'dts'))[0], 'audio')
        self.assertEqual(self.verdict(stream('video', 'mpeg4', height=576), stream('audio', 'mp3'), fmt='avi')[0], 'video')
        self.assertEqual(self.verdict(stream('video', 'h264', height=720), stream('audio', 'aac'), fmt='avi')[0], 'remux')
        self.assertEqual(self.verdict(stream('video', 'h264', profile='High 10', height=1080), stream('audio', 'aac'))[0], 'video')
        self.assertEqual(self.verdict(stream('video', 'av1', height=1080), stream('audio', 'opus'))[0], 'video')
        self.assertEqual(self.verdict(stream('video', 'h264', height=4320), stream('audio', 'aac'))[0], 'video')
        self.assertEqual(self.verdict(stream('audio', 'aac'))[0], 'error')
        verdict, reasons, warnings = self.verdict(stream('video', 'h264', height=1080), stream('audio', 'truehd'), stream('subtitle', 'hdmv_pgs_subtitle'))
        self.assertEqual(verdict, 'audio'); self.assertTrue(any('图片字幕' in w for w in warnings)); self.assertTrue(reasons)
        self.assertEqual(set(VERDICTS), {'ok', 'remux', 'audio', 'video', 'error'})


@unittest.skipUnless(FFMPEG, 'ffmpeg/ffprobe not installed')
class CompatE2E(unittest.TestCase):
    setUp = fixture.ManagementE2E.setUp
    tearDown = fixture.ManagementE2E.tearDown
    request = fixture.ManagementE2E.request
    state = fixture.ManagementE2E.state
    mutate = fixture.ManagementE2E.mutate
    wait_scan = fixture.ManagementE2E.wait_scan
    op = test_orders.OrdersE2E.op
    login_as = test_orders.OrdersE2E.login_as

    def make(self, name, args, seconds=2):
        target = self.media / name
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=25', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                        '-t', str(seconds), *args, str(target)], check=True)
        return target

    def compat(self, **q):
        return self.request('/api/admin/operations?' + '&'.join('%s=%s' % kv for kv in {'section': 'compat', **q}.items()), auth=True)[2]

    def wait_job(self, ident, statuses=('done', 'failed', 'cancelled'), timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = next(r for r in self.compat()['rows'] if r['id'] == ident)
            if row.get('job_status') in statuses: return row
            time.sleep(0.2)
        self.fail('job did not reach %s' % (statuses,))

    def tv_catalog(self):
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1', self.tv.server_port, timeout=10)
        conn.request('GET', '/api/v1/catalog', headers={'X-TV-Token': 'tv-test-token'}); body = json.loads(conn.getresponse().read()); conn.close()
        return body

    def test_real_dolby_copy_is_aac_stereo_and_video_packets_are_unchanged(self):
        source = self.make('dolby.mkv', ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'eac3', '-ac', '6'])
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        self.wait_scan('main')
        media = next(m for m in self.state()['media'] if m['title']=='dolby')
        self.assertFalse(self.op('compat', 'compat_probe', ids=[media['id']])[2]['failed'])
        row = next(r for r in self.compat()['rows'] if r['media_id']==media['id'])
        self.assertEqual(row['verdict'], 'audio')
        self.assertEqual(self.op('compat', 'compat_enqueue', ids=[row['id']])[2]['ok'], [row['id']])
        done = self.wait_job(row['id']); self.assertEqual(done['job_status'], 'done', done)
        target = self.store.media_path(unquote(done['copy_route']))
        info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(target)]))
        audio = next(s for s in info['streams'] if s['codec_type']=='audio')
        self.assertEqual((audio['codec_name'],audio['channels']), ('aac',2))
        def video_hash(file):
            return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(file), '-map', '0:v:0', '-c', 'copy', '-f', 'hash', '-hash', 'sha256', '-'])
        self.assertEqual(video_hash(source), video_hash(target))
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def setup_library(self):
        dts = self.make('电影/环绕声.mkv', ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'dca', '-strict', '-2'])
        avi = self.make('电影/老片.avi', ['-c:v', 'mpeg4', '-c:a', 'libmp3lame'])
        good = self.make('电影/正常.mp4', ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac'])
        self.wait_scan('main')
        with self.store.db() as db: db.execute('UPDATE media SET published=1')
        ids = [m['id'] for m in self.state()['media']]
        result = self.op('compat', 'compat_probe', ids=ids)[2]
        self.assertFalse(result['failed'], result)
        rows = {r['title']: r for r in self.compat()['rows'] if r.get('verdict')}
        return dts, avi, good, rows

    def test_probe_convert_switch_catalog_and_keep_source(self):
        (self.media / '电影').mkdir()
        dts, avi, good, rows = self.setup_library()
        verdicts = {t: r['verdict'] for t, r in rows.items()}
        self.assertEqual(verdicts.get('环绕声'), 'audio'); self.assertEqual(verdicts.get('老片'), 'video'); self.assertEqual(verdicts.get('正常'), 'ok')
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (dts, avi, good)}
        refused = self.op('compat', 'compat_enqueue', ids=[rows['正常']['id']])[2]
        self.assertEqual(refused['ok'], []); self.assertIn('无需', refused['failed'][0]['reason'])
        queued = self.op('compat', 'compat_enqueue', ids=[rows['环绕声']['id'], rows['老片']['id']])[2]
        self.assertEqual(len(queued['ok']), 2)
        done = [self.wait_job(rows[t]['id']) for t in ('环绕声', '老片')]
        self.assertEqual([d['job_status'] for d in done], ['done', 'done'], done)
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (dts, avi, good)})
        for d in done:
            copy = self.store.media_path(unquote(d['copy_route']))
            info = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-print_format', 'json', '-show_streams', str(copy)], capture_output=True, check=True).stdout)
            codecs = {s['codec_type']: s['codec_name'] for s in info['streams']}
            self.assertEqual(codecs, {'video': 'h264', 'audio': 'aac'})
        paths = {i['title']: i['path'] for i in self.tv_catalog()['items']}
        self.assertTrue(paths['环绕声'].startswith('/media/_compat/')); self.assertTrue(paths['老片'].startswith('/media/_compat/')); self.assertFalse(paths['正常'].startswith('/media/_compat/'))
        sources = {i['title']: i.get('source_path', '') for i in self.tv_catalog()['items']}
        self.assertTrue(sources['环绕声'].endswith('.mkv') and not sources['环绕声'].startswith('/media/_compat/')); self.assertEqual(sources['正常'], '')
        self.assertEqual(self.op('compat', 'compat_control', id=rows['环绕声']['id'], op='use_original')[0], 200)
        self.assertFalse({i['title']: i['path'] for i in self.tv_catalog()['items']}['环绕声'].startswith('/media/_compat/'))
        # A rescan must not import the generated copies as new films.
        self.wait_scan('main')
        self.assertEqual(sorted(m['title'] for m in self.state()['media']), ['正常', '环绕声', '现有影片', '老片'])
        # Changing the source invalidates the copy instead of serving stale content.
        self.assertEqual(self.op('compat', 'compat_control', id=rows['环绕声']['id'], op='use_copy')[0], 200)
        with avi.open('ab') as f: f.write(b'\0')
        self.assertFalse({i['title']: i['path'] for i in self.tv_catalog()['items']}['老片'].startswith('/media/_compat/'))

    def test_pause_cancel_retry_and_restart_recovery(self):
        (self.media / '电影').mkdir()
        long = self.make('电影/长片.avi', ['-c:v', 'mpeg4', '-q:v', '2', '-c:a', 'libmp3lame'], seconds=40)
        self.wait_scan('main')
        ids = [m['id'] for m in self.state()['media']]
        self.op('compat', 'compat_probe', ids=ids)
        row = next(r for r in self.compat()['rows'] if r.get('verdict') == 'video')
        self.op('compat', 'compat_enqueue', ids=[row['id']])
        self.wait_job(row['id'], ('running',))
        self.assertEqual(self.op('compat', 'compat_control', id=row['id'], op='pause')[2]['job_status'], 'paused')
        self.assertEqual(self.op('compat', 'compat_control', id=row['id'], op='resume')[2]['job_status'], 'queued')
        self.wait_job(row['id'], ('running',))
        self.assertEqual(self.op('compat', 'compat_control', id=row['id'], op='cancel')[2]['job_status'], 'cancelled')
        cancelled = self.wait_job(row['id'], ('cancelled',))
        time.sleep(0.5)
        self.assertFalse(list((self.media / '_compat').glob('*/*.part.mkv')))
        self.assertFalse(cancelled.get('copy_route'))
        self.assertEqual(self.op('compat', 'compat_control', id=row['id'], op='retry')[2]['job_status'], 'queued')
        self.assertEqual(self.wait_job(row['id'], timeout=120)['job_status'], 'done')
        # Simulate a crash mid-job: a running record and a temp file are recovered on the next start.
        with self.store.db() as db:
            rec = self.store.compat_record(db, row['id']); rec.update(job_status='running'); self.store.put_object(db, 'compat', rec)
        (self.media / '_compat' / row['id'] / 'job.part.mkv').write_bytes(b'partial')
        self.store.recover_compat_jobs()
        self.assertFalse((self.media / '_compat' / row['id'] / 'job.part.mkv').exists())
        self.assertEqual(self.wait_job(row['id'], timeout=120)['job_status'], 'done')
        self.assertTrue(long.exists())

    def test_permissions(self):
        viewer = self.login_as('vwc2', 'viewer')
        self.assertEqual(self.op('compat', 'compat_probe', headers=viewer, ids=['existing'])[0], 403)
        self.assertEqual(self.request('/api/admin/operations?section=compat', headers=viewer)[0], 200)

    def test_copy_choice_rejects_stale_revision_but_job_controls_use_job_state(self):
        with self.store.db() as db:
            self.store.put_object(db, 'compat', {'id': 'choice', 'title': '版本测试', 'copy_route': '/media/_compat/test.mkv', 'copy_status': 'ready', 'use_copy': True})
            self.store.put_object(db, 'compat', {'id': 'waiting', 'title': '排队测试', 'job_status': 'queued'})
        stale = self.state()['revision']
        self.op('advertiser', 'save', item={'id': 'revision', 'name': '推进版本'})
        payload = {'kind': 'compat', 'action': 'compat_control', 'id': 'choice', 'op': 'use_original', 'revision': stale}
        self.assertEqual(self.request('/api/admin/operations', payload, auth=True)[0], 409)
        with self.store.db() as db: self.assertTrue(self.store.compat_record(db, 'choice')['use_copy'])
        self.assertEqual(self.op('compat', 'compat_control', id='choice', op='use_original')[0], 200)
        self.assertEqual(self.request('/api/admin/operations', {**payload, 'op': 'use_copy'}, auth=True)[0], 409)
        self.assertEqual(self.op('compat', 'compat_control', id='choice', op='use_copy')[2]['use_copy'], True)
        queued = self.request('/api/admin/operations', {**payload, 'id': 'waiting', 'op': 'pause'}, auth=True)
        self.assertEqual((queued[0], queued[2]['job_status']), (200, 'paused'))

    def test_enqueue_during_worker_exit_is_not_stranded(self):
        snapshot, release = threading.Event(), threading.Event()
        source, first = inspect.getsourcelines(CompatOperations._compat_loop)
        exit_line = first + next(i for i, s in enumerate(source) if 'if not queue:' in s)
        def trace(frame, event, arg):
            if event == 'line' and frame.f_code is CompatOperations._compat_loop.__code__ and frame.f_lineno == exit_line and not frame.f_locals.get('queue'):
                snapshot.set(); release.wait(5)
            return trace
        with self.store.db() as db:
            self.store.put_object(db, 'compat', {'id': 'race', 'title': '调度测试', 'verdict': 'video'})
        def complete(ident):
            with self.store.db() as db:
                r = self.store.compat_record(db, ident); r['job_status'] = 'done'; self.store.put_object(db, 'compat', r)
        outcome = []
        threading.settrace(trace)
        with patch.object(self.store, '_run_compat', side_effect=complete):
            self.store.start_compat_worker()
            try:
                self.assertTrue(snapshot.wait(5))
                enqueue = threading.Thread(target=lambda: outcome.append(self.op('compat', 'compat_enqueue', ids=['race'])))
                enqueue.start()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    with self.store.db() as db: queued = self.store.compat_record(db, 'race').get('job_status') == 'queued'
                    if queued: break
                    time.sleep(.01)
                self.assertTrue(queued)
            finally:
                release.set(); threading.settrace(None)
            enqueue.join(5)
            self.assertFalse(enqueue.is_alive()); self.assertEqual(outcome[0][0], 200)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with self.store.db() as db: status = self.store.compat_record(db, 'race').get('job_status')
                if status == 'done': break
                time.sleep(.01)
            self.assertEqual(status, 'done', 'successfully enqueued work must run without a second click')

    def test_unexpected_job_failure_does_not_abandon_following_jobs(self):
        with self.store.db() as db:
            for ident in ('a-broken', 'b-next'):
                self.store.put_object(db, 'compat', {'id': ident, 'title': ident, 'job_status': 'queued', 'job_queued_at': 0})
        def run(ident):
            if ident == 'a-broken': raise OSError('simulated filesystem failure')
            with self.store.db() as db:
                r = self.store.compat_record(db, ident); r['job_status'] = 'done'; self.store.put_object(db, 'compat', r)
        with patch.object(self.store, '_run_compat', side_effect=run):
            self.store.start_compat_worker(); worker = self.store._compat_worker
            if worker: worker.join(5)
        with self.store.db() as db:
            self.assertEqual(self.store.compat_record(db, 'a-broken')['job_status'], 'failed')
            self.assertEqual(self.store.compat_record(db, 'b-next')['job_status'], 'done')

    def test_pausing_a_running_job_releases_the_queue(self):
        (self.media / '电影').mkdir()
        self.make('电影/长片.avi', ['-c:v', 'mpeg4', '-q:v', '2', '-c:a', 'libmp3lame'], seconds=120)
        self.make('电影/短片.avi', ['-c:v', 'mpeg4', '-c:a', 'libmp3lame'], seconds=2)
        self.wait_scan('main')
        self.op('compat', 'compat_probe', ids=[m['id'] for m in self.state()['media']])
        rows = {r['title']: r for r in self.compat()['rows']}
        long_id, short_id = rows['长片']['id'], rows['短片']['id']
        self.op('compat', 'compat_enqueue', ids=[long_id]); self.wait_job(long_id, ('running',))
        self.assertEqual(self.op('compat', 'compat_control', id=long_id, op='pause')[2]['job_status'], 'paused')
        self.op('compat', 'compat_enqueue', ids=[short_id])
        self.assertEqual(self.wait_job(short_id, timeout=20)['job_status'], 'done')
        self.assertEqual(self.wait_job(long_id, ('paused',))['job_status'], 'paused')
        self.assertFalse(list((self.media/'_compat'/long_id).glob('*.part.mkv')))
        self.op('compat', 'compat_control', id=long_id, op='resume')
        self.assertEqual(self.wait_job(long_id, timeout=90)['job_status'], 'done')

    def test_old_process_completion_cannot_overwrite_an_immediate_resume(self):
        import io
        from operations_compat import fingerprint
        path = self.media/'clip.mp4'; fp, size, mtime = fingerprint(path)
        with self.store.db() as db:
            self.store.put_object(db, 'compat', {'id': 'generation', 'title': '代次回归', 'route': '/media/clip.mp4', 'fingerprint': fp, 'size': size, 'mtime': mtime, 'verdict': 'video', 'info': {'duration': 1}, 'job_status': 'queued', 'job_generation': 0})
        class OldProcess:
            stdout = io.BytesIO()
            def poll(self): return 0
            def wait(proc, **kwargs):
                with self.store.db() as db:
                    r = self.store.compat_record(db, 'generation'); r.update(job_generation=1, job_status='queued'); self.store.put_object(db, 'compat', r)
                (self.media/'_compat/generation/job.1.part.mkv').write_bytes(b'new-generation-owned')
                return 0
        def spawn(args, **kwargs):
            Path(args[-1]).write_bytes(b'old-partial-copy')
            return OldProcess()
        from pathlib import Path
        with patch('operations_compat.subprocess.Popen', side_effect=spawn): self.store._run_compat('generation')
        with self.store.db() as db: record = self.store.compat_record(db, 'generation')
        self.assertEqual((record['job_status'], record['job_generation']), ('queued', 1))
        self.assertFalse(record.get('copy_route')); self.assertFalse((self.media/'_compat/generation/job.0.part.mkv').exists())
        self.assertEqual((self.media/'_compat/generation/job.1.part.mkv').read_bytes(), b'new-generation-owned')


if __name__ == '__main__':
    unittest.main()
