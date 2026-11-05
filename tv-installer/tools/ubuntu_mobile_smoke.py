"""Fresh Ubuntu LAN HTTP startup check. Never connects to or installs on a television."""
import json
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.request
from release_config import ART, VERSION

IMAGE = 'ubuntu@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517'


def main():
    folder = ART / f'release/hotel-tv-installer-{VERSION}-linux-amd64'
    command = ['docker', '--context', 'desktop-linux']
    container = subprocess.check_output(command + [
        'run', '-d', '--rm', '--platform', 'linux/amd64', '--read-only', '--user', '1000:1000',
        '--tmpfs', '/tmp', '--tmpfs', '/root',
        '--tmpfs', '/home/ubuntu:uid=1000,gid=1000,mode=0700', '-p', '127.0.0.1::18795',
        '-v', str(folder) + ':/app:ro', '-e', 'HOTEL_INSTALLER_STATE=/tmp/tv20-state',
        IMAGE, 'sh', '-c', 'cd /app && sh ./后台启动手机局域网安装.sh && exec sleep 90',
    ], text=True, timeout=60).strip()
    try:
        host = code = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            log = subprocess.check_output(command + ['logs', container], text=True, stderr=subprocess.STDOUT, timeout=5)
            address = re.search(r'http://([0-9.]+:18795)/', log)
            short_code = re.search(r'手机连接码：(\d{8})', log)
            if address and short_code:
                host, code = address[1], short_code[1]
                break
            time.sleep(.2)
        assert host and code, 'Ubuntu mobile entry did not start'
        init_name = subprocess.check_output(command + ['exec', container, 'cat', '/proc/1/comm'], text=True, timeout=5).strip()
        assert init_name == 'sleep', 'Background launcher did not return independently'
        mapped = subprocess.check_output(command + ['port', container, '18795/tcp'], text=True, timeout=5).strip()
        base = 'http://' + mapped
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def request(path, body=None, token=''):
            headers = {'Host': host, 'Origin': 'http://' + host}
            if token:
                headers['X-App-Token'] = token
            if body is not None:
                headers['Content-Type'] = 'application/json'
            payload = None if body is None else json.dumps(body).encode()
            req = urllib.request.Request(base + path, headers=headers, data=payload)
            return opener.open(req, timeout=10)

        # Docker maps a host port; the request preserves the service's actual LAN Host/Origin.
        html = request('/').read().decode()
        assert 'name="installer-lan" content="true"' in html and code not in html
        try:
            request('/api/state')
            raise AssertionError('Unauthenticated state was exposed')
        except urllib.error.HTTPError as error:
            assert error.code == 401
        token = json.load(request('/api/pair-browser', {'code': code}))['token']
        state = json.load(request('/api/state', token=token))
        assert state['version'] == VERSION and state['host'] == 'linux/amd64' and not state['has_pin']
        doctor = json.load(request('/api/doctor', {}, token))
        assert 'Android Debug Bridge' in doctor['adb']
        guides = json.load(request('/api/connection-guides', token=token))
        index = json.load(request('/api/community-index', token=token))
        result = dict(version=VERSION, host=state['host'], fresh_ubuntu=True,
                      bound_private_interface=True, denied_without_code=True,
                      short_code_authorized=True, actual_bundled_adb=True,
                      background_launcher_returned=True,
                      unprivileged_user=True,
                      connection_families=len(guides['guides']), community_directories=len(index['examples']),
                      physical_tv_connected=False, docker_host_header_mapping=True)
        assert result['connection_families'] == 26 and result['community_directories'] == 420
        (ART / 'ubuntu-mobile-smoke.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result))
    finally:
        subprocess.run(command + ['rm', '-f', container], capture_output=True, timeout=15)


if __name__ == '__main__':
    main()
