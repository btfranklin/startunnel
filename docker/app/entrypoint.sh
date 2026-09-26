#!/bin/sh
set -eu

umask 077

if [ -n "${PROMETHEUS_MULTIPROC_DIR:-}" ]; then
    case "$PROMETHEUS_MULTIPROC_DIR" in
        /tmp/*) ;;
        *)
            echo "PROMETHEUS_MULTIPROC_DIR must be below /tmp." >&2
            exit 1
            ;;
    esac
    mkdir -p -- "$PROMETHEUS_MULTIPROC_DIR"
    find "$PROMETHEUS_MULTIPROC_DIR" -type f -name '*.db' -delete
fi

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

make_service_url() {
    scheme="$1"
    username="$2"
    password="$3"
    hostname="$4"
    port="$5"
    path="$6"
    STARTUNNEL_URL_SCHEME="$scheme" \
    STARTUNNEL_URL_USERNAME="$username" \
    STARTUNNEL_URL_PASSWORD="$password" \
    STARTUNNEL_URL_HOSTNAME="$hostname" \
    STARTUNNEL_URL_PORT="$port" \
    STARTUNNEL_URL_PATH="$path" \
        /app/.venv/bin/python -c 'import os; from urllib.parse import quote; e = os.environ; print("{}://{}:{}@{}:{}/{}".format(e["STARTUNNEL_URL_SCHEME"], quote(e["STARTUNNEL_URL_USERNAME"], safe=""), quote(e["STARTUNNEL_URL_PASSWORD"], safe=""), e["STARTUNNEL_URL_HOSTNAME"], e["STARTUNNEL_URL_PORT"], e["STARTUNNEL_URL_PATH"]))'
}

read_secret STARTUNNEL_SECRET_KEY /run/secrets/startunnel_secret_key
read_secret STARTUNNEL_API_KEY_PEPPER /run/secrets/startunnel_api_key_pepper
read_secret STARTUNNEL_ADDRESS_SECRET /run/secrets/startunnel_address_secret
read_secret STARTUNNEL_ADDRESS_DERIVATION_SECRET /run/secrets/startunnel_address_derivation_secret
read_secret STARTUNNEL_IDEMPOTENCY_SECRET /run/secrets/startunnel_idempotency_secret
read_secret STARTUNNEL_METRICS_TOKEN /run/secrets/startunnel_metrics_token

database_role="${STARTUNNEL_DATABASE_ROLE:-runtime}"
if [ "$database_role" = "admin" ]; then
    read_secret STARTUNNEL_DATABASE_PASSWORD /run/secrets/postgres_admin_password
    if [ -n "${STARTUNNEL_DATABASE_PASSWORD:-}" ]; then
        DATABASE_URL="$(make_service_url \
            postgresql \
            "${STARTUNNEL_DATABASE_ADMIN_USER:-startunnel_admin}" \
            "$STARTUNNEL_DATABASE_PASSWORD" \
            "${STARTUNNEL_DATABASE_HOST:-postgres}" \
            "${STARTUNNEL_DATABASE_PORT:-5432}" \
            "${STARTUNNEL_DATABASE_NAME:-startunnel}")"
        export DATABASE_URL DATABASE_ADMIN_URL="$DATABASE_URL"
    elif [ -n "${DATABASE_ADMIN_URL:-}" ]; then
        export DATABASE_URL="$DATABASE_ADMIN_URL"
    fi
else
    read_secret STARTUNNEL_DATABASE_PASSWORD /run/secrets/postgres_runtime_password
    if [ -n "${STARTUNNEL_DATABASE_PASSWORD:-}" ]; then
        DATABASE_URL="$(make_service_url \
            postgresql \
            "${STARTUNNEL_DATABASE_RUNTIME_USER:-startunnel}" \
            "$STARTUNNEL_DATABASE_PASSWORD" \
            "${STARTUNNEL_DATABASE_HOST:-postgres}" \
            "${STARTUNNEL_DATABASE_PORT:-5432}" \
            "${STARTUNNEL_DATABASE_NAME:-startunnel}")"
        export DATABASE_URL
    fi
fi

# A restore target must also replace the database in an existing URL.
if [ -n "${STARTUNNEL_DATABASE_NAME:-}" ] && [ -n "${DATABASE_URL:-}" ]; then
    DATABASE_URL="$(/app/.venv/bin/python -c 'import os; from urllib.parse import quote, urlsplit; url = urlsplit(os.environ["DATABASE_URL"]); print(url._replace(path="/" + quote(os.environ["STARTUNNEL_DATABASE_NAME"], safe="")).geturl())')"
    export DATABASE_URL
fi

if [ "$#" -eq 0 ]; then
    echo "No container command was supplied." >&2
    exit 1
fi

exec "$@"
