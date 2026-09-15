#!/usr/bin/env bash
# Forward all converter options unchanged through the site's ud launcher.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec ud "${TARMAC_PYTHON:-python3}" "${TARMAC_SCRIPT:-$script_dir/tarmac_to_cachegrind.py}" "$@"
