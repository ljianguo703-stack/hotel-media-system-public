#!/usr/bin/env python3
"""Exercise native dpkg packaging and the activation hook in an offline disposable Ubuntu."""
import argparse
import json
import re
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=r'''import hashlib,json,os,subprocess,sys
from pathlib import Path
version=sys.argv[1];out=Path('/out');deb=out/('hotel-tv-install-agent_'+version+'_all.deb')
if not deb.exists():subprocess.run(['python3','/source/deploy/software/package_agent.py','--source','/source','--output',str(deb),'--version',version],check=True)
subprocess.run(['dpkg-deb','-x',str(deb),'/'],check=True)
subprocess.run(['dpkg-deb','-e',str(deb),'/control'],check=True)
base=Path('/opt/hotel-tv-installer');base.mkdir(parents=True)
current=base/'current';current.mkdir();(current/'sentinel').write_text('preserve')
Path('/etc/systemd/system').mkdir(parents=True,exist_ok=True);Path('/etc/systemd/system/hotel-tv-installer.path').write_text('fixture')
# This container has no systemd PID 1; record activation requests explicitly.
stub=Path('/usr/local/bin/systemctl');stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> /out/systemctl-calls.txt\n');stub.chmod(0o755)
r=subprocess.run(['/control/postinst','configure'],capture_output=True,text=True)
assert r.returncode!=0 and (current/'sentinel').read_text()=='preserve'
(current/'sentinel').unlink();current.rmdir()
old=base/'releases/legacy';old.mkdir(parents=True);(old/'sentinel').write_text('preserve')
current.symlink_to(old)
subprocess.run(['/control/postinst','configure'],check=True)
assert current.resolve()==Path('/usr/lib/hotel-tv-installer')
backup=Path('/var/lib/hotel-tv-installer/pre-managed-executor');assert backup.read_text().strip()==str(old)
assert (old/'sentinel').read_text()=='preserve' and backup.stat().st_mode&0o777==0o600
subprocess.run(['/control/postinst','configure'],check=True);assert backup.read_text().strip()==str(old)
assert Path('/etc/systemd/system/hotel-tv-installer.service.d/20-start-limits.conf').read_text()=='[Unit]\nStartLimitIntervalSec=0\n'
calls=(out/'systemctl-calls.txt').read_text();assert 'daemon-reload' in calls and 'reset-failed hotel-tv-installer.service hotel-tv-installer.path' in calls and 'restart' not in calls
manifest=json.loads((current/'manifest.json').read_text())
for name,digest in manifest['files'].items():assert hashlib.sha256((current/name).read_bytes()).hexdigest()==digest
subprocess.run(['python3','-E','-s',str(current/'software_agent.py'),'--help'],check=True,stdout=subprocess.DEVNULL)
assert set(manifest['files'])=={'software_agent.py','software_store.py','software_packages.py','fleet_agent.py','fleet_store.py'}
result={'package':deb.name,'sha256':hashlib.sha256(deb.read_bytes()).hexdigest(),'native_dpkg_extract':True,'activation_hook':True,'existing_directory_refused':True,'previous_link_preserved':True,'replay_preserves_backup':True,'five_module_hashes':True,'isolated_import_help':True,'start_limit_dropin':True,'activation_calls':'recording stub','network':'none','apt_dependency_install':'not tested','real_systemd_or_tv':'not tested'}
(out/'agent-package-check.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
'''

def main():
 p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--version',default='1.0.0~rc3');a=p.parse_args()
 if not re.fullmatch(r'[0-9][A-Za-z0-9.+~:-]{0,80}',a.version):raise ValueError('Invalid package version')
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=True)
 image=subprocess.check_output(['docker','--context','desktop-linux','image','inspect','hotel-sw01-test-python:latest','--format','{{.Id}}'],text=True).strip()
 subprocess.run(['docker','--context','desktop-linux','run','--rm','--network','none','-v',str(ROOT)+':/source:ro','-v',str(out)+':/out',image,'python3','-c',SCRIPT,a.version],check=True)
if __name__=='__main__':main()
