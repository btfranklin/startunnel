#!/bin/sh
set -eu

: "${STARTUNNEL_DB_RUNTIME_PASSWORD:?STARTUNNEL_DB_RUNTIME_PASSWORD is required}"

psql \
    --set=ON_ERROR_STOP=1 \
    --set=database_name="$POSTGRES_DB" \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" <<'SQL'
\getenv runtime_password STARTUNNEL_DB_RUNTIME_PASSWORD
SELECT format(
    'CREATE ROLE startunnel LOGIN PASSWORD %L',
    :'runtime_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'startunnel')
\gexec

\ir /usr/local/share/startunnel/runtime-grants.sql
SQL
