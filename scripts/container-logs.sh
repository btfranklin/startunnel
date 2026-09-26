#!/bin/sh
set -eu

project_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$project_root"

if [ "$#" -eq 0 ]; then
    set -- web maintenance migrate postgres
fi

docker compose -f compose.yaml -f compose.dev.yaml logs --no-log-prefix --tail 200 "$@"
