#!/usr/bin/env python3
"""Run one browser acceptance against a fresh disposable fixture, then stop only that fixture."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import signal
import time

ROOT=Path(__file__).resolve().parents[1]

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--script',choices=('ad12_bundle_e2e.cjs','ad11_guide_e2e.cjs','mock_e2e.cjs','ad13_media_e2e.cjs','av01_audio_e2e.cjs','ad14_validation_e2e.cjs','ad15_visual_e2e.cjs','passwords_e2e.cjs','home_backgrounds_e2e.cjs','nas_navigation_e2e.cjs','media_center_e2e.cjs','fleet_e2e.cjs'),default='ad12_bundle_e2e.cjs');p.add_argument('--source',type=Path,default=ROOT);a=p.parse_args()
    out=ROOT/'local/evidence/browser';out.mkdir(parents=True,exist_ok=True)
    marker=ROOT/('local/evidence/V110/mock-info.json' if a.script=='media_center_e2e.cjs' else 'artifacts/management/AD08/dev.json')
    before=marker.stat().st_mtime_ns if marker.exists() else 0
    with (out/(a.script+'.fixture.log')).open('w') as log:
        env=dict(os.environ,HOTEL_MOCK_PROGRAM_ROOT=str(a.source.resolve()))
        proc=subprocess.Popen([sys.executable,str(ROOT/('tools/media_center_mock.py' if a.script=='media_center_e2e.cjs' else 'tools/mock_dev.py')),*(['--media-cleanup'] if a.script=='ad13_media_e2e.cjs' else ['--audio-compat'] if a.script=='av01_audio_e2e.cjs' else ['--nas-navigation'] if a.script=='nas_navigation_e2e.cjs' else ['--visuals'] if a.script=='ad15_visual_e2e.cjs' else ['--fleet'] if a.script=='fleet_e2e.cjs' else [])],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
        try:
            for _ in range(200):
                if proc.poll() is not None: raise RuntimeError('temporary fixture did not start')
                try:
                    if marker.stat().st_mtime_ns>before:
                        info=json.loads(marker.read_text())
                        if info['url'].startswith('http://127.0.0.1:'):break
                except (OSError,ValueError):pass
                time.sleep(.1)
            else:raise RuntimeError('temporary fixture startup timed out')
            return subprocess.run(['node',str(ROOT/'tools'/a.script)],cwd=ROOT).returncode
        finally:
            proc.send_signal(signal.SIGINT)
            try:proc.wait(timeout=10)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()

if __name__=='__main__':sys.exit(main())
