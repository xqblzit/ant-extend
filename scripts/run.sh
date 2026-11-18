#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -n "${ANT_EXTEND_PYTHON:-}" ]; then
  case "$ANT_EXTEND_PYTHON" in /usr/bin/python*|/Library/Developer/CommandLineTools/*) exit 2 ;; esac
  exec "$ANT_EXTEND_PYTHON" -B "$SCRIPT_DIR/retry.py" "$@"
fi
# Prefer existing Homebrew Python; never invoke Apple's developer-tools shim.
for interpreter in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if [ -x "$interpreter" ]; then
    exec "$interpreter" -B "$SCRIPT_DIR/retry.py" "$@"
  fi
done
if [ "$(uname -s)" != Darwin ] && command -v python3 >/dev/null 2>&1; then
  exec python3 -B "$SCRIPT_DIR/retry.py" "$@"
fi
# A missing runtime must not keep an otherwise finished conversation alive.
case "${1:-}" in
  --open|--install|--status|--panel-url|--check-config|--pause|--resume)
    printf 'ant-extend requires Python 3.10 or later from Homebrew or python.org.\n' >&2
    exit 2 ;;
  *) printf '{}\n' ;;
esac
