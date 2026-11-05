#!/bin/sh
# Add the Cockpit page and root-owned local action; no password changes during installation.
set -eu
[ "$(id -u)" = 0 ] || { echo '请在 Ubuntu 使用 sudo 执行'; exit 1; }
source_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
command -v cockpit-bridge >/dev/null || { echo '请先安装 cockpit'; exit 1; }
install -d -o root -g root -m 0755 /usr/local/lib/hotel-passwords /usr/share/cockpit/hotel-passwords
install -o root -g root -m 0644 "$source_root/deploy/passwords/manage.py" /usr/local/lib/hotel-passwords/manage.py
for file in manifest.json index.html passwords.js passwords.css; do
    install -o root -g root -m 0644 "$source_root/admin/passwords/$file" "/usr/share/cockpit/hotel-passwords/$file"
done
systemctl enable --now cockpit.socket
echo '密码管理已安装；在后台或 Cockpit 中打开，使用 Ubuntu 账号验证'
