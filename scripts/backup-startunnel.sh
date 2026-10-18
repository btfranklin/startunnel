#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "Usage: scripts/backup-startunnel.sh /absolute/path/startunnel.backup" >&2
    exit 2
fi

destination="$1"
case "$destination" in
    /*.backup) ;;
    *)
        echo "The backup path must be absolute and end in .backup." >&2
        exit 2
        ;;
esac

destination_parent="$(dirname -- "$destination")"
if [ ! -d "$destination_parent" ]; then
    echo "The backup parent directory does not exist." >&2
    exit 2
fi
if [ -e "$destination" ]; then
    echo "The backup destination already exists. Choose a new path." >&2
    exit 2
fi

project_root="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
temporary="${destination}.partial.$$"
umask 077
: "${COMPOSE_FILE:=compose.yaml:compose.dev.yaml}"
export COMPOSE_FILE

cleanup() {
    rm -rf -- "$temporary"
}
trap cleanup EXIT HUP INT TERM

cd "$project_root"
mkdir -m 700 -- "$temporary"
docker compose exec -T postgres \
    pg_dump \
    --username startunnel_admin \
    --dbname startunnel \
    --format custom \
    --compress 9 \
    --no-owner \
    --no-acl > "$temporary/postgres.dump"

(
    cd "$temporary"
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum postgres.dump > manifest.sha256
    else
        shasum -a 256 postgres.dump > manifest.sha256
    fi
)
mv -- "$temporary" "$destination"
trap - EXIT HUP INT TERM
echo "Created the StarTunnel PostgreSQL backup: $destination"
