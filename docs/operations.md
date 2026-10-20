# Operations

For a new shared instance, start with the [team setup index](setup/README.md).
It has complete guides for AWS EC2, AWS Lightsail, DigitalOcean, and an existing
Linux server. This page is the service and configuration reference.

## Local development

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
operator's approved private secret channel to the CLI machine. Keep the copy
private, then remove the temporary container copy. Do not print the key or put it
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
| `caddy` | Optional TLS proxy, enabled by the `edge` profile |

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
| `STARTUNNEL_METRICS_TOKEN` | required in production | Metrics bearer token |

Admin status and capabilities, plus `inspect_runtime`, report the effective
active credential limit from this setting.

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

Web startup requires PostgreSQL and completed migrations. It does not wait for
maintenance health. Keep maintenance running for cycle deadlines and retention
deletion. Expired content remains unreadable while maintenance is unavailable.

Maintenance records reconciliation, deletion, error, and notification counts in
PostgreSQL. With no notifications or due deadlines, it reconciles once per
minute and refreshes its local heartbeat. Earlier deadlines and database
notifications can wake it sooner.

Database query timing applies to each Django connection in its process. It
includes queries from synchronous views, asynchronous adapters, commands, and
maintenance work. In production Compose, `/metrics` combines query metrics
from Uvicorn workers in the web container. Caddy forwards this endpoint. Supply
the `X-Metrics-Token` header with the
configured metrics token. Missing or incorrect tokens return HTTP 404. The
endpoint does not combine query metrics from other containers. Query labels contain
only the SQL operation type.

## Idle runtime

The default deployment uses one web worker. These activities continue with no
clients and no due application work:

| Component | Recurring work | Reason |
|---|---|---|
| Web server | Uvicorn checks its server state every 100 ms and updates Date headers each second | Standard server lifecycle |
| Web and maintenance notification listeners | Block on PostgreSQL socket activity | Immediate delivery without periodic polling |
| Maintenance | Reconciles persisted deadlines every 60 seconds | Recovery after missed notifications; heartbeat and expired-record cleanup |
| Connection pools | Check for excess idle connections every 600 seconds | Release unused connections; pool workers otherwise block |
| Docker health checks | Run every 60 seconds; every 2 seconds during startup | PostgreSQL readiness, web readiness, and maintenance heartbeat |
| PostgreSQL | Native writer, checkpoint, and vacuum scheduling | Durable writes and database upkeep |
| Optional Caddy | Certificate renewal checks every 10 minutes, OCSP checks each hour, and storage cleanup each day | Automatic TLS upkeep |

The web readiness probe uses the runtime image's built-in `wget`. With one web
upstream, Caddy returns request failures directly. Docker health checks report
readiness.

