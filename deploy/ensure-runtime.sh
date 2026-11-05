#!/bin/sh
# Shared by the source entry point and embedded single-file installer.
hotel_ensure_python() {
    HOTEL_PYTHON=$(command -v python3 || true)
    if [ -n "$HOTEL_PYTHON" ] && "$HOTEL_PYTHON" -c 'import json, sqlite3, tarfile; from zoneinfo import ZoneInfo; ZoneInfo("Asia/Singapore")' >/dev/null 2>&1; then
        export HOTEL_PYTHON
        return 0
    fi
    for hotel_arg in "$@"; do
        case "$hotel_arg" in
            --plan|--help|-h)
                echo '缺少完整 Python3；预览未修改系统。正式使用 sudo bash 安装文件，将自动刷新APT并补齐运行环境。' >&2
                return 2 ;;
        esac
    done
    if [ "$(id -u)" != 0 ]; then
        echo '首次安装请在 Ubuntu 本机终端使用 sudo bash 安装文件；无需预先开启SSH。' >&2
        return 2
    fi
    if [ ! -r /etc/os-release ] || ! command -v apt-get >/dev/null 2>&1; then
        echo '运行环境自动准备仅支持 Ubuntu 24.04/26.04 LTS。' >&2
        return 2
    fi
    hotel_os_id=$(sed -n 's/^ID=//p' /etc/os-release | tr -d '"')
    hotel_os_version=$(sed -n 's/^VERSION_ID=//p' /etc/os-release | tr -d '"')
    if [ "$hotel_os_id" != ubuntu ]; then
        echo '运行环境自动准备仅支持 Ubuntu。' >&2
        return 2
    fi
    case "$hotel_os_version" in 24.04|26.04) ;; *) echo '请使用 Ubuntu 24.04/26.04 LTS。' >&2; return 2 ;; esac
    echo '首次准备：刷新APT索引并安装Python3、证书和时区数据；不安装或启用SSH。'
    apt-get -o DPkg::Lock::Timeout=180 -o Acquire::Retries=3 update || return $?
    DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=180 -o Acquire::Retries=3 install -y --no-install-recommends python3 ca-certificates tzdata || return $?
    HOTEL_PYTHON=/usr/bin/python3
    "$HOTEL_PYTHON" -c 'import json, sqlite3, tarfile; from zoneinfo import ZoneInfo; ZoneInfo("Asia/Singapore")' || return $?
    export HOTEL_PYTHON
}
