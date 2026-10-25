# Testing

Use the smallest lane that proves the change. Every Compose proof must use the isolated runner and a disposable project name.

The runner discards ambient application and Compose settings. It supplies its
own files, project, and test database URLs. It checks the actual Docker endpoint
and refuses existing project resources.

Compose runners provide a tmpfs at `/tmp` for fixture outputs. This mount does
not permit direct execution. Executable fixtures use temporary directories
under `artifacts` and remove them after the test. Direct local
provisioning commands can use a chosen existing directory. They still create a
new mode-0600 file and refuse an existing output path.

Run the local aggregate check as well as the canonical container lane. Compose
configuration tests require the host Docker CLI and skip inside test containers
that do not have it. Test containers do not receive the host Docker socket.

## Local gates

```shell
pdm run check
pdm run test
pdm run openapi
```

Each test run creates a fresh test database by default. `--reuse-db` is an
explicit local option. With SQLite, bare `pdm run test` deselects PostgreSQL
tests. Explicit `-m postgres` selection requires a PostgreSQL test database and
fails with the isolated lane command when that setup is missing. Browser and
live-agent tests keep their own dependency and opt-in checks. Credential rotation
tests require TCP connections so they can verify password authentication.
The `sqlite_only` marker is limited to the absent-PostgreSQL dependency check
and deliberate stored-state corruption that PostgreSQL guards reject. These
cases are deselected on PostgreSQL. Ordinary API, async, retention, and lifecycle
tests run on both backends. Async tests that use separate connections commit
their fixture data with `transaction=True`.

`pdm run check` runs formatting, lint, types, repository checks, migration checks, OpenAPI checks, and the fast test suite.
It runs the offline Perfect Doc structure gate and checks local Markdown files and heading anchors with `linkbust`.
External web links are not fetched. Run `pdm run linkbust --no-color` for a
focused link check. Documentation tests parse CLI examples with the actual
command-line parser and compare documented CLI environment names with the
client source.
CLI contract tests compare agent and admin operations with OpenAPI, including
admin query fields. The CLI response contract gate compares the bundled offline
definitions with the generated OpenAPI responses and their referenced types.
Version checks cover sync and async requests, rejected writes without database
work, older-client update messages, and matching server and client build labels.
Run `pdm run openapi` to update both artifacts. Response workflow tests use
the published definitions to connect writes, discovery, paginated reads, and
closure. Standalone tests also check offline configuration and executable paths.
Tutorial failure tests check cycle cleanup and reuse of the
close idempotency key after an uncertain result.

The aggregate gate also runs the dependency-free client smoke suite and checks
CLI types. Run the smoke suite directly with:

```shell
python3 tests/cli_standalone.py
```

It runs a copied client outside the checkout against a local synthetic HTTP
server. CI runs it without project packages on Linux, Windows, and macOS with Python
3.10 and 3.14. These jobs check real runtimes; the existing AST check separately
checks Python 3.10 syntax. The isolated examples lane proves the installed
client against the application and PostgreSQL.

## Select a lane

Run `pdm run check` for each change, then add the applicable proof:

| Change | Additional verification |
|---|---|
| API, trees, lifecycle, receipts, or rate limits | Isolated `postgres` lane |
| Models or migrations | `postgres` lane and `prove_admin_upgrade.py` |
| CLI or examples | Isolated `examples` lane; standalone CLI smoke test |
| Agent exchange | Isolated `agents` lane |
| Templates, forms, or browser behavior | Isolated `browser` lane |
| Deployment, backup, restore, or database grants | Isolated `system` lane |
| Trusted proxies, forwarded headers, or the TLS edge | Isolated `proxy` lane and `system` lane |
| Markdown only | `pdm run docs-structure`, included in the aggregate |
| Script behavior | Focused tests in `tests/tools/` |

The `system` lane includes examples, agents, and browser flows. See the
[tool index](../scripts/README.md) for script owners and the commands below for
lane syntax. The canonical container lane verifies a fresh environment.

## Documentation structure

