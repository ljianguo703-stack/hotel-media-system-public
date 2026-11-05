#!/bin/sh
set -eu

# Missing connection details are an intentional preparation state, not a failed login.
# Never fall back to the sample /conf/npc.conf supplied by an upstream image.
if [ -z "${NPC_SERVER_ADDR:-}" ] || [ -z "${NPC_SERVER_VKEY:-}" ]; then
    echo 'NPC 0.34.10 已准备，等待在影院后台填写服务端地址和验证密钥。'
    exec sleep infinity
fi

# Docker owns updates. Keep the pinned executable even if the server requests an update.
# Upstream 0.34.10 logs the vkey at info level. Retain warnings/errors only.
echo 'NPC 0.34.10 正在连接；请在 NPS 服务端查看在线状态。'
set -f
# The controller validates and canonicalizes these flags; no eval or shell commands.
set -- ${NPC_EXTRA_ARGS:-}
exec /npc -disable_remote_update \
    -server="$NPC_SERVER_ADDR" \
    -vkey="$NPC_SERVER_VKEY" \
    -type="${NPC_CONN_TYPE:-tcp}" \
    -dns_server="${NPC_DNS_SERVER:-}" \
    -debug=false -log=stdout -log_level=warn -log_color=false "$@"
