#!/usr/bin/env python3
"""Disposable loopback-only browser fixture. Never reads the real installation or secrets."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from management import Store, password_hash
from server import make_server

root = Path(tempfile.mkdtemp(prefix='hotel-media-mock-'))
data, media = root / 'data', root / 'media'; data.mkdir(); media.mkdir()
config = root / 'devices.json'; config.write_text('{"devices":{"test-tv":{"token":"pk02-mock-only","room":"101"}}}')
sample = media / 'ready.mp4'
subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=black:size=320x240:rate=24','-f','lavfi','-i','sine=frequency=440','-t','1','-c:v','libx264','-c:a','aac',str(sample)],check=True)
items=[]
for i,(ident,title) in enumerate([('ready','可上架技术片'),('blocked','等待兼容技术片'),('missing','缺失测试片'),('live','已上架测试片')]):
    if ident != 'ready': shutil.copyfile(sample,media/(ident+'.mp4'))
    items.append({'id':ident,'title':title,'kind':'movie','category':'电影','year':'2026','path':'/media/'+ident+'.mp4','jellyfin_id':str(i+1)*32})
(data/'catalog.json').write_text(json.dumps({'hotel':'PK02 隔离浏览器验收','items':items},ensure_ascii=False))
(data/'ads.json').write_text('{"ads":[]}')
store=Store(data,media,config,[media])
with store.db() as db:
    db.execute('UPDATE users SET password=? WHERE name=?',(password_hash('pk02-mock-test-only'),'admin'))
    db.execute('UPDATE media SET published=0');db.execute("UPDATE media SET available=0 WHERE id='missing'");db.execute("UPDATE media SET published=1 WHERE id='live'")
    store.put_kv(db,'media_automation',{'enabled':True})
    background=store.home_settings(db)['background']
    for row in db.execute('SELECT id,body FROM media').fetchall():
        item=json.loads(row['body']);item['poster']=background;db.execute('UPDATE media SET body=? WHERE id=?',(json.dumps(item,ensure_ascii=False),row['id']))
    revision=store.kv(db,'revision')
store.compat_probe(['ready','live'],'fixture',revision)
servers=[make_server('127.0.0.1',0,config,data,media,store=store),make_server('127.0.0.1',0,config,data,media,store=store,admin_web=Path(__file__).resolve().parents[1]/'admin')]
for server in servers:threading.Thread(target=server.serve_forever,daemon=True).start()
info={'url':'http://127.0.0.1:'+str(servers[1].server_port),'tv_url':'http://127.0.0.1:'+str(servers[0].server_port),'fixture_root':str(root)}
output=Path(__file__).resolve().parents[1]/'local/evidence/PK02';output.mkdir(parents=True,exist_ok=True);(output/'mock-info.json').write_text(json.dumps(info));print(json.dumps(info),flush=True)
try:
    while True:time.sleep(1)
except KeyboardInterrupt:
    for server in servers:server.shutdown();server.server_close()
    shutil.rmtree(root)
