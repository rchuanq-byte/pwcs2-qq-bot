#!/usr/bin/env bash
set -eu
# Avoid large native crash dumps on the small production disk.
ulimit -c 0
exec bash /app/entrypoint.sh "$@"