Install the published [Perfect Doc native executable](https://github.com/btfranklin/perfect-doc/releases/tag/v0.1.0)
and verify it against that release's `SHA256SUMS`, or use its Homebrew tap.
The aggregate check includes this gate. Run it directly when changing Markdown:

```shell
pdm run docs-structure
```

CI installs the checksummed Linux executable before running this gate.
The Docker test image builds the same release from a fixed source revision
for its architecture. A separate build stage keeps the Rust compiler out of
the test image. The tool is not added to the application image or installed
inside a test run.
The repository's `perfect-doc.toml` checks Markdown headings, fences, tables,
references, and file names. Its virtual `/source/` namespace maps served
application URLs to their source documents for the scan. The existing
`scripts/check_docs.py` gate separately checks application route ownership.
Django templates continue through djLint and browser checks.

Documentation checks validate structure, links, route ownership, runnable
examples, and API or CLI contracts. Do not require particular sentences,
heading text, marketing words, or paragraph lengths. Example programs must
report failure through their exit status; their printed prose can change.

## Coverage

The fast aggregate gate requires at least 90 percent total coverage and complete
line and branch coverage for its critical protocol functions.
Beacon targets cover publication, withdrawal, board reads and waits, and text
and cursor validation. The PostgreSQL lane runs the `test-postgres` PDM composite.
It records a separate
`postgres-coverage.json` report, then requires complete line and branch coverage
for the recursive subtree query. It does not combine SQLite coverage with that
report or apply the fast suite's total threshold to PostgreSQL-only tests.

## Compose lanes

```shell
pdm run python scripts/run_isolated_test_lane.py canonical
pdm run python scripts/run_isolated_test_lane.py postgres
pdm run python scripts/run_isolated_test_lane.py examples
pdm run python scripts/run_isolated_test_lane.py agents
pdm run python scripts/run_isolated_test_lane.py browser
pdm run python scripts/run_isolated_test_lane.py proxy
```

The `postgres` lane proves real PostgreSQL constraints, transactions, rate-limit atomicity, `LISTEN`/`NOTIFY`, lifecycle concurrency, maintenance deadlines, and runtime-role restrictions.
It first runs the integration tests and checks their critical-coverage report.
It then runs all unit and API tests on PostgreSQL with `--no-cov`. This second
pass cannot replace the integration coverage evidence. CI uses the same lane;
the fast aggregate suite continues to run on SQLite.
The `proxy` lane uses production Django settings, the shared Caddy service,
and the unchanged production Caddyfile. Its browser checks HTTPS login, secure
cookies, replaced forwarding headers, a valid password change, and rejection of
a cross-origin CSRF request. Caddy issues a disposable local certificate for
`proxy-proof.internal`; the test browser accepts that issuer only for this
proof. This does not prove a public certificate. The runner selects a private
subnet without an existing Docker network overlap. It changes no operator
environment file and publishes no proxy proof ports.
The privilege test executes the production grant SQL with `psql` and compares
its permissions with the migration. Both use the same history-table list.
System and restore proofs run one shared role check. It verifies normal writes
and requires SQLSTATE `42501` for denied operations. Other database failures do
not count as permission denial.
Subtree tests check deep preorder pages, fixed snapshots, depth limits, and
invalid continuations. Lifecycle tests force both post/close lock orders and
both expiry-close/post orders, with PostgreSQL lock-wait evidence. Retention
tests delete a branched message tree and check mentions, Beacon content,
checkpoints, retry receipts, and the retained event spine.
Maintenance tests run two command processes against the active test database.
They stop the leader and check that the surviving process acquires the lock and
deletes new due work. Separate concurrent refresh tests check that batch deletion
preserves sessions and rate-limit buckets whose expiry moved into the future.
The notification recovery test terminates an established listener backend,
commits a reply while reconnection is paused, and checks both waiter channels
after subscriptions return. An activity read then checks the committed reply.

The canonical lane proves a fresh database. The PostgreSQL lane checks schema,
constraints, and triggers. Admin changes also require forward migration and
recovery proof on a restored matching initial baseline. Other historical graphs
are unsupported unless their conversion has separate evidence.
The agent lane starts maintenance because its end-to-end exchange also checks
physical deletion after the retention deadline.

## Restored initial baseline

Run this isolated PostgreSQL proof after the PostgreSQL lane:

```shell
pdm run python scripts/prove_admin_upgrade.py
```

The proof creates a fresh pre-change initial schema with historical migration
models, seeds disposable account and message data, and performs a real custom
`pg_dump` and `pg_restore` into a second database. It then applies the forward
migrations and checks account IDs and passwords, agent-key digests and
authentication, historical audit records, private recovery-key issuance, and
admin authentication. The one-time address migration must delete old tunnels,
addresses, messages, mentions, activity events, checkpoints, and agent retry
records. Tunnel control receipts are removed; other admin receipts remain.
Existing credentials must create a new tunnel and exchange new messages. A
second migration run must preserve that new tunnel, its address, and messages.

The restored historical baseline first passes through the session migration.
That step must preserve message IDs, ancestry, and absent labels. The later
address-storage reset is a separate expected outcome. The proof then dumps the
current pre-Beacon schema with new messages, session labels, a checkpoint, and
its current address. It restores that data into a third database, applies the
Beacon migration, verifies those records, and publishes a Beacon. It then
removes admin access, recovers a private admin key, and withdraws the Beacon
through the admin API. An exact retry must return the same operation receipt.
The known address, messages, and checkpoint must remain. Accounts, credentials,
and historical audit records must remain across both restores.

Before the shared-record ownership migrations, the proof seeds audit records,
agent and admin receipts, and content types with user and group permissions.
The migrations must keep their tables, row IDs, permission IDs, and joins.
An additional migration run must preserve those records and exact retries.

The isolated runner removes its containers and volumes. Dumps remain inside
the disposable container. Temporary snapshots and recovery keys stay private
and are removed at exit. Output reports safe check names, not keys or content.
CI runs this proof after its PostgreSQL lane.

On Linux, source proof helpers use the host user and group IDs so they can
write private bind-mounted reports. On macOS, Docker Desktop maps file
ownership and helpers use the image's default IDs. This avoids collisions
between macOS system groups and groups already present in the Linux image.
Candidate runtime images retain their original user settings. The `artifacts`
directory can be a symlink to another writable directory.

The default proof uses a synthetic matching initial baseline. For the published
v0.1.0 predecessor, download its `release.json` outside the checkout and run:

```shell
pdm run python scripts/prove_admin_upgrade.py --baseline-record /absolute/path/v0.1.0/release.json
```

This mode seeds and fully migrates the actual published v0.1.0 image, checks
its source and version labels, and restores its dump into a separate database.
The old image must authenticate existing passwords and keys and read the old
messages from that restored database. The forward upgrade must pass the reset,
new-write, repeat-migration, and recovery checks above. Source CI on `main`
includes both proof modes. This supports v0.1.0 in `supported_from`; it does not
verify a particular production backup or reverse migrations. Test a restored
copy of the actual installed database before an installation upgrade.

For each published v0.2.0 or v0.3.0 predecessor, download its attached `release.json`
outside the checkout and run:

```shell
pdm run python scripts/prove_published_upgrade.py --release-record /absolute/path/v0.2.0/release.json
pdm run python scripts/prove_published_upgrade.py --release-record /absolute/path/v0.3.0/release.json
```

This proof checks the release record against its source tag, pulls the
recorded immutable image, and checks its OCI source and version labels. It seeds
an account, agent key, admin key, and message tree with that runtime. It dumps
the database, restores it into a separate disposable database, and checks
account login, both existing keys, and message ancestry with the published
runtime. Current source then applies migrations, creates a new tunnel, closes a
due cycle through maintenance, and recovers a private admin key. Pass
`--candidate-image ghcr.io/...@sha256:...` to run the current-source checks in
an immutable candidate image. The isolated runner removes its containers and
volumes. This proves an upgrade from the selected published fixture; it does not
prove a specific production backup.
Source CI on `main` runs this proof after the published v0.1.0 proof.

## Required focused evidence

- Identity changes: password and key-only accounts, credential-type isolation, expiry, revocation, final-access removal, concurrent account changes, bootstrap, recovery, and browser session invalidation.
- Credential changes: digest storage, secret-file failures, lost responses, exact retries, changed-input conflicts, independent agent-key lifecycle, and revocation.
- Admin changes: complete unattended CLI setup and operation, audit attribution, receipt inspection, pagination, secret-free logs, and no-JavaScript browser controls.
- Lifecycle changes: exact deadline access, delayed close semantics, `forever`, tombstone deletion, and idempotent cleanup.
- Notification changes: register-before-reread, commit wakeup, listener reconnect, and bounded polling fallback.
- Rate changes: exact limits and atomic concurrent updates with real PostgreSQL.
- Beacon publication changes: domain and API tests, populated-page query bounds, real PostgreSQL write races, canonical and PostgreSQL lanes, and the restored current-database upgrade proof.
- Docker changes: fresh build, startup, healthy services, dynamic host-port output, backup, restore, and runtime-role denial.

Beacon expiry tests force both production tunnel-lock orders against cycle
close, publisher withdrawal, and admin withdrawal. PostgreSQL must report the
second writer as blocked by the first writer. Each race must leave one Beacon
end event, one end audit record, one board revision change, and contiguous
tunnel event positions.

## Performance acceptance

Use the isolated idle measurement lane after a fresh build:

```shell
pdm run python scripts/run_isolated_test_lane.py resources
```

The lane measures the current working tree. For a comparison, run
`pdm run python scripts/run_resource_measurement.py --baseline-ref REF`.
This option also checks out and measures the selected baseline in a temporary
detached worktree. Each stack uses one Uvicorn worker and no development
reloader, with the default connection pool limits. The reference stack contains
PostgreSQL, web, and maintenance. It does not include the optional Caddy edge.
The lane removes its stacks and volumes. It uses only internal clients. The
report is in `artifacts/resource-measurement-*.json`.
The lane needs a local Unix socket or local TCP Docker endpoint.

1. Warm the stack for two minutes.
2. Measure idle CPU and recorded activity counters for ten minutes.
3. Read CPU and memory counters through the local Docker Engine API. Record
   each service's CPU rate over its own measured interval. Capture health-probe
   events live. Read database and maintenance counters outside the CPU interval.
4. Run three idle-load-idle cycles and confirm that memory returns to a stable band.

Acceptance targets:

- candidate total idle CPU is below 1 percent of one core on the documented
  reference machine;
- routine maintenance reconciliation and Docker health probes run at most once
  per minute when no deadline is near;
- no container grows memory monotonically across the three cycles;
- Compose reaches ready state within two minutes on the reference machine.

Record the machine, Docker version, commit, duration, sample interval, and exact commands with each result.
The CPU interval includes normal health checks. It contains no measurement
commands inside containers. A restart, counter reset, or change to the container
set fails the measurement. Memory must settle for each service; a stable total
cannot hide growth in one service.

The [runtime inventory](operations.md#idle-runtime) lists dependency timers that
continue at idle. These timers are outside the once-per-minute application
work limit. Measure an operator's proxy and monitoring separately when those
services are outside the reference stack. The lane does not infer physical host
wakeups, energy use, or watts from CPU or probe counts.

## Candidate image evidence

Normal pushes run source and service checks, including browser and example
suites. After CI passes, `release-candidate.yml` on `main` reuses that completed
run and prepares the image. A repeated request reuses retained, verified
candidate evidence for the same commit. These checks use the immutable digest:

```shell
pdm run security-release source
pdm run security-release image --skip-build --image IMAGE_DIGEST
pdm run python scripts/run_isolated_test_lane.py system --candidate-image IMAGE_DIGEST
pdm run python scripts/run_full_load_profile.py --candidate-image IMAGE_DIGEST
```

The source check runs before expensive CI builds. It checks lock files, Bandit,
npm dependencies, and tracked or nonignored source files. Git-free source
exports are also supported. Ignored local reports and private input files are
outside this scan.

The image check scans the runtime image and its reports. It blocks high and
critical image vulnerabilities. It does not repeat source or dependency checks.
Both commands scan their current reports for secrets. Tool errors appear in the
job log. CI uploads reports written before a failure; a missing report cannot
make a failed job pass.

For official release promotion, use the official digest reference. Self-hosted
operators can use an image from their own registry pinned with `@sha256:`.
Its revision and version labels must match the source checkout. The isolated
runners pull and inspect the image revision and version before use, build only
proof helpers, and verify application service image IDs. Helper builds use BuildKit. Hosted proof builds import and export a separate
cache for each service and platform. Local builds use the local layer cache.
A cache failure is recorded and can fall back once to a build without the remote
cache. The build still checks source labels, platform, and image IDs. Cache
credentials are given only to the host build process. Changed source files
cause their copy layers to rebuild.
Browser and example suites run once in source CI. The system proof checks
health, runtime image contents, an active reader across a web restart, a
post-restart agent exchange, database privileges, backup, restore, and database
loss and recovery, plus the Caddy HTTPS and browser CSRF proof. It builds the
client and browser helpers that it uses.
Image scanning, system proof, and load testing run in parallel. All three must
pass for promotion. The system proof tests the exact runtime image in an
isolated development configuration and a separate production-settings proxy
service; it does not prove a particular public
server's TLS or secret-file configuration. Perform the external production
checks before publishing the reviewed draft. See [release policy](releases.md).

The image check reads static sources and collected manifest assets, loads CA
certificates, and checks that pip and documentation build tools are excluded.
It runs in the application container. It does not depend on Dockerfile stage
names, copy commands, or Python package paths. The canonical lane separately
checks that documentation tools work in the test image.

The load proof schedules 50 sends per second for five minutes with 100 activity
readers and 500 disposable agent credentials held by one fixture admin. Only its
isolated stack raises the active credential capacity to 500; per-credential rate
limits remain unchanged. Readers rotate credentials to distribute their traffic.
Long polls use a separate HTTP connection pool. The isolated web service uses
four workers with 18 database connections per pool. Message submissions overlap,
with at most 256 in flight, and have a bounded drain after the workload deadline.
Missed submissions, capacity exhaustion, request errors, and unfinished sends
fail the proof rather than extending its duration. Its report still requires
at least 95% of the expected sends and successful fixture cleanup.
CI retains each load attempt, including failed reports, safe status counts,
send latencies, and cleanup results. Candidate identity is checked before fixture
creation and again after the workload. The helper uses locked production Python
dependencies and current proof source. It contains no documentation, browser,
Node.js, or development test tools.

The load lane enables one-second pool sampling in each web worker through
`STARTUNNEL_PROOF_METRICS=1`. Normal application runs do not start this sampler.
Worker gauges use the existing multiprocess metrics directory and remove dead
worker values on shutdown. Host observations record per-service CPU, throttling,
memory, available pressure counters, database connection and lock counters,
pool waits, and collector cost. Counter reads run in a fixed worker pool through
the Docker Engine. Pool metrics come from the existing worker files, without
adding HTTP traffic. Each collection must finish within one second; a missed
deadline stops the collector and fails the proof. Driver observations record
scheduling lag and in-flight sends. Unavailable values are null. No report includes SQL text,
credentials, addresses, connection strings, or message content.

Safe reports also record build, startup, fixture, traffic, drain, verification,
and cleanup time. Collection is bounded and does not queue catch-up samples.
A failed or interrupted run keeps its partial reports. A successful release
proof requires complete observations and both fixture and stack cleanup.

Use the isolated helper benchmark to measure build cost without traffic:

```shell
pdm run python scripts/benchmark_proof_helpers.py
```

It compares the full development helper with cold and warm focused helpers on
one host. It checks the same source, platform, and locked inputs, tool imports,
image size, warm cache use, and cleanup. Its results are diagnostic evidence.

To test observations during a known failure, use a separate disposable stack:

```shell
pdm run python scripts/run_full_load_profile.py --candidate-image IMAGE_DIGEST --diagnostic-lock-seconds 10 --project-name stload-lock-diagnostic
pdm run python scripts/validate_load_diagnostic.py --project-name stload-lock-diagnostic
```

The first command must fail with a retained load report. The second command
checks for the expected failure, observed lock waits, and successful cleanup.
The lock lasts at most the selected interval and is removed if the child exits.
The workload still schedules 15,000 sends over five minutes with the standard
reader, credential, worker, and pool budgets. This run cannot certify a release.
It does not explain the cause of an earlier, uncontrolled failure.
