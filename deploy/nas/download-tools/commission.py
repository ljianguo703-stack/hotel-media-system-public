#!/usr/bin/env python3
"""Commission native Prowlarr -> qB integration, preserving all existing torrents."""
import argparse
import copy
import http.cookiejar
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import build_opener, HTTPCookieProcessor, ProxyHandler, Request


def ensure_network(backup, verify):
    """Join Prowlarr to qB's existing bridge. Never add a new NIC/default route to libtorrent."""
    import yaml
    path = Path('/opt/stacks/hotel-prowlarr/compose.yaml')
    config = yaml.safe_load(path.read_text())
    services = config['services']; assert len(services) == 1
    service = next(iter(services.values())); assert service['container_name'] == 'hotel-prowlarr'
    target = 'hotel-qbittorrent_default'
    def networks(name):
        return json.loads(subprocess.check_output(['docker', 'inspect', name]))[0]['NetworkSettings']['Networks']
    assert target in networks('hotel-qbittorrent')
    if verify:
        assert target in networks('hotel-prowlarr')
        assert config['networks']['qb-native'] == {'external': True, 'name': target}
        return
    original = backup / 'prowlarr-compose-before.yaml'
    if not original.exists(): original.write_bytes(path.read_bytes()); original.chmod(0o600)
    current = service.get('networks') or ['default']
    if isinstance(current, list): current = {name: {} for name in current}
    current['qb-native'] = {}; service['networks'] = current
    config.setdefault('networks', {})['qb-native'] = {'external': True, 'name': target}
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    subprocess.run(['docker', 'compose', '-f', str(path), 'config', '--quiet'], check=True)
    if target not in networks('hotel-prowlarr'):
        subprocess.run(['docker', 'network', 'connect', target, 'hotel-prowlarr'], check=True)


def run(backup, verify=False):
    assert os.geteuid() == 0
    backup.mkdir(mode=0o700, parents=True, exist_ok=True)
    ensure_network(backup, verify)
    db = sqlite3.connect('/srv/hotel-tv/data/events.sqlite3')
    settings = json.loads(db.execute("SELECT value FROM kv WHERE key='download_settings'").fetchone()[0]); db.close()
    secrets = json.loads(Path('/srv/hotel-tv/data/download-secrets.json').read_text())
    source = next(s for s in settings['sources'] if s['type'] == 'prowlarr')
    key = secrets['sources'][source['id']]['api_key']
    qb = settings['engines']['qbittorrent']
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def q(path, data=None):
        req = Request(qb['url'].rstrip('/') + '/api/v2/' + path,
                      data=urlencode(data).encode() if data is not None else None,
                      headers={'Content-Type': 'application/x-www-form-urlencoded',
                               'Origin': qb['url'].rstrip('/'), 'Referer': qb['url'].rstrip('/') + '/'})
        return opener.open(req, timeout=30).read()

    def p(path, data=None, method=None):
        req = Request(source['url'].rstrip('/') + '/api/v1/' + path,
                      data=json.dumps(data).encode() if data is not None else None, method=method,
                      headers={'X-Api-Key': key, 'Content-Type': 'application/json'})
        raw = opener.open(req, timeout=45).read()
        return json.loads(raw) if raw else None

    def save(name, data):
        path = backup / name
        if not path.exists(): path.write_text(json.dumps(data)); path.chmod(0o600)

    for attempt in range(60):
        try:
            p('health')
            break
        except (URLError, ConnectionError):
            if attempt == 59: raise
            time.sleep(.5)
    # qB 4.x returns 200/Ok.; 5.x returns 204 with a port-specific SID cookie.
    # Verify the authenticated API next, rather than depending on response text.
    assert q('auth/login', {'username': qb['username'], 'password': secrets['qbittorrent']['password']}).strip() in (b'', b'Ok.')
    before = json.loads(q('torrents/info'))
    if verify:
        cats = json.loads(q('torrents/categories'))
        assert cats['默认下载']['savePath'] == '/downloads'
        prefs = json.loads(q('app/preferences'))
        assert prefs['use_category_paths_in_manual_mode'] and not prefs['temp_path_enabled']
        assert not prefs['bypass_local_auth'] and prefs['web_ui_csrf_protection_enabled']
        clients = p('downloadclient')
        assert len(clients) == 1 and clients[0]['enable']
        p('downloadclient/test', clients[0])
        assert p('config/host')['authenticationMethod'] == 'external'
        q('auth/logout', {})
        print('EXISTING_NATIVE_CONFIGURATION_AND_CLIENT_TEST_VERIFIED')
        return
    signature = lambda rows: {r['hash']: {k:r.get(k) for k in ('save_path', 'download_path', 'auto_tmm', 'category')} for r in rows}
    save('qb-torrents-before.json', signature(before))
    prefs = json.loads(q('app/preferences')); save('qb-preferences-before.json', prefs)
    cats = json.loads(q('torrents/categories')); save('qb-categories-before.json', cats)
    category = '默认下载'
    if category in cats:
        assert cats[category]['savePath']=='/downloads', 'Existing category differs; preserve it'
    else:q('torrents/createCategory', {'category': category, 'savePath': '/downloads', 'downloadPathEnabled': 'false'})
    q('app/setPreferences', {'json': json.dumps({'use_category_paths_in_manual_mode': True, 'locale': 'zh_CN', 'temp_path_enabled': False})})
    assert signature(json.loads(q('torrents/info'))) == signature(before), 'Existing torrent paths changed'
    preferences = json.loads(q('app/preferences'))
    assert not preferences['bypass_local_auth'] and not preferences['bypass_auth_subnet_whitelist_enabled']
    assert preferences['web_ui_csrf_protection_enabled'] and preferences['web_ui_host_header_validation_enabled']
    clients = p('downloadclient'); save('prowlarr-clients-before.json', clients)
    if clients:
        assert len(clients)==1 and clients[0]['name']=='qBittorrent - Local', 'Existing native clients need manual review'
        p('downloadclient/test',clients[0]);q('auth/logout',{});return
    payload = copy.deepcopy(next(s for s in p('downloadclient/schema') if s['implementation'] == 'QBittorrent'))
    payload.update(name='qBittorrent - Local', enable=True, priority=1)
    fields = {'host': 'hotel-qbittorrent', 'port': 8098, 'username': qb['username'],
              'password': secrets['qbittorrent']['password'], 'category': category, 'initialState': 0}
    for f in payload['fields']:
        if f['name'] in fields: f['value'] = fields[f['name']]
    p('downloadclient/test', payload)
    client = p('downloadclient', payload)
    host = p('config/host'); save('prowlarr-host-before.json', host)
    host.update(authenticationMethod='external', authenticationRequired='enabled')
    p('config/host', host, 'PUT')
    q('auth/logout', {})
    print(json.dumps({'native_client_test': True, 'client_id': client['id'], 'existing_torrents_preserved': len(before),
                      'category': category, 'prowlarr_auth': 'external-behind-admin-gate'}, ensure_ascii=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    try:
        run(args.backup, args.verify)
    except Exception as exc:
        # Upstream validation responses and configs can contain credentials: never dump them.
        import traceback
        line = traceback.extract_tb(exc.__traceback__)[-1].lineno
        raise SystemExit('Native commissioning failed at line ' + str(line) + ': ' + type(exc).__name__ + ' HTTP ' + str(getattr(exc, 'code', 'n/a')))
