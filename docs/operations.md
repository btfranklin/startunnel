# Operations

For a new shared instance, start with the [team setup index](setup/README.md).
It has complete guides for AWS EC2, AWS Lightsail, DigitalOcean, and an existing
Linux server. This page is the service and configuration reference.

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

Create the first admin account and key without a browser:

```shell
docker compose exec -T web /app/.venv/bin/python manage.py create_instance_admin USERNAME --key-file /tmp/startunnel-admin.key
```

The new private file is inside the `web` container. Transfer it through the
operator's approved private secret channel to the CLI machine. Preserve mode
`0600`, then remove the temporary container copy. Do not print the key or put it
in chat. The command refuses an existing account or file. Use the
[admin quickstart](user/admin-quickstart.md) for CLI access and verification.

For browser-password setup, use this interactive alternative without `-T`:

```shell
docker compose exec web /app/.venv/bin/python manage.py create_instance_admin USERNAME
```

The command reads and confirms the password without showing it.
Choose one bootstrap mode for a new account. Existing accounts can add passwords
or keys through the admin API, CLI, or browser.

For lost remote access, run `recover_instance_admin USERNAME --key-file FILE`
on the server. Recovery activates the named existing account, issues a new
non-expiring key, and records the action. It does not restore old revoked keys
and has no unauthenticated remote endpoint. Keep the new file private and verify
its identity, health, and audit event. See
[access recovery](setup/maintain.md#recover-administrator-access).

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

For authenticated product inspection, use `admin status` and `admin doctor`
through the existing CLI. These commands report safe database and maintenance
status, capacity, limits, and runtime information. `admin doctor` exits with
failure when a required check fails. A healthy web process alone does not prove
that maintenance is healthy. See the [admin quickstart](user/admin-quickstart.md).
Settings are readable diagnostics; changes still use deployment configuration.

- `/health/live` proves that the web process responds.
- `/health/ready` proves that PostgreSQL is available.
- The maintenance container health check reads a worker-local heartbeat file and allows 180 seconds without a fresh update.

Maintenance records the last reconciliation time, observed deletion lag, the last deletion error, reconciliation counts, due-work counts, and notification-wake counts in PostgreSQL. An idle worker wakes at most once per minute to refresh its local heartbeat and reconcile persisted deadlines. It does not scan every few seconds.

Database query timing applies to each Django connection in its process. It
includes queries from synchronous views, asynchronous adapters, commands, and
maintenance work. The `/metrics` endpoint publishes web-process query metrics;
it does not combine query metrics from other containers. Query labels contain
only the SQL operation type.

## Maintenance recovery

The worker holds a PostgreSQL advisory lock. A second worker waits without doing cleanup. If the active worker stops, another worker can acquire the lock and process overdue work. On startup it registers `LISTEN` before it reads deadlines, which closes the check-then-sleep race.

Inspect safe state with:

```shell
docker compose exec web /app/.venv/bin/python manage.py inspect_runtime
```

## Back up and restore

For production, use [the backup and recovery guide](setup/backups.md). It loads
the production Compose configuration, preserves the application secrets,
encrypts the backup, and checks a copy outside the server.

Create a PostgreSQL backup:

```shell
scripts/backup-startunnel.sh /absolute/private/path/startunnel.backup
```

Verify a restore in a temporary database on the selected PostgreSQL server:

```shell
scripts/verify-startunnel-restore.sh /absolute/private/path/startunnel.backup
```

The backup path is a directory with `postgres.dump` and `manifest.sha256`.
The parent directory must have mode `0700`. The helper stops application
writers during the dump and restarts them afterward. It defaults to development
Compose files unless `COMPOSE_FILE` is set. The database backup does not include
the secret files required for recovery; preserve them as described in the
production guide. There is no cache or archive volume to coordinate.

## Logs and secrets

Use `scripts/container-logs.sh` for bounded development logs. For production,
load the production configuration and use the [production log command](setup/maintain.md#fault-checks).
Logs must not contain admin keys, agent keys, addresses, cursors, passwords, message content,
or identity data. Keep `.env` and `.env.tutorial` untracked and at mode `0600`.

## Production

Use [Install StarTunnel](setup/install.md) for the complete production procedure,
including the image, Linux secret-file ownership, DNS, HTTPS, and initial administrator access.
Use [Get the application image](setup/image.md) to select an official release
or build a custom image. See [Release policy](releases.md) for publication and compatibility.

`compose.prod.yaml` uses immutable images and Docker secret files.
`STARTUNNEL_APP_IMAGE` selects the reviewed image with an `@sha256:` digest;
`STARTUNNEL_DOMAIN` selects the public hostname. The production launcher checks
the secret files and every enabled image, then starts without a host build.

The `--edge` option enables the included Caddy TLS service. The four setup
guides use it. A deployment without that profile needs its own TLS reverse
proxy and private route to the web container; that is a custom integration.
Both forms need DNS, encrypted storage, and a backup destination.
No OAuth registration or external cache is required. Use the setup guide's
production shell for administrator and maintenance commands.

The admin additions use forward migrations from the matching initial baseline.
Do not rewrite applied migrations. Confirm the installed source and migration
prefix before an upgrade, then test a restored backup. Other migration graphs
need an explicit conversion. See [release policy](releases.md). Admin accounts
are not editable in Django admin because that would bypass access protection.
