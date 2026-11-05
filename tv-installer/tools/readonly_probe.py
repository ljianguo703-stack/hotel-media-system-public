#!/usr/bin/env python3
"""Run only /api/probe from a built installer; never installs or changes TV settings."""
import argparse,hashlib,json,re,subprocess,tempfile,time,urllib.request
from pathlib import Path
from release_config import ART,VERSION

p=argparse.ArgumentParser();p.add_argument('--bundle',type=Path,required=True);p.add_argument('--target',required=True);args=p.parse_args()
with tempfile.TemporaryDirectory(prefix='ti06-readonly-') as tmp:
    log=Path(tmp)/'startup.log'
    with log.open('w') as out:
        child=subprocess.Popen([str(args.bundle/'hotel-tv-installer'),'--no-browser','--state',str(Path(tmp)/'state')],stdout=out,stderr=subprocess.STDOUT)
    try:
        match=None
        for _ in range(100):
            match=re.search(r'http://(127\.0\.0\.1:\d+)/#([a-f0-9]{32})',log.read_text())
            if match:break
            if child.poll() is not None:raise RuntimeError('Installer startup failed')
            time.sleep(.1)
        assert match
        request=urllib.request.Request('http://'+match[1]+'/api/probe',headers={'X-App-Token':match[2],'Content-Type':'application/json'},data=json.dumps({'target':args.target}).encode())
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        result=json.load(opener.open(request,timeout=90))
        assert result['client_installed'] is False and result['decision']['allowed'] is True
        proof=dict(result='PASS',version=VERSION,executable_sha256=hashlib.sha256((args.bundle/'hotel-tv-installer').read_bytes()).hexdigest(),operation='read-only installer /api/probe; no install/config/settings operation',facts=result['facts'],client_installed=False)
        (ART/'read-only-hotfix-verification.json').write_text(json.dumps(proof,indent=2)+'\n')
        print(json.dumps(proof,ensure_ascii=False))
    finally:
        child.terminate();child.wait(timeout=10)
