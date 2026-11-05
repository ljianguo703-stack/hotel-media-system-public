#!/bin/sh
# Explicit one-time deployment step. Do not run as part of an ordinary web-server restart.
set -eu
[ "$(id -u)" = 0 ] || { echo '需要 root 在 Ubuntu 本机执行'; exit 1; }
source_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
[ -f /etc/os-release ] && . /etc/os-release
[ "${ID:-}" = ubuntu ] || { echo '执行器安装脚本仅支持 Ubuntu'; exit 1; }
[ -f /srv/hotel-tv/data/events.sqlite3 ] || { echo '请先部署包含软件模块的后台并初始化数据库'; exit 1; }
getent group hotel-tv >/dev/null
# No application, data disk, Docker stack or television is modified by this setup.
command -v aapt >/dev/null && command -v apksigner >/dev/null && command -v adb >/dev/null || {
 echo '先手动安装校验和电视维护依赖：apt-get install aapt apksigner adb default-jre-headless'; exit 1;
}
release_id=$(date -u +%Y%m%d%H%M%S)
target=/opt/hotel-tv-installer/releases/$release_id
install -d -m 0755 "$target"
for file in software_agent.py software_store.py software_packages.py fleet_agent.py fleet_store.py; do install -m 0644 "$source_root/server/$file" "$target/$file"; done
install -d -m 0755 /opt/hotel-tv-installer
ln -s "$target" /opt/hotel-tv-installer/current-next
mv -Tf /opt/hotel-tv-installer/current-next /opt/hotel-tv-installer/current
install -d -o root -g hotel-tv -m 0700 /var/lib/hotel-tv-installer
install -d -o root -g root -m 0700 /var/lib/hotel-tv-installer/android
install -m 0644 "$source_root/deploy/software/hotel-tv-installer.service" /etc/systemd/system/hotel-tv-installer.service
install -d -o hotel-tv -g hotel-tv -m 0750 /srv/hotel-tv/data/software /srv/hotel-tv/data/software/queue
install -m 0644 "$source_root/deploy/software/hotel-tv-installer.path" /etc/systemd/system/hotel-tv-installer.path
systemctl daemon-reload
systemctl enable --now hotel-tv-installer.path
systemctl is-active hotel-tv-installer.path
