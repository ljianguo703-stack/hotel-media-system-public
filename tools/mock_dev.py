#!/usr/bin/env python3
"""Disposable loopback-only AD08 acceptance fixture. Never reads production configuration."""
import json, os, shutil, subprocess, sys, tempfile, threading, time, zlib, struct
from pathlib import Path
program=Path(os.environ.get('HOTEL_MOCK_PROGRAM_ROOT',str(Path(__file__).resolve().parents[1]))).resolve()
sys.path.insert(0,str(program/'server'))
from management import Store, password_hash
from server import make_server
# Warm the optional mounted-volume cache before the disposable fixture's first state read.
if (program/'server/library_scan.py').is_file():
    import library_scan
    library_scan.mounted_volumes(refresh=True)
root=Path(tempfile.mkdtemp(prefix='hotel-ad08-'));data=root/'data';media=root/'media'
data.mkdir();media.mkdir();config=root/'devices.json'
config.write_text(json.dumps({'devices':{'test-tv':{'token':'ad08-test-only','room':'101','group':'全部客房'}}}))
(data/'catalog.json').write_text(json.dumps({'hotel':'山间酒店','items':[]}));(data/'ads.json').write_text('{"ads":[]}')
def png(name, rgb, w=960, h=540, border=False):
    rows=[]
    for y in range(h):
        row=bytearray()
        for x in range(w):
            f=.45+.55*x/w;color=tuple(int(c*f) for c in rgb)
            if border:
                if x<12:color=(220,40,40)
                if x>=w-12:color=(50,200,80)
                if y<12:color=(255,80,0)
                if y>=h-12:color=(40,90,230)
            row.extend(color)
        rows.append(b'\0'+row)
    def chunk(kind,payload):return struct.pack('>I',len(payload))+kind+payload+struct.pack('>I',zlib.crc32(kind+payload)&0xffffffff)
    (media/name).write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',w,h,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b''.join(rows)))+chunk(b'IEND',b''))
png('sponsor.png',(31,95,100))
if '--visuals' in sys.argv:
    png('portrait.png',(70,110,150),360,640,True)
    png('landscape.png',(70,110,150),960,540,True)
    png('long.png',(70,110,150),360,1200,True)
if '--nas-navigation' in sys.argv:
    # No native NPC daemon on this host. Keep HTTP authentication real; fake only its local RPC.
    import npc_client
    npc_client.request=lambda *args,**kwargs: {'config': {'revision': 0, 'enabled': True, 'server': '', 'key_set': False, 'type': 'tcp', 'dns': '', 'extra': ''}, 'runtime': {'state': 'waiting_config'}}
store=Store(data,media,config,[media]);user={'name':'admin','role':'admin'}
with store.db() as db:db.execute('UPDATE users SET password=? WHERE name=?',(password_hash('ad08-local-test'),'admin'))
for i,(name,industry) in enumerate([('青禾餐厅','餐饮'),('山风出行','交通'),('云水茶室','茶饮')]):
    store.save_entity('advertiser',{'id':'sponsor-'+str(i),'name':name,'industry':industry,'enabled':True},'admin',store.state()['revision'])
    store.save_campaign('sponsor-'+str(i),dict(name=name+'全屏',merchant=name,tier=1,format='图文',path='',text='',status='投放中',duration=15,priority=50,gap_seconds=300,cap=2,rotation_order=i,rotation_enabled=True,
        placements=['home','preroll','midroll','pause','postroll'],group='全部客房',timezone='Asia/Singapore',start='00:00',end='00:00',days=[1,2,3,4,5,6,7],
        creative_card=dict(title=name,body='慢下来，享受旅途中的美好时光。',contact='酒店一层 · 服务电话 123456',image='/media/sponsor.png',qr_link='https://example.com/contact',accent='#214B53')), 'admin',store.state()['revision'])
if '--audio-compat' in sys.argv:
    target=media/'dolby-demo.mkv'
    subprocess.run(['ffmpeg','-v','error','-nostdin','-n','-f','lavfi','-i','color=c=0x284b59:s=320x240:r=24:d=3','-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=3','-c:v','libx264','-pix_fmt','yuv420p','-c:a','eac3','-ac','6',str(target)],check=True)
    item={'id':'dolby-demo','title':'Dolby Audio Test','kind':'movie','path':'/media/dolby-demo.mkv','category':'test','year':'2026'}
    with store.db() as db:db.execute('INSERT INTO media VALUES(?,?,?,?,?)',('dolby-demo','main',json.dumps(item),1,1))
if '--media-cleanup' in sys.argv:
    stamp=int(time.time()*1000)
    for ident,title,size,age,pinned in [('cold-large','山间往事',2*1024*1024,40,False),('cold-small','雨后小路',1024*1024,40,False),('warm','海边来信',512*1024,40,False),('new-film','新片保护示例',1024,2,False),('pinned-film','置顶保护示例',1024,40,True)]:
        (media/(ident+'.mp4')).write_bytes(b'0'*size)
        item={'id':ident,'title':title,'kind':'movie','path':'/media/'+ident+'.mp4','_created_at':stamp-age*86400000,'_pinned':pinned,'category':'剧情','year':2026}
        with store.db() as db:db.execute('INSERT INTO media VALUES(?,?,?,?,?)',(ident,'main',json.dumps(item),1,1))
    for n in range(2):
        event={'type':'content_started','content_id':'warm','client_session_id':'watch-'+str(n),'at_ms':stamp-(10+n)*86400000}
        with store.db() as db:db.execute('INSERT INTO events VALUES(?,?,?,?)',('watch-'+str(n),'test-tv',event['at_ms'],json.dumps(event)))
if '--fleet' in sys.argv:
    import fleet_mock_seed
    fleet_mock_seed.seed(store)
servers=[make_server('127.0.0.1',0,config,data,media,store=store),make_server('127.0.0.1',0,config,data,media,store=store,admin_web=program/'admin')]
for s in servers:threading.Thread(target=s.serve_forever,daemon=True).start()
info={'url':'http://127.0.0.1:'+str(servers[1].server_port),'tv_url':'http://127.0.0.1:'+str(servers[0].server_port),'data':str(root)}
output=Path(__file__).resolve().parents[1]/'artifacts/management/AD08';output.mkdir(parents=True,exist_ok=True);(output/'dev.json').write_text(json.dumps(info));print(json.dumps(info),flush=True)
try:
    while True:
        time.sleep(1)
        if '--fleet' in sys.argv: fleet_mock_seed.tick(store,root/'fake-fleet-agent')
except KeyboardInterrupt:
    for s in servers:s.shutdown();s.server_close()
    shutil.rmtree(root)
