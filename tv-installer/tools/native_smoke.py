#!/usr/bin/env python3
"""Read-only startup tests. Never detects or connects any television."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.request
import zipfile
import shutil
from release_config import ART,VERSION

ROOT=Path(__file__).resolve().parents[1]
IMAGE='ubuntu@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517'


def mac(arch):
    expected_profiles=len(json.loads((ROOT/'catalog.json').read_text())['profiles'])
    with tempfile.TemporaryDirectory(prefix='电视安装 开箱IN02-') as temp:
        with zipfile.ZipFile(ART/f'release/hotel-tv-installer-{VERSION}-darwin-{arch}.zip') as z:
            z.extractall(temp)
            for info in z.infolist():
                (Path(temp)/info.filename).chmod(info.external_attr>>16&0o777)
        folder=Path(temp)/f'hotel-tv-installer-{VERSION}-darwin-{arch}'
        executable=folder/'hotel-tv-installer'
        adb=subprocess.check_output([str(folder/'platform-tools/adb'),'version'],text=True,timeout=15)
        assert '37.0.1' in adb
        log=Path(temp)/'startup.txt'
        args=[str(executable),'--no-browser','--state',str(Path(temp)/'state')]
        if arch=='amd64':args=['arch','-x86_64']+args
        with log.open('w') as output:
            p=subprocess.Popen(args,stdout=output,stderr=subprocess.STDOUT)
        try:
            deadline=time.monotonic()+30;match=None
            while time.monotonic()<deadline:
                match=re.search(r'http://(127\.0\.0\.1:\d+)/#([a-f0-9]{32})',log.read_text())
                if match:break
                if p.poll() is not None:raise RuntimeError('native startup failed')
                time.sleep(.1)
            assert match, 'native startup timed out'
            req=urllib.request.Request('http://'+match[1]+'/api/state',headers={'X-App-Token':match[2]})
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            state=json.load(opener.open(req,timeout=5))
            assert len(state['catalog']['profiles'])==expected_profiles and not state['has_pin']
            assert state['version']==VERSION and len(state['attachments'])==3 and all(r['available'] for r in state['attachments'])
            doctor=urllib.request.Request('http://'+match[1]+'/api/doctor',headers={'X-App-Token':match[2],'Content-Type':'application/json'},data=b'{}')
            assert 'Android Debug Bridge' in json.load(opener.open(doctor,timeout=10))['adb']
            duplicate=subprocess.run(args,capture_output=True,text=True,timeout=10)
            assert duplicate.returncode==1 and '已有工具运行' in duplicate.stderr
            return dict(host=state['host'],version=state['version'],records=expected_profiles,fresh_state=True,
                        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),unicode_and_spaces_path=True,bundled_adb=True,attachments=3)
        finally:
            p.terminate()
            try:p.wait(timeout=10)
            except subprocess.TimeoutExpired:p.kill();p.wait()


def main():
    result=dict(mac=[mac('arm64'),mac('amd64')],second_instance_rejected=True)
    folder=ART/f'release/hotel-tv-installer-{VERSION}-linux-amd64'
    cmd=['docker','--context','desktop-linux','run','--rm','--platform','linux/amd64','--network','none','--read-only',
         '--tmpfs','/tmp','--tmpfs','/root','-v',str(folder)+':/app:ro',IMAGE]
    version=subprocess.check_output(cmd+['/app/platform-tools/adb','version'],text=True,timeout=30)
    assert '37.0.1' in version
    p=subprocess.run(cmd+['timeout','3','/app/hotel-tv-installer','--no-browser','--state','/tmp/hotel-tv-installer'],
                     capture_output=True,text=True,timeout=30)
    assert p.returncode==124 and '酒店电视安装工具 v'+VERSION in p.stdout,p.stderr
    result['linux']='Ubuntu amd64 container (CPU emulation): bundled ADB and executable startup passed; no TV connected'
    result['windows']='x64 cross compilation and PE structure verified; not executed on Windows'
    (ART/'native-smoke.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
