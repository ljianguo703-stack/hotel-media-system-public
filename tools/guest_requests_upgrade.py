#!/usr/bin/env python3
"""Exercise real upload/queue/upgrade/rollback in disposable loopback processes."""
import argparse
import hashlib
import http.client
import json
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from software_agent import Agent


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    with socket.socket() as check:
        check.bind(('127.0.0.1', 18792))
    with tempfile.TemporaryDirectory(prefix='hotel-rq02-upgrade-') as temporary:
        root = Path(temporary)
        releases, data, media = root / 'releases', root / 'data', root / 'media'
        for folder in (releases, data, media):
            folder.mkdir()
        with zipfile.ZipFile(args.baseline) as archive:
            baseline = json.loads(archive.read('hotel-software.json'))
            previous = releases / baseline['version']
            previous.mkdir()
            for name in baseline['files']:
                target = previous / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
        current = root / 'current'
        current.symlink_to(previous)
        config = root / 'devices.json'
        config.write_text(json.dumps({'devices': {'test-tv': {'token': 'rq02-upgrade-test-only', 'room': '101'}}}))
        film = media / 'preserved.mp4'
        film.write_bytes(b'upgrade-fixture-media')
        fingerprint = hashlib.sha256(film.read_bytes()).hexdigest()
        config_hash = hashlib.sha256(config.read_bytes()).hexdigest()
        (data / 'catalog.json').write_text(json.dumps({'hotel': '升级验证酒店', 'items': []}))
        (data / 'ads.json').write_text('{"ads":[]}')
        tv_port, admin_port = free_port(), free_port()
        cookie, csrf = '', ''
        process = None
        log = (evidence / 'upgrade-processes.log').open('w')

        def service(command, **_kwargs):
            nonlocal process
            if command == ['systemctl', 'stop', 'hotel-tv']:
                if process and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=15)
            elif command == ['systemctl', 'start', 'hotel-tv']:
                process = subprocess.Popen([sys.executable, str(current / 'server/server.py'), '--host', '127.0.0.1',
                    '--port', str(tv_port), '--admin-port', str(admin_port), '--admin-web', str(current / 'admin'),
                    '--config', str(config), '--data', str(data), '--media', str(media)], stdout=log, stderr=log)
            else:
                raise AssertionError('Unexpected installer operation ' + repr(command))
            return ''

        def request(route, body=None, headers=None, port=None, decode=True):
            connection = http.client.HTTPConnection('127.0.0.1', port or admin_port, timeout=10)
            options = {'Cookie': cookie, 'X-CSRF-Token': csrf, **(headers or {})}
            raw = body
            if isinstance(body, dict):
                raw = json.dumps(body).encode()
                options['Content-Type'] = 'application/json'
            connection.request('POST' if body is not None else 'GET', route, body=raw, headers=options)
            response = connection.getresponse()
            status, response_headers, payload = response.status, dict(response.getheaders()), response.read()
            connection.close()
            assert status < 400, (status, payload.decode('utf-8', 'replace'))
            return (json.loads(payload) if decode else payload.decode()), response_headers

        def install(path):
            package = request('/api/admin/software/upload', path.read_bytes(), {'X-Filename': path.name})[0]['package']
            state = request('/api/admin/software')[0]
            queued = request('/api/admin/software/install', {'package_id': package['id'], 'targets': ['server'],
                'confirm': True, 'revision': state['revision'], 'request_id': secrets.token_hex(16)})[0]
            ident = queued['jobs'][0]['id']
            assert agent.once()
            jobs = request('/api/admin/software')[0]['jobs']
            return next(job for job in jobs if job['id'] == ident)

        try:
            service(['systemctl', 'start', 'hotel-tv'])
            agent = Agent(data, root / 'agent', releases, current, admin_port=admin_port, runner=service)
            agent.wait_health()
            credentials = dict(line.split('=', 1) for line in (data / 'admin-initial-password.txt').read_text().splitlines())
            login, headers = request('/api/admin/login', {'username': 'admin', 'password': credentials['password']})
            cookie, csrf = headers['Set-Cookie'].split(';', 1)[0], login['user']['csrf']
            completed = install(args.candidate.resolve())
            assert completed['status'] == 'completed', completed
            assert Path(completed['database_backup']).is_file()
            revision = request('/api/admin/state')[0]['revision']
            request('/api/admin/operations', {'kind': 'guest_request', 'action': 'guest_settings', 'revision': revision,
                'settings': {'enabled': True, 'public_url': '203.0.113.10:8080', 'prompt': '想看什么影片？扫码留言',
                    'first_delay_seconds': 180, 'interval_seconds': 1800, 'visible_seconds': 20}})
            state = request('/api/admin/operations?section=guest_requests')[0]
            assert state['listener']['available'] and state['listener']['port'] == 18792
            url = urlsplit(state['settings']['entry_url'])
            form, guest_headers = request(url.path + '?' + url.query, port=18792, decode=False)
            fields = {'entry': parse_qs(url.query)['entry'][0], 'message': '升级后提交的影片心愿',
                      'submission_id': re.search(r'name="submission_id" value="([a-f0-9]+)"', form)[1]}
            request('/request', urlencode(fields), {'Content-Type': 'application/x-www-form-urlencoded',
                'Cookie': guest_headers['Set-Cookie'].split(';', 1)[0], 'Origin': 'http://203.0.113.10:8080'}, port=18792, decode=False)
            assert request('/api/admin/operations?section=guest_requests')[0]['total'] == 1
            with zipfile.ZipFile(args.candidate) as archive:
                manifest = json.loads(archive.read('hotel-software.json'))
                files = {name: archive.read(name) for name in manifest['files']}
            files['server/server.py'] = b'raise RuntimeError("isolated rollback fixture")\n'
            manifest.update(version='rq02-rollback-fixture', base_release=manifest['version'],
                            files={name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()})
            broken = root / 'rollback-fixture.zip'
            with zipfile.ZipFile(broken, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('hotel-software.json', json.dumps(manifest))
                for name, raw in files.items():
                    archive.writestr(name, raw)
            restored = install(broken)
            assert restored['status'] == 'rolled_back', restored
            assert current.resolve().name == manifest['base_release']
            assert request('/api/admin/operations?section=guest_requests')[0]['rows'][0]['message'] == fields['message']
            assert hashlib.sha256(film.read_bytes()).hexdigest() == fingerprint
            assert hashlib.sha256(config.read_bytes()).hexdigest() == config_hash
            result = {'passed': True, 'baseline': baseline['version'], 'version': current.resolve().name,
                      'upload_and_queue': True, 'installer_completed': completed['status'], 'database_backup': True,
                      'guest_listener': state['listener'], 'post_upgrade_submission': True,
                      'failed_update_rolled_back': restored['status'], 'suggestion_preserved_after_rollback': True,
                      'media_and_device_config_preserved': True,
                      'environment': 'Disposable real Python/SQLite HTTP processes; fixture handles systemctl commands'}
            (evidence / 'upgrade-results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
            print(json.dumps(result, ensure_ascii=False))
        finally:
            service(['systemctl', 'stop', 'hotel-tv'])
            log.close()


if __name__ == '__main__':
    main()
