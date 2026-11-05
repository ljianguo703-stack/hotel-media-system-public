#!/bin/sh
# Install only the Hotel TV pilot. Do not change disks, networking, SSH, or unrelated services.
set -eu
umask 027
payload=${1:?usage: install.sh PAYLOAD_DIRECTORY RELEASE_ID [MEDIA_OPERATOR]}
release_id=${2:?missing release id}
operator=${3:-}
case "$release_id" in ''|*[!a-zA-Z0-9_-]*) echo 'Invalid release id' >&2; exit 2;; esac
[ "$(id -u)" = 0 ] || { echo 'Run this installer with sudo' >&2; exit 2; }
[ -f "$payload/server/server.py" ] && [ -f "$payload/seed/devices.json" ] || { echo 'Incomplete payload' >&2; exit 2; }
command -v python3 >/dev/null
command -v systemctl >/dev/null
command -v flock >/dev/null
exec 9>/var/lock/hotel-tv-deploy.lock
flock -n 9 || { echo 'Another Hotel TV deployment is running; retry after it finishes' >&2; exit 2; }
if [ -f "$payload/upgrade-only" ]; then
    [ -f /etc/hotel-tv/devices.json ] && [ -f /srv/hotel-tv/data/catalog.json ] || { echo 'Upgrade-only bundle requires an existing pilot deployment' >&2; exit 2; }
fi
python3 -c 'import sqlite3, json, http.server'

for name in hotel-tv hotel-tv-preview; do
    getent group "$name" >/dev/null || groupadd --system "$name"
    id -u "$name" >/dev/null 2>&1 || useradd --system --gid "$name" --home-dir /nonexistent --shell /usr/sbin/nologin "$name"
done
install -d -m 0755 /opt/hotel-tv /opt/hotel-tv/releases
install -d -m 0750 -o root -g hotel-tv /etc/hotel-tv
install -d -m 0750 -o hotel-tv -g hotel-tv /srv/hotel-tv/data
install -d -m 2775 -o hotel-tv -g hotel-tv /srv/hotel-tv/media
install -d -m 0700 /var/backups/hotel-tv

if [ -e /opt/hotel-tv/current ] && [ ! -L /opt/hotel-tv/current ]; then
    echo '/opt/hotel-tv/current already exists as a normal directory; refusing to replace it' >&2
    exit 2
fi
release=/opt/hotel-tv/releases/$release_id
[ ! -e "$release" ] || { echo 'Release already exists; use a fresh release id' >&2; exit 2; }
backup=/var/backups/hotel-tv/$release_id
install -d -m 0700 "$backup"
if [ -L /opt/hotel-tv/current ]; then readlink /opt/hotel-tv/current > "$backup/previous-release.txt"; fi
for unit in hotel-tv.service hotel-tv-admin-preview.service; do
    if [ -f "/etc/systemd/system/$unit" ]; then cp -p "/etc/systemd/system/$unit" "$backup/$unit"; fi
done
# Only our manifests and SQLite database are backed up; media files are never rewritten.
python3 - "$backup" <<'PY'
import pathlib, shutil, sqlite3, sys
dest = pathlib.Path(sys.argv[1])
for p in [pathlib.Path('/srv/hotel-tv/data/catalog.json'), pathlib.Path('/srv/hotel-tv/data/ads.json'), pathlib.Path('/etc/hotel-tv/devices.json')]:
    if p.is_file(): shutil.copy2(p, dest / p.name)
p = pathlib.Path('/srv/hotel-tv/data/events.sqlite3')
if p.is_file():
    with sqlite3.connect(str(p)) as source, sqlite3.connect(str(dest / p.name)) as target: source.backup(target)
PY
install -d -m 0755 "$release"
cp -R "$payload/server" "$payload/admin" "$payload/units" "$release/"
cp "$payload/LICENSE" "$payload/NOTICE" "$release/"
chown -R root:root "$release"
find "$release" -type d -exec chmod 0755 {} \;
find "$release" -type f -exec chmod 0644 {} \;

# Seed only missing files. Existing hotel manifests, credentials, events and films survive reruns.
python3 - "$payload/seed" <<'PY'
import os, pathlib, pwd, shutil, sys
seed = pathlib.Path(sys.argv[1]); owner = pwd.getpwnam('hotel-tv')
pairs = [(seed/'devices.json', pathlib.Path('/etc/hotel-tv/devices.json'), 0, owner.pw_gid, 0o640)]
for name in ['catalog.json','ads.json','events.sqlite3']:
    pairs.append((seed/name, pathlib.Path('/srv/hotel-tv/data')/name, owner.pw_uid, owner.pw_gid, 0o640))
for src, dst, uid, gid, mode in pairs:
    if src.is_file() and not dst.exists():
        shutil.copy2(src,dst); os.chown(dst,uid,gid); os.chmod(dst,mode)
media = seed/'media'
for src in media.rglob('*'):
    if not src.is_file() or src.is_symlink(): continue
    dst=pathlib.Path('/srv/hotel-tv/media')/src.relative_to(media)
    if dst.exists(): continue
    dst.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(src,dst); os.chown(dst,owner.pw_uid,owner.pw_gid); os.chmod(dst,0o644)
PY
if [ -n "$operator" ] && [ "$operator" != root ]; then
    id -u "$operator" >/dev/null
    usermod -aG hotel-tv "$operator"
fi
media_owner=hotel-tv
if [ -n "$operator" ] && [ "$operator" != root ]; then media_owner=$operator; fi
install -d -m 2775 -o "$media_owner" -g hotel-tv /srv/hotel-tv/media/电影 /srv/hotel-tv/media/电视剧
install -d -m 2775 -o hotel-tv -g hotel-tv /srv/hotel-tv/media/广告 /srv/hotel-tv/media/_posters
install -m 0644 "$release/units/hotel-tv.service" /etc/systemd/system/hotel-tv.service
ln -s "$release" /opt/hotel-tv/current.next
mv -Tf /opt/hotel-tv/current.next /opt/hotel-tv/current
systemctl daemon-reload
if systemctl cat hotel-tv-admin-preview.service >/dev/null 2>&1; then systemctl disable --now hotel-tv-admin-preview.service; fi
systemctl enable hotel-tv.service
systemctl restart hotel-tv.service
systemctl is-active hotel-tv.service
printf '%s\n' 'Hotel TV installed: TV 18790; authenticated administration 18791.' 'Initial admin credentials: /srv/hotel-tv/data/admin-initial-password.txt (private; do not publish).'
