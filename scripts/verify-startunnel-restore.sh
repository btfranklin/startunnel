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

runtime_query() {
    query="$1"
    docker compose exec -T postgres \
        sh -c 'PGPASSWORD="$STARTUNNEL_DB_RUNTIME_PASSWORD" exec psql --set=ON_ERROR_STOP=1 --username startunnel --dbname "$1" --command "$2"' \
        sh "$restore_database" "$query"
}
runtime_query "SELECT count(*) FROM django_migrations; BEGIN; INSERT INTO django_session (session_key, session_data, expire_date) VALUES ('restore-check-session', 'restore-check-data', now() + interval '5 minutes'); UPDATE django_session SET session_data = 'restore-check-updated' WHERE session_key = 'restore-check-session'; DELETE FROM django_session WHERE session_key = 'restore-check-session'; ROLLBACK;" >/dev/null
expect_runtime_operation_denied() {
    label="$1"
    query="$2"
    if runtime_query "$query" >/dev/null 2>&1; then
        echo "The restore check found that runtime ${label} is allowed." >&2
        exit 1
    fi
}
expect_runtime_operation_denied "schema changes" \
    "CREATE TABLE startunnel_forbidden_schema (id integer);"
expect_runtime_operation_denied "immutable history deletion" \
    "DELETE FROM tunnels_message;"

echo "The PostgreSQL restore check passed with ${table_count} tables."
