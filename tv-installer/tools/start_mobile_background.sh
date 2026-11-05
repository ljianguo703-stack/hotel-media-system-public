#!/bin/sh
set -eu
installer_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
# In a source checkout this file is under tools; packaged copies live beside the executable.
if [ ! -f "$installer_dir/hotel-tv-installer" ] && [ -f "$installer_dir/../hotel-tv-installer" ]; then
    installer_dir="$(CDPATH= cd -- "$installer_dir/.." && pwd)"
fi
cd -- "$installer_dir"
umask 077
installer_state="${HOTEL_INSTALLER_STATE:-$installer_dir/data}"
mkdir -p "$installer_state"
[ -x ./hotel-tv-installer ] || chmod +x ./hotel-tv-installer
[ -x ./platform-tools/adb ] || chmod +x ./platform-tools/adb
launch_log="$(mktemp "$installer_state/mobile-entry.XXXXXX.log")"
nohup ./hotel-tv-installer --lan --no-browser --state "$installer_state" "$@" >"$launch_log" 2>&1 </dev/null &
launch_pid=$!
printf '%s\n' "$launch_pid" >"$launch_log.pid"
attempt=0
while [ "$attempt" -lt 100 ]; do
    if grep -Fq '手机连接码：' "$launch_log"; then
        sed -n '1,3p' "$launch_log"
        printf '\n后台进程：%s\n日志：%s\n可关闭SSH窗口或电脑，Ubuntu会继续执行。结束安装任务后再停止该进程。\n' "$launch_pid" "$launch_log"
        exit 0
    fi
    if ! kill -0 "$launch_pid" 2>/dev/null; then
        cat "$launch_log"
        exit 1
    fi
    attempt=$((attempt + 1))
    sleep 0.1
done
printf '启动尚未确认，进程：%s；请查看日志：%s\n' "$launch_pid" "$launch_log" >&2
exit 1
