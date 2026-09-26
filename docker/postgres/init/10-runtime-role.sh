#!/bin/sh
set -eu

runtime_password="${STARTUNNEL_DB_RUNTIME_PASSWORD:?STARTUNNEL_DB_RUNTIME_PASSWORD is required}"

case "$runtime_password" in
    *[!A-Za-z0-9_-]* | "")
        echo "The runtime database password must use URL-safe characters." >&2
        exit 1
        ;;
esac

PGOPTIONS="-c startunnel.runtime_password=${runtime_password}" \
    psql \
    --set=ON_ERROR_STOP=1 \
    --set=database_name="$POSTGRES_DB" \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" <<'SQL'
SELECT format(
    'CREATE ROLE startunnel LOGIN PASSWORD %L',
    current_setting('startunnel.runtime_password')
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'startunnel')
\gexec

\ir /usr/local/share/startunnel/runtime-grants.sql
SQL
