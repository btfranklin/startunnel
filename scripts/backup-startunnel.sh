#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "Usage: scripts/backup-startunnel.sh /absolute/private/path/startunnel.backup" >&2
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
parent_mode="$(stat -c '%a' "$destination_parent" 2>/dev/null || stat -f '%Lp' "$destination_parent")"
if [ "$parent_mode" != "700" ]; then
    echo "The backup parent directory must have mode 0700." >&2
    exit 2
fi
if [ -e "$destination" ]; then
    echo "The backup destination already exists. Choose a new path." >&2
    exit 2
fi

project_root="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
temporary="${destination}.partial.$$"
running_services=""
umask 077
: "${COMPOSE_FILE:=compose.yaml:compose.dev.yaml}"
export COMPOSE_FILE

restart_services() {
    if [ -n "$running_services" ]; then
        # The service names are fixed output from Docker Compose, not user input.
        # shellcheck disable=SC2086
        docker compose up -d $running_services >/dev/null
    fi
}

cleanup() {
    rm -rf -- "$temporary"
    restart_services
}
trap cleanup EXIT HUP INT TERM

cd "$project_root"
running_services="$(
    docker compose ps --status running --services |
        awk '$0 == "web" || $0 == "maintenance" {print}' |
        tr '\n' ' '
)"
if [ -n "$running_services" ]; then
    # The service names are fixed by the allowlist above.
    # shellcheck disable=SC2086
    docker compose stop $running_services >/dev/null
fi

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
restart_services
echo "Created the StarTunnel PostgreSQL backup: $destination"
