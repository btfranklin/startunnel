#!/bin/sh
set -eu

project_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$project_root"

docker compose -f compose.yaml -f compose.dev.yaml ps

if ! docker compose -f compose.yaml -f compose.dev.yaml exec -T web \
    curl --noproxy '*' --fail --silent --max-time 3 http://127.0.0.1:8000/health/ready
then
    echo "StarTunnel is not ready. Run: docker compose -f compose.yaml -f compose.dev.yaml logs web" >&2
    exit 1
fi
printf '\n'
