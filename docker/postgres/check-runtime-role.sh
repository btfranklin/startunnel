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
DO $$
BEGIN
    IF EXISTS (
        WITH classified(table_name, may_update, may_delete) AS (VALUES
            ('accounts_admincredential', true, true),
            ('accounts_adminoperation', true, true),
            ('accounts_user', true, true),
            ('accounts_user_groups', true, true),
            ('accounts_user_user_permissions', true, true),
            ('agents_agentcredential', true, true),
            ('auth_group', true, true),
            ('auth_group_permissions', true, true),
            ('auth_permission', true, true),
            ('core_maintenancestate', true, true),
            ('core_ratelimitbucket', true, true),
            ('django_admin_log', true, true),
            ('django_content_type', true, true),
            ('django_migrations', true, true),
            ('django_session', true, true),
            ('tunnels_activitycheckpoint', true, false),
            ('tunnels_auditevent', false, false),
            ('tunnels_beacon', true, false),
            ('tunnels_boardstate', true, false),
            ('tunnels_cycle', true, false),
            ('tunnels_idempotencyrecord', true, true),
            ('tunnels_message', true, false),
            ('tunnels_messagemention', true, false),
            ('tunnels_tunnel', true, false),
            ('tunnels_tunneladdress', true, false),
            ('tunnels_tunnelevent', true, false)
        ), actual AS (
            SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'
        )
        SELECT 1 FROM classified FULL JOIN actual USING (table_name)
        WHERE classified.table_name IS NULL OR actual.table_name IS NULL
            OR NOT has_table_privilege(current_user, format('public.%I', table_name), 'SELECT')
            OR NOT has_table_privilege(current_user, format('public.%I', table_name), 'INSERT')
            OR has_table_privilege(current_user, format('public.%I', table_name), 'UPDATE') <> may_update
            OR has_table_privilege(current_user, format('public.%I', table_name), 'DELETE') <> may_delete
    ) THEN
        RAISE EXCEPTION 'runtime table permissions do not match the table classification';
    END IF;
    IF (
        SELECT count(*) FROM pg_trigger AS trigger
        JOIN pg_class AS relation ON relation.oid = trigger.tgrelid
        JOIN pg_namespace AS schema ON schema.oid = relation.relnamespace
        WHERE schema.nspname = 'public' AND NOT trigger.tgisinternal
            AND trigger.tgname = 'startunnel_runtime_history_guard' AND trigger.tgenabled = 'O'
            AND relation.relname IN (
                'tunnels_cycle', 'tunnels_beacon',
                'tunnels_idempotencyrecord', 'accounts_adminoperation'
            )
    ) <> 4 THEN
        RAISE EXCEPTION 'runtime history guards are missing or disabled';
    END IF;
END
$$;
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
