#!/bin/bash
# Consistent backup of NAS application state. Movie files and ordinary shared files are separate.
set -euo pipefail
[ "$(id -u)" = 0 ] || exit 2
backup_dir=/var/backups/hotel-nas/$(date +%Y%m%d-%H%M%S)
install -d -m 0700 "$backup_dir"
trap 'docker start nas-jellyfin nas-filebrowser nas-dockge >/dev/null' EXIT
docker stop nas-jellyfin nas-filebrowser nas-dockge >/dev/null
tar -czf "$backup_dir/nas-applications.tar.gz" -C / opt/stacks srv/nas/appdata srv/nas/images.lock.json etc/samba/smb.conf etc/samba/hotel-nas.conf srv/hotel-tv/data/nas-integration.json var/lib/samba/private/passdb.tdb
chmod 0600 "$backup_dir/nas-applications.tar.gz"
python3 - "$backup_dir" <<'PY'
import pathlib,sqlite3,sys
root=pathlib.Path(sys.argv[1])
with sqlite3.connect('/srv/hotel-tv/data/events.sqlite3') as source,sqlite3.connect(str(root/'hotel-events.sqlite3')) as target:source.backup(target)
(root/'hotel-events.sqlite3').chmod(0o600)
PY
sha256sum "$backup_dir/nas-applications.tar.gz" > "$backup_dir/SHA256SUMS"
docker start nas-jellyfin nas-filebrowser nas-dockge >/dev/null
trap - EXIT
printf 'NAS application backup: %s\n' "$backup_dir"
