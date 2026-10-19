#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "Usage: scripts/verify-startunnel-restore.sh /absolute/private/path/startunnel.backup" >&2
    exit 2
fi

backup="$1"
case "$backup" in
    /*.backup) ;;
    *)
        echo "The backup path must be absolute and end in .backup." >&2
        exit 2
        ;;
esac
for required in postgres.dump manifest.sha256; do
    if [ ! -f "$backup/$required" ]; then
        echo "The backup is missing ${required}." >&2
        exit 2
    fi
done

project_root="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
restore_database="startunnel_restore_check_$$"
umask 077
: "${COMPOSE_FILE:=compose.yaml:compose.dev.yaml}"
export COMPOSE_FILE

cleanup() {
    docker compose exec -T postgres \
        dropdb --if-exists --force --username startunnel_admin "$restore_database" \
        >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

cd "$backup"
if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -c manifest.sha256
else
    shasum -a 256 -c manifest.sha256
fi

cd "$project_root"
docker compose exec -T postgres \
    createdb --username startunnel_admin "$restore_database"
docker compose exec -T postgres \
    pg_restore \
    --username startunnel_admin \
    --dbname "$restore_database" \
    --no-owner \
    --no-acl < "$backup/postgres.dump"
docker compose exec -T postgres \
    psql \
    --set=ON_ERROR_STOP=1 \
    --set=database_name="$restore_database" \
    --username startunnel_admin \
    --dbname "$restore_database" \
    --file /usr/local/share/startunnel/runtime-grants.sql

query_restore() {
    query="$1"
    docker compose exec -T postgres \
        psql --tuples-only --no-align --field-separator '|' \
        --username startunnel_admin --dbname "$restore_database" --command "$query"
}

table_count="$(query_restore "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';")"
migration_count="$(query_restore "SELECT count(*) FROM django_migrations;")"
if [ "$table_count" -lt 1 ] || [ "$migration_count" -lt 1 ]; then
    echo "The restored database is missing its schema or migration history." >&2
    exit 1
fi

docker compose exec -T postgres \
    /usr/local/bin/startunnel-check-runtime-role "$restore_database"

echo "The PostgreSQL restore check passed with ${table_count} tables."
