#!/bin/sh
set -eu
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
INSTALLED="$HOME/.gemini/config/plugins/ant-extend/scripts/run.sh"
if [ -x "$INSTALLED" ]; then exec "$INSTALLED" --open; fi
exec "$DIR/scripts/run.sh" --open
