#!/usr/bin/env python3
"""Check generated nginx syntax and cookie stripping with cached images, no hotel/network access."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[1]
DOCKER=['docker','--context','desktop-linux']

def main():
    spec=importlib.util.spec_from_file_location('native_proxy',ROOT/'deploy/nas/download-tools/install.py')
    proxy=importlib.util.module_from_spec(spec);spec.loader.exec_module(proxy)
    nginx=subprocess.check_output(DOCKER+['images','--filter','reference=nginx','--format','{{.ID}}'],text=True).splitlines()[0]
    python=subprocess.check_output(DOCKER+['image','inspect','hotel-sw01-test-python:latest','--format','{{.Id}}'],text=True).strip()
    with tempfile.TemporaryDirectory(prefix='hotel-in17-nginx-') as folder:
        config=Path(folder)/'default.conf'
        config.write_text(proxy.proxy_config('127.0.0.1')+'\nserver { listen 127.0.0.1:8080; location / { return 200 "$native_cookie"; } }\n')
        mount=['-v',str(config)+':/etc/nginx/conf.d/default.conf:ro']
        subprocess.run(DOCKER+['run','--rm','--network','none',*mount,nginx,'nginx','-t'],check=True)
        container=subprocess.check_output(DOCKER+['run','-d','--network','none',*mount,nginx],text=True).strip()
        try:
            code=r'''import http.client,time,json
cases={'hotel_session=fake-secret':'','SID=native; hotel_session=fake-secret; lang=zh':'SID=native; lang=zh','hotel_session=fake-secret; SID=native':'SID=native','SID=native; hotel_session=fake-secret':'SID=native; ','native_hotel_session=preserved':'native_hotel_session=preserved'}
for attempt in range(50):
    try:
        connection=http.client.HTTPConnection('127.0.0.1',8080,timeout=1);connection.request('GET','/');connection.getresponse().read();break
    except OSError:time.sleep(.1)
for cookie,wanted in cases.items():
    connection=http.client.HTTPConnection('127.0.0.1',8080,timeout=2);connection.request('GET','/',headers={'Cookie':cookie})
    response=connection.getresponse();actual=response.read().decode();assert response.status==200 and actual==wanted,(cookie,actual,wanted)
print(json.dumps({'nginx_syntax':True,'cookie_cases':len(cases),'network':'isolated container loopback','production_touched':False}))
'''
            subprocess.run(DOCKER+['run','--rm','--network','container:'+container,python,'python3','-c',code],check=True)
        finally:subprocess.run(DOCKER+['rm','-f',container],check=True,stdout=subprocess.DEVNULL)

if __name__=='__main__':main()
