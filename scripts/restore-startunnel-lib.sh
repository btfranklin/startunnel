#!/bin/sh
# Shared PostgreSQL restore operations for the two host commands.

restore_error() {
    echo "$1" >&2
    return 1
}

restore_compose() {
    docker compose "$@"
}

restore_postgres() {
    restore_compose exec -T postgres "$@"
}

restore_sql() {
    restore_postgres psql -X --quiet --tuples-only --no-align \
        --set=ON_ERROR_STOP=1 --username startunnel_admin "$@"
}

restore_token() {
    od -An -N12 -tx1 /dev/urandom | tr -d ' \n'
}

verify_restore_archive() {
    case "$backup" in
        /*.backup) ;;
        *) restore_error "The backup path must be absolute and end in .backup."; return 1 ;;
    esac
    for required in postgres.dump manifest.sha256; do
        if [ ! -f "$backup/$required" ]; then
            restore_error "The backup is missing ${required}."
            return 1
        fi
    done
    (
        cd "$backup"
        if command -v sha256sum >/dev/null 2>&1; then
            actual="$(sha256sum postgres.dump)"
        else
            actual="$(shasum -a 256 postgres.dump)"
        fi
        if [ "$actual" != "$(cat manifest.sha256)" ]; then
            restore_error "The PostgreSQL dump checksum does not match."
        fi
    )
}

create_restore_database() {
    name="$1"
    if ! restore_postgres createdb --username startunnel_admin --template=template0 \
        "$name"; then
        restore_error "Database creation did not confirm ownership. Inspect $name before removal."
        return 1
    fi
}

drop_restore_database() {
    if ! restore_postgres dropdb --if-exists --force --username startunnel_admin "$1"; then
        restore_error "Could not remove the restore database $1."
    fi
}

apply_restore_grants() {
    restore_sql --set=database_name="$1" --dbname "$1" \
        --file /usr/local/share/startunnel/runtime-grants.sql
}

verify_restored_database() {
    verified_database="$1"
    table_count="$(restore_sql --dbname "$verified_database" \
        --command "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';")"
    migration_count="$(restore_sql --dbname "$verified_database" \
        --command "SELECT count(*) FROM django_migrations;")"
    if [ "$table_count" -lt 1 ] || [ "$migration_count" -lt 1 ]; then
        restore_error "The restored database is missing its schema or migration history."
        return 1
    fi
    # PGHOST also selects TCP when verifying an older, socket-only probe script.
    restore_compose exec -T -e PGHOST=127.0.0.1 postgres \
        /usr/local/bin/startunnel-check-runtime-role "$verified_database"
}

load_and_verify_restore() {
    restore_target="$1"
    # A restore error can include row contents. Report a safe error here.
    if ! restore_postgres pg_restore --exit-on-error --username startunnel_admin \
        --dbname "$restore_target" --no-owner --no-acl \
        < "$backup/postgres.dump" 2>/dev/null; then
        restore_error "PostgreSQL could not restore the dump into $restore_target."
        return 1
    fi
    apply_restore_grants "$restore_target"
    verify_restored_database "$restore_target"
}
