#!/bin/sh
set -eu

read_secret() {
    variable_name="$1"
    secret_path="$2"
    if [ -r "$secret_path" ]; then
        secret_value="$(tr -d '\r\n' < "$secret_path")"
        if [ -z "$secret_value" ]; then
            echo "The secret file for ${variable_name} is empty." >&2
            exit 1
        fi
        export "${variable_name}=${secret_value}"
    fi
}

read_secret POSTGRES_PASSWORD /run/secrets/postgres_admin_password
read_secret STARTUNNEL_DB_RUNTIME_PASSWORD /run/secrets/postgres_runtime_password

if [ -z "${POSTGRES_PASSWORD:-}" ]; then
    echo "POSTGRES_PASSWORD is required." >&2
    exit 1
fi

if [ -z "${STARTUNNEL_DB_RUNTIME_PASSWORD:-}" ]; then
    echo "STARTUNNEL_DB_RUNTIME_PASSWORD is required." >&2
    exit 1
fi

if [ "$POSTGRES_PASSWORD" = "$STARTUNNEL_DB_RUNTIME_PASSWORD" ]; then
    echo "The PostgreSQL admin and runtime passwords must be different." >&2
    exit 1
fi

exec docker-entrypoint.sh "$@"
