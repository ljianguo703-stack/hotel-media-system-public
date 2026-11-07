#!/usr/bin/env python3
"""Disposable dashboard fixture. Linux counters stay real, only hotel data is synthetic."""
import argparse
import json
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
from management import Store,password_hash
from server import make_server

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container-test',action='store_true',help='Expose only inside a disposable test container; publish host ports on loopback.')
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='hotel-console-fixture-') as folder:
        root=Path(folder);data=root/'data';media=root/'media';data.mkdir();media.mkdir()
        (media/'clip.mp4').write_bytes(bytes(range(256))*4096)
        (data/'catalog.json').write_text(json.dumps({'hotel':'验收酒店','items':[{'id':'clip','title':'验收影片','kind':'movie','path':'/media/clip.mp4'}]}))
        (data/'ads.json').write_text('{"ads":[]}')
        config=root/'devices.json';config.write_text(json.dumps({'devices':{
            'tv-101':{'room':'101','group':'1楼','token':'in20-fixture-101'},
            'tv-201':{'room':'201','group':'2楼','token':'in20-fixture-201'},
            'tv-offline':{'room':'999','group':'3楼','token':'in20-fixture-offline'}}}))
        store=Store(data,media,config,[media])
        with store.db() as db:
            db.execute('UPDATE users SET password=? WHERE name=?',(password_hash('in20-local-test'),'admin'))
            for role in ('operator','viewer'):db.execute('INSERT INTO users VALUES(?,?,?,1)',(role,password_hash('in20-local-test'),role))
        host='0.0.0.0' if args.container_test else '127.0.0.1'
        servers=[make_server(host,18790,config,data,media,store=store),make_server(host,18791,config,data,media,store=store,admin_web=ROOT/'admin')]
        for server in servers:threading.Thread(target=server.serve_forever,daemon=True).start()
        stop=threading.Event()
        for signum in (signal.SIGINT,signal.SIGTERM):signal.signal(signum,lambda *_:stop.set())
        print(json.dumps({'url':'http://127.0.0.1:18791','tv_url':'http://127.0.0.1:18790'}),flush=True)
        stop.wait()
        for server in servers:server.shutdown();server.server_close()

if __name__=='__main__':main()