PostgreSQL defaults include 200 ms background-writer and WAL-writer delays, a
60-second autovacuum interval per database, and a five-minute checkpoint
timeout. Idle writers can sleep longer; these values do not mean that each
timer performs a write. Keep the database's durability and cleanup defaults.
See the PostgreSQL [resource](https://www.postgresql.org/docs/18/runtime-config-resource.html),
[WAL](https://www.postgresql.org/docs/18/runtime-config-wal.html), and
[vacuum](https://www.postgresql.org/docs/18/runtime-config-vacuum.html) settings.

Uvicorn's timer remains part of the supported server. Extra web workers add
server loops, pools, and a supervisor. Connection health checks run on checkout,
not on a background timer. Metrics collection runs when a client requests it.
Listener reconnect attempts use a 1–60 second backoff after a failure.
Caddy retains its standard [certificate maintenance](https://github.com/caddyserver/certmagic/blob/v0.24.0/maintain.go)
and [storage cleanup](https://github.com/caddyserver/caddy/blob/v2.10.2/modules/caddytls/tls.go).

Use the [resource measurement lane](testing.md#performance-acceptance) to check
CPU, memory, readiness, and deletion. CPU percentages refer to one core, not
the whole host. Host operating-system work, the Docker daemon or virtual
machine, external monitoring, backups, and certificate issuance are separate
costs.

### Reference measurement

On 2026-10-18, a ten-minute idle test used production settings, one web worker,
normal Docker health checks, and Caddy with a private TLS certificate. After a
two-minute warmup, the containers used:

| Service | CPU, as a percentage of one core |
|---|---:|
| Web | 0.328% |
| PostgreSQL | 0.173% |
| Maintenance | 0.069% |
| Caddy | 0.024% |
| **Total** | **0.594%** |

Container memory ranged from 269 to 290 MiB, including database cache. Each
scheduled health check ran ten times. Maintenance reconciled ten times, with
no due work or notification wakes. The 1% idle CPU target remains unchanged.

After traffic warmup, total container memory settled near 306 MiB. Caddy used
about 25 MiB, with compression buffers among its largest heap allocations.
Compression remains enabled to reduce response sizes.

The test host was an Apple M3 Pro with 18 GiB RAM. Docker Desktop 29.5.3 used a
Linux virtual machine with ten CPUs and 7.65 GiB available RAM. Repeat the
measurement on the deployment host before using these values for capacity
planning.

## Maintenance recovery

The worker holds a PostgreSQL advisory lock. A second worker waits without doing cleanup. If the active worker stops, another worker can acquire the lock and process overdue work. On startup it registers `LISTEN` before it reads deadlines, which closes the check-then-sleep race.

Inspect safe state with:

```shell
docker compose exec web env -u PROMETHEUS_MULTIPROC_DIR \
  /usr/local/bin/startunnel-entrypoint /app/.venv/bin/python manage.py inspect_runtime
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
The parent directory must be accessible. The helper makes an online dump
while the application stays available. It defaults to development Compose
files unless `COMPOSE_FILE` is set. The database backup does not include the
secret files required for recovery; preserve them as described in the
production guide. There is no cache or archive volume to coordinate.

## Logs and secrets

Use `scripts/container-logs.sh` for bounded development logs. For production,
load the production configuration and use the [production log command](setup/maintain.md#fault-checks).
Logs must not contain admin keys, agent keys, addresses, cursors, passwords, message content,
or identity data. Fixed internal warnings remain visible. Exception entries show
the type and bounded code locations, without exception values. A failed cycle
deletion also shows its cycle UUID. Keep `.env` and `.env.tutorial` untracked and
at mode `0600`.

## Production

Use [Install StarTunnel](setup/install.md) for the complete production procedure,
including the image, Linux secret-file ownership, DNS, HTTPS, and initial administrator access.
Use [Get the application image](setup/image.md) to select an official release
or build a custom image. See [Release policy](releases.md) for publication and compatibility.

Application containers have read-only filesystems and a 64 MiB `/tmp` tmpfs.
The maintenance heartbeat and production web metrics use this temporary storage.
It is cleared when the container stops; PostgreSQL holds durable data. Caddy
has a separate 32 MiB `/tmp` tmpfs and persistent certificate/config volumes.
Tmpfs can use host swap. Use encrypted swap, or disable swap, if temporary data
must not reach an unencrypted disk.

`compose.prod.yaml` uses immutable images and Docker secret files.
`STARTUNNEL_APP_IMAGE` selects the reviewed image with an `@sha256:` digest;
`STARTUNNEL_DOMAIN` selects the public hostname. The production launcher checks
the secret files and every enabled image, then starts without a host build.
`STARTUNNEL_BASE_URL` selects the instance origin and transport for the
`migrate`, `web`, and `maintenance` services. If it is unset, Compose uses
`https://` with `STARTUNNEL_DOMAIN`. The included Caddy edge keeps its HTTPS
default. For HTTP on a trusted container, VPN, or private network, set
`STARTUNNEL_BASE_URL` to that origin and configure its private route and port.
The HTTP or HTTPS scheme sets the default redirect and secure-cookie
settings. `STARTUNNEL_ALLOWED_HOSTS` defaults to `STARTUNNEL_DOMAIN`, but you
can set it to a comma-separated list of Django host names or wildcard domains.
`STARTUNNEL_BASE_URL` does not need to match this list. Django checks each
request host against `ALLOWED_HOSTS`.

`STARTUNNEL_CSRF_TRUSTED_ORIGINS` defaults to empty. Same-origin browser
requests work without an extra entry. Add origins in Django's format only when
the browser uses a different origin; Django checks those origins.

`STARTUNNEL_TRUSTED_PROXY_CIDRS` selects the direct peers whose forwarding
headers the application accepts. It controls `Forwarded`, `X-Forwarded-For`,
`X-Forwarded-Host`, `X-Forwarded-Port`, and `X-Forwarded-Proto`. The protocol
header controls HTTPS detection. The client address controls product login,
Django admin login, and failed admin-key authentication limits. Other peers
lose these headers, and the limits use their direct address.

The base and development defaults trust only `127.0.0.0/8`. Production adds
the bundled Caddy address as one `/32` peer to the configured list. Caddy
replaces incoming client-address and protocol headers before forwarding.
For an external proxy, add only its direct address or required network. That
proxy must replace incoming forwarding claims. Existing explicit values are
preserved. Replace the former `127.0.0.0/8,172.16.0.0/12` default in an existing
environment file; keeping it still trusts the full private range.

`STARTUNNEL_PROXY_IPV4_PREFIX` selects three private IPv4 octets. Its default
is `172.30.254`. The dedicated proxy network uses `.0/24`, its dynamic pool
uses `.0/25`, and Caddy uses the reserved `.254` address. Only web and Caddy
join that network. PostgreSQL uses the separate application network. Select
an unused private prefix if the default conflicts with another host network.
The [shared Caddy service](../compose.caddy.yaml) owns the edge configuration
used by production and the isolated proxy proof.

StarTunnel does not set HSTS. The operator can set the
`Strict-Transport-Security` header at the reverse proxy for an HTTPS origin.
Choose its lifetime and subdomain scope for the domains that you control.
Removing the header does not clear an existing browser policy. To clear that
policy, serve `Strict-Transport-Security: max-age=0` over HTTPS before you stop
HTTPS. See [HSTS expiration](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Strict-Transport-Security#expiration).

`STARTUNNEL_MAX_REQUEST_BODY_BYTES` controls the application request-body limit.
The default is 131,072 bytes; the operator can select up to 1,048,576 bytes.
Caddy forwards requests to this guard. The message-content limit remains
65,536 bytes, independent of the size of its JSON request.

The `--edge` option enables the included Caddy TLS service. The four setup
guides use it. Without that profile, configure an external TLS proxy for an
HTTPS origin or a private route for an HTTP origin. Set the origin port to
match the selected proxy or container route. The standard setup uses DNS and
HTTPS, encrypted storage, and a backup destination. The separate [IP certificate path](setup/no-domain.md) does not
need DNS.
The launcher uses loaded `COMPOSE_FILE`, `COMPOSE_PROJECT_NAME`, and
`COMPOSE_PROFILES` values. If `COMPOSE_FILE` is unset, it uses `compose.yaml`
and `compose.prod.yaml`. The upgrade helper uses the same selected Compose
project and profiles. Use `--env-file` for a config file other than
`production.env`; load that file in the shell before preview or apply.
Each apply keeps a unique mode `0600` snapshot of the old environment file and
prints its path. See [Maintain the instance](setup/maintain.md) before you retry
an interrupted upgrade.
No OAuth registration or external cache is required. Use the setup guide's
production shell for administrator and maintenance commands.

Database changes use forward migrations from the matching initial baseline.
Do not rewrite applied migrations. Confirm the installed source and migration
prefix before an upgrade, then test a restored backup. Other migration graphs
need an explicit conversion. See [release policy](releases.md). Admin accounts
are not editable in Django admin because that would bypass access protection.

## Rotate PostgreSQL passwords

Use the configured production shell from [Maintain the instance](setup/maintain.md#start-each-server-session).
Make a verified backup and select a new private directory for each rotation.
The helper writes new password files without showing their values. It saves a
private rollback file before it changes either database role.
The helper and Compose use the same `STARTUNNEL_SECRETS_DIR` setting.
Generated passwords are independent random values. Existing or replacement
password files can contain other nonempty, single-line UTF-8 text, including
spaces and punctuation. They cannot contain a NUL byte.

```shell
umask 077
mkdir -m 0700 /srv/startunnel/password-rotation
python3 scripts/rotate_postgres_passwords.py prepare --directory /srv/startunnel/password-rotation
python3 scripts/rotate_postgres_passwords.py rotate \
  --new-admin-file /srv/startunnel/password-rotation/postgres_admin_password.new \
  --new-runtime-file /srv/startunnel/password-rotation/postgres_runtime_password.new \
  --rollback-file /srv/startunnel/password-rotation/rollback.json
```

The helper tests both new passwords, replaces the secret files, restarts the
database clients, and checks the running application. If a step fails, it
tries to restore the prior passwords. Read the database and Compose errors
shown on stderr. If automatic restore does not complete, correct the reported
fault and run the saved rollback command:

```shell
python3 scripts/rotate_postgres_passwords.py rollback \
  --rollback-file /srv/startunnel/password-rotation/rollback.json
```

The rollback command also reverses an accepted rotation. Keep the rollback
file private until the new credentials pass the production checks. Do not put
the password values in commands or shell history.
