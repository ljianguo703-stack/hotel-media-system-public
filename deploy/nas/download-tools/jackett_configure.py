#!/usr/bin/env python3
"""Connect the upstream qB plugin; leave indexer selection to the owner. Never log API keys or response bodies."""
import argparse
from http.cookiejar import CookieJar
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlencode
from urllib.request import build_opener, ProxyHandler, Request, HTTPCookieProcessor

from jackett import PLUGIN_SHA256


def configure(plugin_file, output):
    assert os.geteuid() == 0
    cfg = Path('/srv/nas/appdata/jackett/Jackett/ServerConfig.json')
    key = json.loads(cfg.read_text())['APIKey']
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
    opener.open('http://127.0.0.1:9117/UI/Dashboard', timeout=15).close()
    def api(path, data=None, query=None, timeout=25):
        params = {'apikey': key, **(query or {})}
        req = Request('http://127.0.0.1:9117/api/v2.0/' + path + '?' + urlencode(params),
                      data=json.dumps(data).encode() if data is not None else None,
                      headers={'Content-Type': 'application/json'})
        raw = opener.open(req, timeout=timeout).read()
        return json.loads(raw) if raw else None
    active = api('indexers', query={'configured': 'true'})
    # Indexer choice belongs to this hotel's owner. A third-party website being offline
    # must not block installation or make us create/delete somebody's source settings.
    report = {'configured': [{'id': r['id'], 'name': r['name']} for r in active]}
    if not active: report['notice'] = 'Configure your indexers in the native Jackett UI'
    def save():
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2)); output.chmod(0o600)
    save()
    has_python = subprocess.run(['docker', 'exec', 'hotel-qbittorrent', 'python3', '--version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    report['qb_python_available'] = has_python
    if has_python:
        assert hashlib.sha256(plugin_file.read_bytes()).hexdigest() == PLUGIN_SHA256
        sys.path.insert(0, '/opt/hotel-tv/current/server')
        import downloads_engines
        db = sqlite3.connect('/srv/hotel-tv/data/events.sqlite3')
        settings = json.loads(db.execute("SELECT value FROM kv WHERE key='download_settings'").fetchone()[0]); db.close()
        secret = json.loads(Path('/srv/hotel-tv/data/download-secrets.json').read_text())['qbittorrent']['password']
        qb = downloads_engines.QBittorrent(settings['engines']['qbittorrent'], secret)
        try:
            qb._ensure()
            plugins = json.loads(qb._call('/api/v2/search/plugins'))
            installed = any(p['name']=='jackett' for p in plugins)
            before = {r['hash'] for r in json.loads(qb._call('/api/v2/torrents/info'))}
            if not installed:
                # docker cp preserves the private host download mode. The public plugin must
                # be readable by qB's unprivileged process; never relax a secret/config file.
                probe = subprocess.run(['docker', 'exec', 'hotel-qbittorrent', 'test', '-e', '/tmp/jackett.py'])
                if probe.returncode == 0:
                    digest = subprocess.check_output(['docker', 'exec', 'hotel-qbittorrent', 'sha256sum', '/tmp/jackett.py'], text=True).split()[0]
                    if digest != PLUGIN_SHA256: raise RuntimeError('Unrecognized existing plugin staging file')
                else:
                    subprocess.run(['docker', 'cp', str(plugin_file), 'hotel-qbittorrent:/tmp/jackett.py'], check=True)
                subprocess.run(['docker', 'exec', 'hotel-qbittorrent', 'chmod', '0644', '/tmp/jackett.py'], check=True)
                qb._call('/api/v2/search/installPlugin', {'sources': '/tmp/jackett.py'})
                for _ in range(120):
                    plugins = json.loads(qb._call('/api/v2/search/plugins'))
                    if any(p['name']=='jackett' for p in plugins): break
                    time.sleep(.25)
                else: raise RuntimeError('Native plugin installation timeout')
            paths = list(Path('/srv/nas/appdata/qbittorrent').rglob('nova3/engines/jackett.py'))
            assert len(paths)==1
            path = paths[0].with_suffix('.json')
            if not path.exists() or json.loads(path.read_text()).get('api_key') == 'YOUR_API_KEY_HERE':
                path.write_text(json.dumps({'api_key':key, 'url':'http://hotel-jackett:9117', 'tracker_first':False, 'thread_count':4}, indent=2))
            else:
                report['existing_configuration_preserved'] = True
            parent = path.parent.stat(); os.chown(path, parent.st_uid, parent.st_gid); path.chmod(0o600)
            assert {r['hash'] for r in json.loads(qb._call('/api/v2/torrents/info'))} == before
            report['qb_official_plugin'] = {'installed':True, 'sha256':PLUGIN_SHA256, 'configuration_mode':'0600', 'existing_torrents_preserved':True}
            subprocess.run(['docker', 'exec', 'hotel-qbittorrent', 'python3', '-c', "from pathlib import Path;p=Path('/tmp/jackett.py');p.unlink(missing_ok=True)"], check=True)
        finally:
            qb._call('/api/v2/auth/logout', {}); qb.password=''
    save()
    print(json.dumps(report, ensure_ascii=True))


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--plugin',type=Path,required=True);p.add_argument('--report',type=Path,required=True);args=p.parse_args()
    try: configure(args.plugin,args.report)
    except Exception as exc: raise SystemExit('Jackett configuration stopped: '+type(exc).__name__+' HTTP '+str(getattr(exc,'code','n/a')))
