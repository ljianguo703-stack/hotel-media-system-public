#!/bin/sh
# Add only the NPC stack. Existing stacks, hotel services and media are untouched.
set -eu
umask 077

[ "$(id -u)" = 0 ] || { echo '请使用 sudo sh install.sh' >&2; exit 1; }
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
stack_dir=/opt/stacks/npc
app_dir=/srv/nas/appdata/npc
image=ghcr.io/djylb/npc:v0.34.10@sha256:64b05938c189edbdcb90865427f6e28e6aeb3e8e38f4a01495b69a0760944812

# Fail before changing an unknown installation. For retries use compose in the existing stack.
[ ! -e "$stack_dir" ] && [ ! -L "$stack_dir" ] || { echo 'npc 栈已存在，停止覆盖。' >&2; exit 1; }
[ ! -e "$app_dir" ] && [ ! -L "$app_dir" ] || { echo 'npc 数据目录已存在，停止覆盖。' >&2; exit 1; }
if docker container inspect nas-npc >/dev/null 2>&1; then
    echo 'nas-npc 容器已存在，停止覆盖。' >&2
    exit 1
fi
docker compose version
python3 "$source_dir/../../docker_images.py" "$image"
docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges:true \
    --user 65534:65534 "$image" -version

install -d -m 0700 "$stack_dir"
install -d -m 0755 "$app_dir"
install -m 0600 "$source_dir/compose.yaml" "$stack_dir/compose.yaml"
install -m 0600 "$source_dir/.env.example" "$stack_dir/.env"
printf 'NPC_SERVER_ADDR=\nNPC_SERVER_VKEY=\nNPC_CONN_TYPE=tcp\n' > "$stack_dir/connection.env"
install -m 0644 "$source_dir/entrypoint.sh" "$stack_dir/entrypoint.sh"
install -m 0600 "$source_dir/README.txt" "$stack_dir/README.txt"

cd "$stack_dir"
docker compose config --quiet
docker compose up -d
docker inspect --format '{{.Name}} {{.State.Status}} restart={{.HostConfig.RestartPolicy.Name}}' nas-npc
echo 'NPC 已安装，当前等待连接信息。可到 Dockge 的 npc 栈填写 .env 后重新部署。'
