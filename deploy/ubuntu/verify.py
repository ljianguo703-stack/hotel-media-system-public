#!/usr/bin/env python3
"""Smoke-test one deployed pilot without printing device credentials."""
import argparse
import json
from pathlib import Path
import sqlite3
import urllib.error
import urllib.request
import uuid
import http.cookiejar


def verify(url, config, preview_url=None, database=None, admin_password_file=None):
    devices = json.loads(Path(config).read_text(encoding='utf-8'))['devices']
    device, entry = next(iter(devices.items()))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, token=entry['token'], method='GET', headers=None, body=None):
        h = {'X-TV-Token':token, 'X-TV-Probe':'1'}; h.update(headers or {})
        req = urllib.request.Request(url.rstrip('/')+path, data=body, headers=h, method=method)
        try:
            with opener.open(req,timeout=8) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as response:
            return response.code, dict(response.headers), response.read()

    results = {}
    results['unauthorized_rejected'] = request('/api/v1/health',token='invalid-deployment-probe')[0] == 401
    status, _, data = request('/api/v1/health')
    results['authenticated_health'] = status == 200 and json.loads(data)['ok'] is True
    status, _, data = request('/api/v1/catalog'); catalog = json.loads(data)
    results['catalog'] = status == 200 and isinstance(catalog.get('items'),list) and bool(catalog.get('version'))
    results['catalog_count'] = len(catalog.get('items',[]))
    status, _, data = request('/api/v1/ads'); ads = json.loads(data)
    results['ads'] = status == 200 and isinstance(ads.get('ads'),list) and bool(ads.get('version'))
    results['ad_count'] = len(ads.get('ads',[]))
    first = next((item.get('path') or next(iter(item.get('episodes',[])),{}).get('path') for item in catalog['items']
                  if item.get('path') or item.get('episodes')), None)
    if first:
        assert first.startswith('/media/')
        status, headers, body = request(first,headers={'Range':'bytes=0-31'})
        results['media_range'] = status == 206 and len(body) == 32 and headers.get('Content-Range','').startswith('bytes 0-31/')
        status, headers, body = request(first,method='HEAD')
        results['media_head'] = status == 200 and not body and int(headers.get('Content-Length','0')) > 0
    event_id = 'deployment-probe-'+str(uuid.uuid4())
    raw = json.dumps({'events':[{'id':event_id,'type':'deployment_probe','exclude_from_ad_reports':True}]}).encode()
    accepted = []
    for _ in range(2):
        status, _, body = request('/api/v1/events',method='POST',headers={'Content-Type':'application/json'},body=raw)
        accepted.append(status == 200 and json.loads(body).get('accepted') == [event_id])
    results['events_accepted'] = all(accepted)
    if database:
        try:
            with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro&immutable=1',uri=True) as conn:
                results['event_retry_idempotent'] = conn.execute('SELECT count(*) FROM events WHERE id=?',(device+':'+event_id,)).fetchone()[0] == 1
        except sqlite3.Error as exc:
            raise RuntimeError('数据库只读验证失败，请以 hotel-tv 服务账户运行并检查路径/读取权限') from exc
    if preview_url:
        with opener.open(preview_url,timeout=8) as response:
            html = response.read().decode('utf-8')
            results['admin_login_page'] = response.status == 200 and '酒店影院管理' in html
        with opener.open(preview_url.rstrip('/')+'/api/admin/session',timeout=8) as response:
            results['admin_requires_login'] = json.loads(response.read()) == {'user':None}
        if admin_password_file:
            credentials = dict(line.split('=',1) for line in Path(admin_password_file).read_text().splitlines() if '=' in line)
            admin = urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            origin = preview_url.rstrip('/')
            req=urllib.request.Request(origin+'/api/admin/login',data=json.dumps({'username':credentials['username'],'password':credentials['password']}).encode(),headers={'Content-Type':'application/json'})
            with admin.open(req,timeout=8) as response: session=json.loads(response.read())['user']
            with admin.open(origin+'/api/admin/state',timeout=8) as response: state=json.loads(response.read())
            results['admin_authenticated_state'] = session['role']=='admin' and bool(state['sources']) and state['storage']['default'].startswith('/')
            req=urllib.request.Request(origin+'/api/admin/logout',data=b'{}',headers={'Content-Type':'application/json','X-CSRF-Token':session['csrf']})
            with admin.open(req,timeout=8) as response: results['admin_logout']=response.status==200
    assert all(v for v in results.values() if isinstance(v,bool)), results
    return results


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--url',required=True);p.add_argument('--config',required=True,type=Path)
    p.add_argument('--preview-url');p.add_argument('--database',type=Path)
    p.add_argument('--admin-password-file',type=Path)
    a=p.parse_args();print(json.dumps(verify(a.url,a.config,a.preview_url,a.database,a.admin_password_file),indent=2))
