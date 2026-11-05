#!/bin/sh
set -eu
umask 077
[ "$(id -u)" = 0 ] || { echo '请使用 sudo 执行。' >&2; exit 1; }
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
target=/usr/local/lib/hotel-npc
stack=/opt/stacks/npc
image=ghcr.io/djylb/npc:v0.34.10@sha256:64b05938c189edbdcb90865427f6e28e6aeb3e8e38f4a01495b69a0760944812
[ "$(docker inspect --format '{{.Config.Image}}' nas-npc)" = "$image" ] || { echo 'NPC 镜像不符，停止操作。' >&2; exit 1; }
[ -d "$stack" ] && [ ! -L "$stack" ] || exit 1
# Backup only this stack and its private configuration, never other containers.
install -d -m 0700 /var/backups/hotel-npc
backup_dir=$(mktemp -d /var/backups/hotel-npc/np02-XXXXXXXX)
cp -a "$stack" "$backup_dir/stack"
cp -a /srv/nas/appdata/npc "$backup_dir/appdata"
if [ -d "$target" ]; then cp -a "$target" "$backup_dir/control"; fi
if [ -f /etc/systemd/system/hotel-npc-control.service ]; then cp -a /etc/systemd/system/hotel-npc-control.service "$backup_dir/unit"; fi
was_running=$(docker inspect --format '{{.State.Running}}' nas-npc)
was_active=$(systemctl is-active hotel-npc-control.service 2>/dev/null || true)
rollback() {
    code=$?
    [ "$code" = 0 ] && return
    trap - EXIT
    systemctl stop hotel-npc-control.service 2>/dev/null || true
    for name in compose.yaml entrypoint.sh README.txt .env connection.env; do
        if [ -f "$backup_dir/stack/$name" ]; then cp -a "$backup_dir/stack/$name" "$stack/$name"; else rm -f "$stack/$name"; fi
    done
    if [ -f "$backup_dir/appdata/control.json" ]; then cp -a "$backup_dir/appdata/control.json" /srv/nas/appdata/npc/control.json; else rm -f /srv/nas/appdata/npc/control.json; fi
    for name in control.py npc_protocol.py; do
        if [ -f "$backup_dir/control/$name" ]; then cp -a "$backup_dir/control/$name" "$target/$name"; else rm -f "$target/$name"; fi
    done
    if [ -f "$backup_dir/unit" ]; then cp -a "$backup_dir/unit" /etc/systemd/system/hotel-npc-control.service; else systemctl disable hotel-npc-control.service 2>/dev/null || true; rm -f /etc/systemd/system/hotel-npc-control.service; fi
    systemctl daemon-reload
    [ "$was_active" != active ] || systemctl start hotel-npc-control.service
    if [ "$was_running" = true ]; then docker compose -f "$stack/compose.yaml" up -d; else docker compose -f "$stack/compose.yaml" stop; fi
    echo "控制服务安装失败，已恢复原配置。备份：$backup_dir" >&2
    exit "$code"
}
trap rollback EXIT
systemctl stop hotel-npc-control.service 2>/dev/null || true
install -d -m 0755 "$target"
install -m 0644 "$source_dir/control.py" "$target/control.py"
install -m 0644 "$source_dir/npc_protocol.py" "$target/npc_protocol.py"
install -m 0600 "$source_dir/compose.yaml" "$stack/compose.yaml"
install -m 0644 "$source_dir/entrypoint.sh" "$stack/entrypoint.sh"
install -m 0600 "$source_dir/README.txt" "$stack/README.txt"
python3 "$target/control.py" --prepare
install -m 0644 "$source_dir/hotel-npc-control.service" /etc/systemd/system/hotel-npc-control.service
systemctl daemon-reload
systemctl enable --now hotel-npc-control.service
echo "NPC 后台控制服务已安装；备份：$backup_dir"
