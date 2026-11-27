#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "Usage: scripts/verify-startunnel-restore.sh /absolute/private/path/startunnel.backup" >&2
    exit 2
fi

backup="$1"
project_root="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
. "$project_root/scripts/restore-startunnel-lib.sh"
umask 077
: "${COMPOSE_FILE:=compose.yaml:compose.dev.yaml}"
export COMPOSE_FILE

verify_restore_archive
cd "$project_root"
token="$(restore_token)"
test "${#token}" -eq 24
restore_database="startunnel_restore_check_$token"
create_restore_database "$restore_database"

cleanup() {
    status="$?"
    trap - EXIT
    if ! drop_restore_database "$restore_database" && [ "$status" -eq 0 ]; then
        status=1
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

load_and_verify_restore "$restore_database"
trap - EXIT
drop_restore_database "$restore_database"
echo "The PostgreSQL restore check passed with ${table_count} tables."
