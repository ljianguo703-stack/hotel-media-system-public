#!/usr/bin/env python3
"""Wrap the audited release archive in one executable Ubuntu entry point."""
import argparse
import hashlib
import io
from pathlib import Path
import re
import sys
import tarfile
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_release import validate_names

STUB = r'''#!/bin/sh
set -eu
# Verify the embedded archive before installing even a missing interpreter.
hotel_payload_line=$(LC_ALL=C sed -n '/^__HOTEL_PAYLOAD__$/ {=;q;}' "$0")
[ -n "$hotel_payload_line" ] || { echo 'Missing installation payload' >&2; exit 2; }
hotel_payload_line=$((hotel_payload_line + 1))
if command -v sha256sum >/dev/null 2>&1; then
    hotel_payload_hash=$(tail -n +"$hotel_payload_line" "$0" | sha256sum)
elif command -v shasum >/dev/null 2>&1; then
    hotel_payload_hash=$(tail -n +"$hotel_payload_line" "$0" | shasum -a 256)
else
    echo '缺少SHA256校验工具，未安装任何内容；Ubuntu自带的coreutils须可用。' >&2
    exit 2
fi
hotel_payload_hash=${hotel_payload_hash%% *}
[ "$hotel_payload_hash" = '__SHA__' ] || { echo 'Installation checksum mismatch; nothing installed' >&2; exit 2; }
__RUNTIME_BOOTSTRAP__
hotel_ensure_python "$@" || exit "$?"
exec "$HOTEL_PYTHON" - "$0" "$@" <<'HOTEL_PYTHON'
import hashlib, io, os
from pathlib import Path
import subprocess, sys, tarfile, tempfile
version = '__VERSION__'
expected = '__SHA__'
source = Path(sys.argv[1]).read_bytes()
marker = b'\n__HOTEL_PAYLOAD__\n'
offset = source.find(marker)
if offset < 0: raise SystemExit('Missing installation payload')
payload = source[offset+len(marker):]
if hashlib.sha256(payload).hexdigest() != expected:
    raise SystemExit('Installation checksum mismatch; nothing installed')
arguments = sys.argv[2:]
preview = '--plan' in arguments or '--help' in arguments
if not preview and os.geteuid() != 0:
    raise SystemExit('Please run: sudo bash '+sys.argv[1]+' (existing systems must not run first-install)')
def extract(destination):
    with tarfile.open(fileobj=io.BytesIO(payload),mode='r:gz') as archive:
        members = archive.getmembers()
        if len(members)>10000 or sum(m.size for m in members)>200*1024*1024: raise SystemExit('Oversized payload')
        for member in members:
            relative=Path(member.name)
            if relative.is_absolute() or '..' in relative.parts or member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise SystemExit('Unsafe archive entry')
            target=destination/relative
            if member.isdir(): target.mkdir(parents=True,exist_ok=True)
            else:
                target.parent.mkdir(parents=True,exist_ok=True)
                with target.open('xb') as out:out.write(archive.extractfile(member).read())
                target.chmod(member.mode & 0o755)
    (destination/'.package-sha256').write_text(expected+'\n')
if preview:
    with tempfile.TemporaryDirectory(prefix='hotel-install-plan-') as folder:
        root=Path(folder);extract(root)
        raise SystemExit(subprocess.call([sys.executable,str(root/'deploy/bootstrap.py'),*arguments]))
base=Path('/var/lib/hotel-tv-setup/sources');base.mkdir(parents=True,exist_ok=True,mode=0o700)
root=base/version
if root.is_symlink():raise SystemExit('Unsafe setup directory')
if root.exists():
    proof=root/'.package-sha256'
    if not proof.is_file() or proof.read_text().strip()!=expected:raise SystemExit('Existing setup source differs; not overwritten')
else:
    with tempfile.TemporaryDirectory(prefix='.extract-',dir=base) as folder:
        temp=Path(folder);extract(temp);temp.rename(root)
os.execv(sys.executable,[sys.executable,str(root/'deploy/bootstrap.py'),*arguments])
HOTEL_PYTHON
exit 1
__HOTEL_PAYLOAD__
'''


def build(archive, output, version):
    archive, output = Path(archive), Path(output)
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', version): raise ValueError('Invalid version')
    payload = archive.read_bytes()
    with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as stream:
        validate_names(stream.getnames())
        if any(m.issym() or m.islnk() for m in stream.getmembers()): raise ValueError('No links in installer')
        if stream.extractfile('VERSION').read().decode().strip() != version: raise ValueError('Version mismatch')
        for name in ('install.sh', 'deploy/bootstrap.py'): stream.getmember(name)
        runtime = stream.extractfile('deploy/ensure-runtime.sh').read().decode()
    if output.exists(): raise ValueError('Output already exists')
    digest = hashlib.sha256(payload).hexdigest()
    output.write_bytes(STUB.replace('__RUNTIME_BOOTSTRAP__', runtime).replace('__VERSION__', version).replace('__SHA__', digest).encode() + payload)
    output.chmod(0o755)
    return {'file': output.name, 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(), 'payload_sha256': digest}


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--archive',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--version',required=True)
    args=parser.parse_args();print(__import__('json').dumps(build(args.archive,args.output,args.version)))
