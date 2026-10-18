# Testing

Use the smallest lane that proves the change. Every Compose proof must use the isolated runner and a disposable project name.

The runner discards ambient application and Compose settings. It supplies its
own files, project, and test database URLs. It checks the actual Docker endpoint
and refuses existing project resources.

Compose runners provide a tmpfs at `/tmp` for fixture outputs. Direct local
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

`pdm run check` runs formatting, lint, types, repository checks, migration checks, OpenAPI checks, and the fast test suite.
It runs the offline Perfect Doc structure gate and checks local Markdown files and heading anchors with `linkbust`.
External web links are not fetched. Run `pdm run linkbust --no-color` for a
focused link check. Documentation tests parse CLI examples with the actual
command-line parser and compare documented CLI environment names with the
client source.

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

## Compose lanes

```shell
pdm run python scripts/run_isolated_test_lane.py canonical
pdm run python scripts/run_isolated_test_lane.py postgres
pdm run python scripts/run_isolated_test_lane.py examples
pdm run python scripts/run_isolated_test_lane.py agents
pdm run python scripts/run_isolated_test_lane.py browser
```

The `postgres` lane proves real PostgreSQL constraints, transactions, rate-limit atomicity, `LISTEN`/`NOTIFY`, lifecycle concurrency, maintenance deadlines, and runtime-role restrictions. The privilege test grants the runtime role normal table permissions before it checks the migration's history-delete revocation.

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

This proves a synthetic matching initial baseline. It does not establish
compatibility with a production snapshot or a published predecessor, and it
does not prove reverse migrations. Test the exact installed baseline separately
before an installation upgrade. The first release keeps `supported_from` empty.

## Required focused evidence

- Identity changes: password and key-only accounts, credential-type isolation, expiry, revocation, final-access removal, concurrent account changes, bootstrap, recovery, and browser session invalidation.
- Credential changes: digest storage, secret-file failures, lost responses, exact retries, changed-input conflicts, independent agent-key lifecycle, and revocation.
- Admin changes: complete unattended CLI setup and operation, audit attribution, receipt inspection, pagination, secret-free logs, and no-JavaScript browser controls.
- Lifecycle changes: exact deadline access, delayed close semantics, `forever`, tombstone deletion, and idempotent cleanup.
- Notification changes: register-before-reread, commit wakeup, listener reconnect, and bounded polling fallback.
- Rate changes: exact limits and atomic concurrent updates with real PostgreSQL.
- Beacon publication changes: domain and API tests, populated-page query bounds, real PostgreSQL write races, canonical and PostgreSQL lanes, and the restored current-database upgrade proof.
- Docker changes: fresh build, startup, healthy services, dynamic host-port output, backup, restore, and runtime-role denial.

## Performance acceptance

Use the isolated idle measurement lane after a fresh build:

```shell
pdm run python scripts/run_isolated_test_lane.py resources
```

The lane measures the current working tree. For a comparison, run
`pdm run python scripts/run_resource_measurement.py --baseline-ref REF`.
This option also checks out and measures the selected baseline in a temporary
detached worktree. Each stack uses one Uvicorn worker and no development
reloader. The lane removes its stacks and volumes. It uses only internal
clients. The report is in `artifacts/resource-measurement-*.json`.

1. Warm the stack for two minutes.
2. Measure idle CPU and recorded activity counters for ten minutes.
3. Record cgroup CPU at the measurement boundaries, low-frequency memory,
   health-probe executions, database transactions, and available maintenance
   counters.
4. Run three idle-load-idle cycles and confirm that memory returns to a stable band.

Acceptance targets:

- candidate total idle CPU is below 1 percent of one core on the documented
  reference machine;
- no service wakes more than once per minute when no deadline is near;
- no container grows memory monotonically across the three cycles;
- Compose reaches ready state within two minutes on the reference machine.

Record the machine, Docker version, commit, duration, sample interval, and exact commands with each result.
The lane does not infer physical host wakeups, energy use, or watts from CPU or
probe counts.

## Candidate image evidence

A manual CI run on `main` builds and validates a distribution candidate.
Normal pushes run the source and service checks. Candidate proof uses the
release image's immutable digest:

```shell
pdm run python scripts/run_isolated_test_lane.py system --candidate-image IMAGE_DIGEST
pdm run python scripts/run_full_load_profile.py --candidate-image IMAGE_DIGEST
pdm run security-release --skip-build --image IMAGE_DIGEST
```

The security lane scans tracked and nonignored candidate files, or source files
in a Git-free export. It scans current security reports and the runtime image
separately. Ignored local reports and private input files are outside the
candidate scan. The lane also checks locked dependencies and blocks high and
critical image vulnerabilities.
Tool errors appear in the job log. CI uploads reports that were written before
a failure. A missing report does not make a failed security job pass.

For official release promotion, use the official digest reference. Self-hosted
operators can use an image from their own registry pinned with `@sha256:`.
Its revision and version labels must match the source checkout. The isolated
runners pull and inspect the image revision and version before use, build only
proof helpers, and verify application service image IDs. Helper builds use the
Docker cache; changed source files still cause their copy layers to rebuild. The system proof covers
health, runtime image contents, agent exchanges, browser flows, runtime database privileges, backup,
restore, database loss, and restarts. It tests the exact runtime image in an
isolated development configuration; it does not prove a particular public
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
CI retains failed load reports, including safe status counts and send latencies.
