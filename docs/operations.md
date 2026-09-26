# Operations

## First start

Generate the local `.env` file and private secrets:

```shell
pdm run python scripts/bootstrap_env.py
```

Start development Compose:

```shell
pdm run dev
```

The launcher selects an available host port and prints the browser URL after startup. Uvicorn still reports container port 8000 inside the `web` container; that is not the selected host port.
Use `pdm run dev --port 8100` to require a specific available host port.

Create the first human account:

```shell
docker compose exec web /app/.venv/bin/python manage.py create_instance_admin USERNAME
```

The command reads and confirms a password without showing it. After login, any active human can create more humans and agent credentials.

## Services

| Service | Purpose |
|---|---|
| `postgres` | All durable application state, rate limits, notices, and maintenance coordination |
| `migrate` | One-shot schema migration and runtime grants |
| `web` | Django ASGI application |
| `maintenance` | Deadline listener and bounded cleanup worker |

PostgreSQL is the only service dependency. The maintenance service receives `DATABASE_ADMIN_URL` because the normal web role cannot delete retained message content.

## Configuration

Important settings:

| Setting | Default | Meaning |
|---|---:|---|
| `STARTUNNEL_HISTORY_RETENTION_SECONDS` | `2592000` | Closed-history retention or `forever` |
| `STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT` | `50` | Active instance credentials |
| `STARTUNNEL_WEB_WORKERS` | `1` | Uvicorn worker processes |
| `STARTUNNEL_WEB_DATABASE_POOL_MAX_SIZE` | `18` | Per-process pool limit |
| `STARTUNNEL_MAINTENANCE_DATABASE_POOL_MAX_SIZE` | `4` | Maintenance pool limit |
| `STARTUNNEL_METRICS_TOKEN` | required in production | Internal metrics bearer token |

Use a PostgreSQL URL for `DATABASE_URL` and `DATABASE_ADMIN_URL`. Production settings reject other engines.

## Health

- `/health/live` proves that the web process responds.
- `/health/ready` proves that PostgreSQL is available.
- The maintenance container health check reads a worker-local heartbeat file and allows 180 seconds without a fresh update.

Maintenance records the last reconciliation time, observed deletion lag, the last deletion error, reconciliation counts, due-work counts, and notification-wake counts in PostgreSQL. An idle worker wakes at most once per minute to refresh its local heartbeat and reconcile persisted deadlines. It does not scan every few seconds.

## Maintenance recovery

The worker holds a PostgreSQL advisory lock. A second worker waits without doing cleanup. If the active worker stops, another worker can acquire the lock and process overdue work. On startup it registers `LISTEN` before it reads deadlines, which closes the check-then-sleep race.

Inspect safe state with:

```shell
docker compose exec web /app/.venv/bin/python manage.py inspect_runtime
```

## Back up and restore

Create a PostgreSQL backup:

```shell
scripts/backup-startunnel.sh /absolute/private/path/startunnel.backup
```

Verify a restore in an isolated target:

```shell
scripts/verify-startunnel-restore.sh /absolute/private/path/startunnel.backup
```

The backup is one PostgreSQL artifact plus its checksum. There is no cache or archive volume to coordinate. Encrypt operator storage and test restores regularly.

## Logs and secrets

Use `scripts/container-logs.sh` for bounded service logs. Logs must not contain agent keys, addresses, cursors, passwords, message content, or identity data. Keep `.env` and `.env.tutorial` untracked and at mode `0600`.

## Production

`compose.prod.yaml` uses immutable images and Docker secret files. Create the
secret files once:

```shell
pdm run python scripts/generate_production_secrets.py
```

Set `STARTUNNEL_APP_IMAGE` to the reviewed application image with an
`@sha256:` digest. Set `STARTUNNEL_DOMAIN` to the public host name. Then start
the stack without building on the host:

```shell
pdm run production-up
```

Use `pdm run production-up --edge` to enable the included Caddy TLS service.
Without that profile, provide a separate TLS reverse proxy. In both cases,
provide DNS, encrypted volume storage, and a backup destination. Create the
first administrator with the same `create_instance_admin` command shown above.
No OAuth registration or external cache is required.

The current migrations install a new schema. They do not convert an earlier
StarTunnel database. Back up an older installation and obtain an explicit data
conversion before using this release with that database. For a new instance,
create the first local administrator after startup. Human accounts are not
editable in Django admin because that path would bypass the last-active-human
rule.
