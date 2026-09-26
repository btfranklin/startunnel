#!/bin/sh
set -eu

project_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$project_root"

docker compose -f compose.yaml -f compose.dev.yaml ps

if ! docker compose -f compose.yaml -f compose.dev.yaml exec -T web \
    /app/.venv/bin/python -c \
    "import urllib.request; request = urllib.request.Request('http://127.0.0.1:8000/health/ready', headers={'Host':'localhost', 'X-Forwarded-Proto':'https'}); print(urllib.request.urlopen(request, timeout=3).read().decode())"
then
    echo "StarTunnel is not ready. Run: docker compose -f compose.yaml -f compose.dev.yaml logs web" >&2
    exit 1
fi
