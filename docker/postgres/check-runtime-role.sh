#!/bin/sh
set -eu

database="${1:-startunnel}"
if [ -r /run/secrets/postgres_runtime_password ]; then
    STARTUNNEL_DB_RUNTIME_PASSWORD="$(tr -d '\r\n' < /run/secrets/postgres_runtime_password)"
fi
: "${STARTUNNEL_DB_RUNTIME_PASSWORD:?STARTUNNEL_DB_RUNTIME_PASSWORD is required}"
export PGPASSWORD="$STARTUNNEL_DB_RUNTIME_PASSWORD"

if ! psql -X --quiet --set=ON_ERROR_STOP=on --username startunnel --dbname "$database" \
    >/dev/null 2>&1 <<'SQL'
SELECT count(*) FROM django_migrations;
BEGIN;
INSERT INTO django_migrations (app, name, applied)
VALUES ('runtime_role_check', 'runtime_dml', now());
DELETE FROM django_migrations WHERE app = 'runtime_role_check' AND name = 'runtime_dml';
INSERT INTO django_session (session_key, session_data, expire_date)
VALUES ('runtime-role-check', 'runtime-check-data', now() + interval '5 minutes');
UPDATE django_session SET session_data = 'runtime-check-updated'
WHERE session_key = 'runtime-role-check';
DELETE FROM django_session WHERE session_key = 'runtime-role-check';
ROLLBACK;
SQL
then
    echo "The runtime role cannot perform normal database writes." >&2
    exit 1
fi

require_denied() {
    label="$1"
    query="$2"
    if ! state="$(psql -X --quiet --tuples-only --no-align --set=ON_ERROR_STOP=off \
        --username startunnel --dbname "$database" 2>/dev/null <<SQL
$query
\echo :SQLSTATE
SQL
    )" || [ "$state" != 42501 ]; then
        echo "The runtime role did not prove permission denial for ${label}." >&2
        exit 1
    fi
}

require_denied "schema changes" "CREATE TABLE startunnel_forbidden_runtime (id integer);"
require_denied "temporary tables" "CREATE TEMP TABLE startunnel_forbidden_runtime_temp (id integer);"
require_denied "database creation" "CREATE DATABASE startunnel_forbidden_runtime_database;"
require_denied "message deletion" "DELETE FROM tunnels_message WHERE FALSE;"
require_denied "audit deletion" "DELETE FROM tunnels_auditevent WHERE FALSE;"
