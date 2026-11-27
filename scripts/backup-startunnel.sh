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
if [ -e "$destination" ] || [ -L "$destination" ]; then
    echo "The backup destination already exists. Choose a new path." >&2
    exit 2
fi

project_root="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
umask 077
: "${COMPOSE_FILE:=compose.yaml:compose.dev.yaml}"
export COMPOSE_FILE

temporary="$(mktemp -d "${destination}.partial.XXXXXXXX")"
published_directory=false
cleanup() {
    status="$?"
    trap - EXIT
    if ! rm -rf -- "$temporary"; then
        echo "Could not remove partial backup $temporary." >&2
        status=1
    fi
    if [ "$published_directory" = true ] && [ ! -f "$destination/manifest.sha256" ]; then
        if ! rm -rf -- "$destination"; then
            echo "Could not remove incomplete backup $destination." >&2
            status=1
        fi
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

cd "$project_root"
set -- docker compose
if [ -n "${STARTUNNEL_BACKUP_ENV_FILE:-}" ]; then
    set -- "$@" --env-file "$STARTUNNEL_BACKUP_ENV_FILE"
fi
"$@" exec -T postgres \
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
# Claim the destination without replacing any existing path.
mkdir -m 700 -- "$destination"
published_directory=true
mv -- "$temporary/postgres.dump" "$destination/postgres.dump"
# The manifest marks a complete backup. Publish it last.
mv -- "$temporary/manifest.sha256" "$destination/manifest.sha256"
echo "Created the StarTunnel PostgreSQL backup: $destination"
