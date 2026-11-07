#!/usr/bin/env bash
# Download the pinned public installer, verify it, then reuse its normal entry point.
set -euo pipefail
umask 077

readonly HOTEL_RELEASE='IN20-20261107'
readonly HOTEL_PACKAGE='hotel-media-system-v1-0-0-rc5-in20-IN20.run'
readonly HOTEL_PACKAGE_SHA256='ca0619924133236f7af5efa44a97b8424fc3d4cd7e9f697e4e706183014c9a7e'
readonly HOTEL_PACKAGE_URL="https://github.com/ljianguo703-stack/hotel-media-system-public/releases/download/${HOTEL_RELEASE}/${HOTEL_PACKAGE}"

hotel_download_dir="${HOTEL_DOWNLOAD_DIR:-${HOME:?}/hotel-cinema-installer/${HOTEL_RELEASE}}"
mkdir -p "$hotel_download_dir"
hotel_installer="$hotel_download_dir/$HOTEL_PACKAGE"

hotel_sha256() {
    local digest
    if command -v sha256sum >/dev/null 2>&1; then
        digest=$(sha256sum "$1")
    elif command -v shasum >/dev/null 2>&1; then
        digest=$(shasum -a 256 "$1")
    else
        printf '%s\n' '缺少 SHA256 工具，请先安装 coreutils。' >&2
        return 1
    fi
    printf '%s\n' "${digest%% *}"
}

if [[ ! -f "$hotel_installer" ]] || [[ "$(hotel_sha256 "$hotel_installer")" != "$HOTEL_PACKAGE_SHA256" ]]; then
    hotel_partial=$(mktemp "$hotel_download_dir/.download.XXXXXX")
    trap 'rm -f -- "$hotel_partial"' EXIT
    if command -v wget >/dev/null 2>&1; then
        wget --https-only --timeout=20 --tries=3 \
            --output-document "$hotel_partial" "$HOTEL_PACKAGE_URL"
    elif command -v curl >/dev/null 2>&1; then
        curl --fail --location --retry 3 --connect-timeout 20 \
            --proto '=https' --tlsv1.2 --output "$hotel_partial" "$HOTEL_PACKAGE_URL"
    else
        printf '%s\n' '请先执行 sudo apt install -y wget ca-certificates。' >&2
        exit 1
    fi
    if [[ "$(hotel_sha256 "$hotel_partial")" != "$HOTEL_PACKAGE_SHA256" ]]; then
        printf '%s\n' '安装包 SHA256 校验失败，请检查网络后重试。' >&2
        exit 1
    fi
    mv -- "$hotel_partial" "$hotel_installer"
    trap - EXIT
fi

printf '安装包校验通过 %s\n' "$hotel_installer"
for hotel_argument in "$@"; do
    case "$hotel_argument" in
        --plan|--help)
            exec bash "$hotel_installer" "$@"
            ;;
    esac
done

if [[ $EUID -eq 0 ]]; then
    exec bash "$hotel_installer" "$@"
fi
exec sudo bash "$hotel_installer" "$@"
