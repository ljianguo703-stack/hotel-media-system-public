#!/usr/bin/env python3
"""Disposable Ubuntu only: real legacy upload, old worker, dpkg and controller."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time


SYSTEMCTL = r'''#!/usr/bin/python3
import os,signal,subprocess,time
from pathlib import Path
import sys
args=sys.argv[1:]
if args not in [['start','hotel-tv'],['stop','hotel-tv']]:raise SystemExit('Unexpected systemctl operation')
with open('/out/service-calls.jsonl','a') as f:f.write(__import__('json').dumps(args)+'\n')
pidfile=Path('/tmp/hotel-fixture.pid')
if args[0]=='stop':
 if pidfile.exists():
  pid=int(pidfile.read_text())
  try:os.kill(pid,signal.SIGTERM)
  except ProcessLookupError:pass
  for _ in range(150):
   try:
    value=Path('/proc/'+str(pid)+'/stat').read_text()
    if value.split(') ',1)[1].startswith('Z '):break
   except FileNotFoundError:break
   time.sleep(.05)
  pidfile.unlink(missing_ok=True)
else:
 if Path('/tmp/fail-target').exists() and Path('/opt/hotel-tv/current').resolve().name.endswith('in20u1'):raise SystemExit(0)
 log=open('/out/server.log','ab',buffering=0)
 p=subprocess.Popen(['/usr/bin/python3','/opt/hotel-tv/current/server/server.py','--host','127.0.0.1','--port','18790','--admin-port','18791','--admin-web','/opt/hotel-tv/current/admin','--config','/etc/hotel-tv/devices.json','--data','/srv/hotel-tv/data','--media','/srv/hotel-tv/media'],stdout=log,stderr=log,start_new_session=True,user=1001,group=1001)
 pidfile.write_text(str(p.pid))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True, type=Path)
    parser.add_argument('--base-version', required=True)
    parser.add_argument('--package', required=True, type=Path)
    parser.add_argument('--legacy', required=True, type=Path)
    parser.add_argument('--fault-retry', action='store_true')
    args = parser.parse_args()
    if not Path('/.dockerenv').exists() or os.geteuid() != 0:
        raise SystemExit('Disposable root Docker fixture only')
    output = Path('/out')
    output.mkdir(exist_ok=True)
    release = Path('/opt/hotel-tv/releases') / args.base_version
    shutil.copytree(args.baseline, release)
    Path('/opt/hotel-tv/current').symlink_to(release)
    for name in ['/srv/hotel-tv', '/srv/hotel-tv/data', '/srv/hotel-tv/media']:
        path = Path(name)
        path.mkdir(exist_ok=True, parents=True)
        os.chown(path, 1001, 1001)
        path.chmod(0o750)
    media = Path('/srv/hotel-tv/media/preserve.mp4')
    media.write_bytes(b'preserved hotel media')
    os.chown(media, 1001, 1001)
    config = Path('/etc/hotel-tv/devices.json')
    config.parent.mkdir(parents=True, exist_ok=True)
    os.chown(config.parent, 1001, 1001)
    config.write_text('{"devices":{"test-tv":{"token":"local-fixture-only","room":"101"}}}')
    os.chown(config, 1001, 1001)
    (Path('/usr/local/bin') / 'systemctl').write_text(SYSTEMCTL)
    Path('/usr/local/bin/systemctl').chmod(0o755)
    # Native dpkg is exercised; this fixture intentionally does not contact APT sources.
    Path('/usr/local/bin/apt-get').write_text('#!/bin/sh\nset -eu\nfor arg in "$@"; do last="$arg"; done\nexec dpkg --install "$last"\n')
    Path('/usr/local/bin/apt-get').chmod(0o755)
    sys.path.insert(0, str(args.legacy))
    from software_agent import Agent
    agent = Agent('/srv/hotel-tv/data', '/var/lib/hotel-tv-installer')
    agent.deployment_lock = Path('/var/lock/hotel-tv-deploy.lock')
    cookie = csrf = ''

    def request(route, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', 18791, timeout=30)
        options = {'Cookie': cookie, 'X-CSRF-Token': csrf, **(headers or {})}
        raw = body
        if isinstance(body, dict):
            raw = json.dumps(body).encode()
            options['Content-Type'] = 'application/json'
        connection.request('POST' if body is not None else 'GET', route, raw, options)
        response = connection.getresponse()
        status, response_headers, payload = response.status, dict(response.getheaders()), response.read()
        connection.close()
        assert status < 400, (status, payload.decode(errors='replace'))
        return json.loads(payload), response_headers

    def install():
        package = request('/api/admin/software/upload', args.package.read_bytes(), {'X-Filename': args.package.name})[0]['package']
        snapshot = request('/api/admin/software')[0]
        queued = request('/api/admin/software/install', {'package_id': package['id'], 'targets': ['server'], 'confirm': True,
                    'revision': snapshot['revision'], 'request_id': secrets.token_hex(16)})[0]
        assert agent.once()
        jobs = request('/api/admin/software')[0]['jobs']
        return next(j for j in jobs if j['id'] == queued['jobs'][0]['id']), jobs

    try:
        subprocess.run(['systemctl', 'start', 'hotel-tv'], check=True)
        agent.wait_health()
        before = hashlib.sha256(config.read_bytes() + media.read_bytes()).hexdigest()
        credentials = dict(line.split('=', 1) for line in Path('/srv/hotel-tv/data/admin-initial-password.txt').read_text().splitlines())
        login, headers = request('/api/admin/login', {'username': 'admin', 'password': credentials['password']})
        cookie, csrf = headers['Set-Cookie'].split(';', 1)[0], login['user']['csrf']
        rollback = None
        if args.fault_retry:
            Path('/tmp/fail-target').touch()
            failed, jobs = install()
            assert failed['status'] == 'failed', failed
            child = next(j for j in jobs if j.get('unified_upgrade'))
            assert child['status'] == 'rolled_back' and Path(child['database_backup']).is_file(), child
            assert Path('/opt/hotel-tv/current').resolve() == release
            rollback = True
            Path('/tmp/fail-target').unlink()
        completed, jobs = install()
        assert completed['status'] == 'completed', completed
        current = Path('/opt/hotel-tv/current').resolve()
        assert current.name == 'v1-0-0-rc5-in20u1'
        child = next(j for j in jobs if j.get('unified_upgrade') and j['status'] == 'completed')
        assert Path(child['database_backup']).is_file()
        assert request('/api/admin/dashboard')[0]['server']['available']
        state = request('/api/admin/software')[0]
        assert next(i for i in state['inventory'] if i['software_id'] == 'hotel-tv-server')['version'] == current.name
        # Reinstalling the same DEB directly must be harmless even after an interrupted configure.
        calls = Path('/out/service-calls.jsonl').read_text()
        subprocess.run(['dpkg', '--install', str(args.package)], check=True, stdout=subprocess.DEVNULL)
        assert Path('/out/service-calls.jsonl').read_text() == calls
        assert before == hashlib.sha256(config.read_bytes() + media.read_bytes()).hexdigest()
        owner = Path('/srv/hotel-tv/data/events.sqlite3').stat()
        assert owner.st_uid == 1001
        checked = subprocess.check_output(['/usr/bin/python3', '-I', '/usr/lib/hotel-tv-system-upgrade/unified_upgrade.py', '--check'], text=True)
        assert json.loads(checked)['changed'] is False
        result = {'passed': True, 'baseline': args.base_version, 'target': current.name,
                  'real_http_upload_and_queue': True, 'legacy_in17_executor': True, 'native_dpkg_postinst': True,
                  'database_snapshot': True, 'configuration_and_media_preserved': True, 'web_uid_preserved': True,
                  'real_linux_dashboard': True, 'duplicate_deb_no_service_restart': True,
                  'injected_start_failure_and_same_deb_retry': rollback,
                  'systemctl': 'fixture process runner', 'apt': 'offline shim to native dpkg; no dependency download'}
        (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        subprocess.run(['systemctl', 'stop', 'hotel-tv'], check=False)


if __name__ == '__main__':
    main()
