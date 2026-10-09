# Testing

Use the smallest lane that proves the change. Every Compose proof must use the isolated runner and a disposable project name.

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

Documentation checks validate links, heading anchors, routes, example includes,
and CLI syntax. They do not require fixed prose or specific sentences.

## Documentation structure

Install the published [Perfect Doc native executable](https://github.com/btfranklin/perfect-doc/releases/tag/v0.1.0)
and verify it against that release's `SHA256SUMS`, or use its Homebrew tap.
The aggregate check includes this gate. Run it directly when changing Markdown:

```shell
pdm run docs-structure
```

CI installs the checksummed Linux executable before running this gate.
The tool is not added to the application image or installed inside a test run.
The repository's `perfect-doc.toml` checks Markdown headings, fences, tables,
references, and file names. Its virtual `/source/` namespace maps served
application URLs to their source documents for the scan. The existing
`scripts/check_docs.py` gate separately checks application route ownership.
Django templates continue through djLint and browser checks.

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

## Restored initial baseline

Run this isolated PostgreSQL proof after the PostgreSQL lane:

```shell
pdm run python scripts/prove_admin_upgrade.py
```

The proof creates a fresh pre-change initial schema with historical migration
models, seeds disposable account and message data, and performs a real custom
`pg_dump` and `pg_restore` into a second database. It then applies the additive
admin migrations and checks account IDs and passwords, agent-key digests and
authentication, message IDs and ancestry, historical audit records, private
recovery-key issuance, admin authentication, and a new message write.

The isolated runner removes its containers and volumes. Dumps remain inside
the disposable container. Temporary snapshots and recovery keys stay private
and are removed at exit. Output reports safe check names, not keys or content.
CI runs this proof after its PostgreSQL lane.

This proves a synthetic matching initial baseline. It does not establish
compatibility with a production snapshot or a published predecessor, and it
does not prove reverse migrations. Test the exact installed baseline separately
before an installation upgrade. The first release keeps `supported_from` empty.

## Required focused evidence

- Identity changes: password and key-only accounts, credential-type isolation, expiry, revocation, last-access protection, concurrent account changes, bootstrap, recovery, and browser session invalidation.
- Credential changes: digest storage, secret-file failures, lost responses, exact retries, changed-input conflicts, independent agent-key lifecycle, and revocation.
- Admin changes: complete unattended CLI setup and operation, audit attribution, receipt inspection, pagination, secret-free logs, and no-JavaScript browser controls.
- Lifecycle changes: exact deadline access, delayed close semantics, `forever`, tombstone deletion, and idempotent cleanup.
- Notification changes: register-before-reread, commit wakeup, listener reconnect, and bounded polling fallback.
- Rate changes: exact limits and atomic concurrent updates with real PostgreSQL.
- Docker changes: fresh build, startup, healthy services, dynamic host-port output, backup, restore, and runtime-role denial.

## Performance acceptance

Use the isolated idle measurement lane after a fresh build:

```shell
pdm run python scripts/run_isolated_test_lane.py resources
```

The lane checks out `HEAD` in a temporary detached worktree as its baseline.
It compares that committed state with the current working tree. For a different
baseline, run `pdm run python scripts/run_resource_measurement.py --baseline-ref REF`.
It builds the baseline and candidate, runs both with one Uvicorn worker and no
development reloader, and removes both stacks and their volumes. It uses only
internal clients. The report is in `artifacts/resource-comparison-*.json`.

1. Warm the stack for two minutes.
2. Measure idle CPU and wakeups for ten minutes.
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

The main-branch CI candidate uses the release image's immutable digest:

```shell
pdm run python scripts/run_isolated_test_lane.py system --candidate-image IMAGE_DIGEST
pdm run python scripts/run_full_load_profile.py --candidate-image IMAGE_DIGEST
pdm run security-release --skip-build --image IMAGE_DIGEST
```

Replace `IMAGE_DIGEST` with the actual official digest reference. The isolated
runners pull and inspect the image revision and version before use, build only
proof helpers, and verify application service image IDs. The system proof covers
health, agent exchanges, browser flows, runtime database privileges, backup,
restore, database loss, and restarts. It tests the exact runtime image in an
isolated development configuration; it does not prove a particular public
server's TLS or secret-file configuration. Perform the external production
checks before publishing the reviewed draft. See [release policy](releases.md).

The load proof schedules 50 sends per second for five minutes with 100 activity
readers and 500 disposable credentials. Only its isolated stack raises the active
credential capacity to 500; per-credential rate limits remain unchanged. Readers
rotate credentials to distribute their traffic. Long polls use a separate HTTP
connection pool. The isolated web service uses four workers with 18 database
connections per pool. Message submissions overlap, with at most 256 in flight,
and have a bounded drain after the workload
deadline. Missed submissions, capacity exhaustion, request errors, and unfinished
sends fail the proof rather than extending its duration. Its report still requires
at least 95% of the expected sends and successful fixture cleanup.
CI retains failed load reports, including safe status counts and send latencies.
