# Testing

Use the smallest lane that proves the change. Every Compose proof must use the isolated runner and a disposable project name.

## Local gates

```shell
pdm run check
pdm run test
pdm run openapi
```

`pdm run check` runs formatting, lint, types, repository checks, migration checks, OpenAPI checks, and the fast test suite.
It also checks local Markdown files and heading anchors with `linkbust`.
External web links are not fetched. Run `pdm run linkbust --no-color` for a
focused link check. Documentation tests parse CLI examples with the actual
command-line parser and compare documented CLI environment names with the
client source.

Documentation checks validate links, heading anchors, routes, example includes,
and CLI syntax. They do not require fixed prose or specific sentences.

## Compose lanes

```shell
pdm run python scripts/run_isolated_test_lane.py canonical
pdm run python scripts/run_isolated_test_lane.py postgres
pdm run python scripts/run_isolated_test_lane.py examples
pdm run python scripts/run_isolated_test_lane.py agents
pdm run python scripts/run_isolated_test_lane.py browser
```

The `postgres` lane proves real PostgreSQL constraints, transactions, rate-limit atomicity, `LISTEN`/`NOTIFY`, lifecycle concurrency, maintenance deadlines, and runtime-role restrictions. The privilege test grants the runtime role normal table permissions before it checks the migration's history-delete revocation.

The canonical lane must prove migration from a fresh database. The PostgreSQL
lane checks the current initial schema, constraints, and triggers. There is no
upgrade path from the old migration graph in this repository.

## Required focused evidence

- Identity changes: local login, login throttling, account creation, last-active-human protection, password change, and session invalidation.
- Credential changes: one-time key display, digest storage, instance ownership, independent human deactivation, and revocation.
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
