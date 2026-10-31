#!/usr/bin/env bash
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
py="/usr/bin/python3"
if ! "$py" -c 'import sys' >/dev/null 2>&1; then
  py="python3"
fi
exec "$py" "$here/release.py" "$@"
