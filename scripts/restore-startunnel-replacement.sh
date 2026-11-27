#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "Usage: scripts/restore-startunnel-replacement.sh /absolute/private/path/startunnel.backup" >&2
    exit 2
fi

backup="$1"
script_directory="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
. "$script_directory/restore-startunnel-lib.sh"
umask 077
: "${COMPOSE_FILE:=compose.yaml:compose.prod.yaml}"
export COMPOSE_FILE

verify_restore_archive

require_stopped_writers() {
    writers="$(restore_compose ps --status running --quiet web maintenance migrate)"
    if [ -n "$writers" ]; then
        restore_error "Web, maintenance, and migration containers must be stopped before recovery."
    fi
}
require_stopped_writers
restore_compose up -d --no-build --wait --wait-timeout 180 postgres
require_stopped_writers

token="$(restore_token)"
test "${#token}" -eq 24
bootstrap_reference="startunnel_bootstrap_check_$token"
restore_database="startunnel_restore_$token"
previous_database="startunnel_bootstrap_$token"
reference_owned=false
restore_owned=false
switch_attempted=false
workspace="$(mktemp -d "${TMPDIR:-/tmp}/startunnel-replacement.XXXXXXXX")"

cleanup() {
    status="$?"
    trap - EXIT
    if [ "$reference_owned" = true ] && ! drop_restore_database "$bootstrap_reference"; then
        status=1
    fi
    if [ "$restore_owned" = true ] && [ "$switch_attempted" = false ]; then
        if ! drop_restore_database "$restore_database"; then
            status=1
        fi
    fi
    if ! rm -rf -- "$workspace"; then
        echo "Could not remove the private recovery workspace $workspace." >&2
        status=1
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

create_restore_database "$bootstrap_reference"
reference_owned=true
apply_restore_grants "$bootstrap_reference" > /dev/null

schema_dump() {
    # PostgreSQL emits a random psql restriction key by default.
    restore_postgres pg_dump --schema-only --restrict-key=StartunnelReference \
        --username startunnel_admin --dbname "$1"
}

verify_bootstrap() {
    schema_dump startunnel > "$workspace/current.sql"
    if ! cmp -s "$workspace/current.sql" "$workspace/expected.sql" ||
        [ "$(restore_sql --dbname startunnel --command 'SELECT count(*) FROM pg_largeobject_metadata;')" != 0 ]; then
        restore_error "The selected startunnel database differs from a fresh bootstrap database."
    fi
}
schema_dump "$bootstrap_reference" > "$workspace/expected.sql"
verify_bootstrap

create_restore_database "$restore_database"
restore_owned=true
load_and_verify_restore "$restore_database"
require_stopped_writers
verify_bootstrap

# Either both names change or neither does. Open connections block the switch.
echo "Switching databases. Recovery names: $restore_database and $previous_database."
switch_attempted=true
if ! restore_sql --dbname postgres --set=previous="$previous_database" \
        --set=recovered="$restore_database" <<'SQL'
BEGIN;
ALTER DATABASE startunnel RENAME TO :"previous";
ALTER DATABASE :"recovered" RENAME TO startunnel;
COMMIT;
SQL
then
    restore_error "The switch did not confirm completion. Inspect both database names before starting any writer."
fi
verify_restored_database startunnel
echo "The recovered database is ready. Retained bootstrap database: $previous_database."
