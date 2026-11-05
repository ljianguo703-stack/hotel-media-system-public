#!/bin/sh
set -eu
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$source_dir/deploy/ensure-runtime.sh"
hotel_ensure_python "$@" || exit "$?"
exec "$HOTEL_PYTHON" "$source_dir/deploy/bootstrap.py" "$@"
