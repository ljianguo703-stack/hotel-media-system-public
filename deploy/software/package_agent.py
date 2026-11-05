#!/usr/bin/env python3
"""Package the privileged fixed-operation worker separately from the web program."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

FILES = ('software_agent.py', 'software_store.py', 'software_packages.py', 'fleet_agent.py', 'fleet_store.py')
POSTINST = '''#!/bin/sh
set -eu
case "${1:-}" in configure) ;; *) exit 0 ;; esac
base=/opt/hotel-tv-installer
current=$base/current
[ ! -e "$current" ] || [ -L "$current" ] || { echo 'Executor current must be a symlink; preserved existing directory' >&2; exit 1; }
[ -f /usr/lib/hotel-tv-installer/fleet_agent.py ]
[ -f /etc/systemd/system/hotel-tv-installer.path ] || { echo "Install the full hotel server before updating its worker" >&2; exit 1; }
install -d -m 0755 "$base"
install -d -m 0700 /var/lib/hotel-tv-installer
if [ -L "$current" ] && [ ! -e /var/lib/hotel-tv-installer/pre-managed-executor ]; then
    (umask 077; readlink "$current" > /var/lib/hotel-tv-installer/pre-managed-executor)
fi
next=$base/.managed-next-$$
trap 'rm -f "$next"' EXIT HUP INT TERM
ln -s /usr/lib/hotel-tv-installer "$next"
mv -Tf "$next" "$current"
install -d -m 0755 /etc/systemd/system/hotel-tv-installer.service.d
install -m 0644 /usr/lib/hotel-tv-installer/20-start-limits.conf /etc/systemd/system/hotel-tv-installer.service.d/20-start-limits.conf
systemctl daemon-reload
systemctl reset-failed hotel-tv-installer.service hotel-tv-installer.path || true
systemctl start hotel-tv-installer.path
# Do not restart the worker installing this package. The next activation uses the new code.
'''


def build(source, output, version):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not re.fullmatch(r'[0-9][A-Za-z0-9.+~:-]{0,80}', version): raise ValueError('Invalid package version')
    if output.exists(): raise ValueError('Preserve the existing output')
    with tempfile.TemporaryDirectory(prefix='hotel-executor-deb-') as directory:
        root=Path(directory);root.chmod(0o755)
        target=root/'usr/lib/hotel-tv-installer';target.mkdir(parents=True)
        hashes={}
        for name in FILES:
            src=source/'server'/name
            if src.is_symlink() or not src.is_file():raise ValueError('Invalid executor source')
            compile(src.read_bytes(),name,'exec')
            shutil.copyfile(src,target/name);(target/name).chmod(0o644)
            hashes[name]=hashlib.sha256(src.read_bytes()).hexdigest()
        (target/'20-start-limits.conf').write_text('[Unit]\nStartLimitIntervalSec=0\n')
        (target/'20-start-limits.conf').chmod(0o644)
        (target/'manifest.json').write_text(json.dumps({'protocol':1,'version':version,'files':hashes},indent=2)+'\n')
        (target/'manifest.json').chmod(0o644)
        control=root/'DEBIAN';control.mkdir()
        (control/'control').write_text('Package: hotel-tv-install-agent\nVersion: '+version+'\nArchitecture: all\nMaintainer: Hotel Media System\nDepends: python3 (>= 3.9), adb, aapt, apksigner, default-jre-headless\nDescription: Fixed-operation hotel TV maintenance executor\n Separate worker update; no service restart, device changes or credential reset.\n')
        (control/'control').chmod(0o644)
        (control/'postinst').write_text(POSTINST);(control/'postinst').chmod(0o755)
        for p in root.rglob('*'):
            if p.is_dir():p.chmod(0o755)
        output.parent.mkdir(parents=True,exist_ok=True)
        subprocess.run(['dpkg-deb','-Zgzip','--build','--root-owner-group',str(root),str(output)],check=True,stdout=subprocess.DEVNULL)
    return {'file':output.name,'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'version':version,'files':hashes,'worker_restart':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',required=True,type=Path);p.add_argument('--output',required=True,type=Path);p.add_argument('--version',required=True)
    a=p.parse_args();print(json.dumps(build(a.source,a.output,a.version)))
