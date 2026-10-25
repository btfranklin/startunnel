# Release tooling map

This map identifies the maintained release code, its callers, and its automated
checks. Use the [release policy](releases.md) for the required order and evidence
rules. Use the [repo-local release skill](../.agents/skills/release-startunnel/SKILL.md)
to guide an agent through that process. The
[public StarTunnel skill](../skills/startunnel/SKILL.md) helps operators and
agents use an instance; it does not own repository publication.

## Locations and ownership

| Location | Purpose | Execution status |
|---|---|---|
| `scripts/` | Maintained commands and shared proof code | Supported; used by local commands and CI |
| `tests/` | Regression tests, synthetic fixtures, and traffic drivers | Supported; collected by the documented test gates |
| `.github/workflows/` | Source CI, candidate validation, promotion, and diagnostics | Supported; actions are pinned |
| `.github/actions/proof-build/` | Shared Buildx and host build-cache credential setup | Supported; credentials do not enter proof containers |
| `.agents/skills/release-startunnel/` | Agent instructions for this repository's release process | Supported; commands remain in `scripts/` |
| `releases/` and `VERSION` | Reviewed version, compatibility, and operator instructions | Source inputs; never substitute for runtime evidence |
| `Dockerfile`, `compose*.yaml`, and `docker/` | Runtime, proof-helper, production, database, and proxy definitions | Shared source inputs; isolated proof projects own disposable resources |
| `artifacts/` | Generated proof reports and logs | Evidence output; maintained commands stay in `scripts/` |

## Release commands

All maintained Python files below are included in `pdm run types` and the
aggregate `pdm run check`. Test locations identify automated behavior checks;
they do not imply complete branch coverage or a real registry test.
The source security lane is separate: run `pdm run security-release source`
before pushing release-tooling changes. The aggregate check does not run Bandit
or the npm vulnerability audit.

| Command or module | Caller and responsibility | Automated tests |
|---|---|---|
| `scripts/release_candidate.py` | Candidate workflow and promotion; select exact source CI, retain attempts, verify evidence | `tests/structure/test_candidate_promotion.py`, `tests/tools/test_proof_evidence.py` |
| `scripts/release_record.py` | Record CLI and workflow; validate compatibility and write deployment files | `tests/structure/test_versioned_releases.py` |
| `scripts/release_workflow.py` | Promotion workflow; typed record and release-note steps | `tests/structure/test_versioned_releases.py` |
| `scripts/release_draft.py` | Promotion workflow; create or recover only a matching draft | `tests/structure/test_candidate_promotion.py` |
| `scripts/release_status.py` | Read-only CLI and workflow summaries; readiness, blockers, attempts, artifact IDs, and dependency timing | `tests/tools/test_release_status.py` |
| `scripts/check_release_references.py` | Aggregate gate; current-version statements, links, and supported predecessors | `tests/tools/test_release_references.py` |
| `scripts/verify_public_release.py` | Public release, anonymous image identity, deployment record, and guide verification | `tests/tools/test_public_release.py` |
| `scripts/release_cleanup.py` | Local cache preview and removal; protect retained proofs and unknown directories | `tests/tools/test_release_cleanup.py` |
| `scripts/stamp_client.py` | Docker build; stamp server and downloadable CLI identity | `tests/tools/test_client_build.py`, `tests/docs/test_agent_cli.py` |
| `scripts/run_security_release.py` | Source CI and candidate image scan | `tests/structure/test_secret_scan_scope.py`; hosted source and image scanner runs |

The promotion workflow calls typed modules for record and note generation.
Executable Python in a workflow heredoc does not receive the repository's
module type checks. Keep new Python logic in `scripts/`.

## Proof commands

| Command or module | Caller and responsibility | Automated tests |
|---|---|---|
| `scripts/run_isolated_test_lane.py` | Source CI and system workflow; select an isolated lane | `tests/structure/test_release_automation.py` |
| `scripts/system_proof.py` | System lane; contents, restart, reader recovery, backup, restore, permissions, proxy, and image identity | `tests/structure/test_release_automation.py`; hosted system proof |
| `scripts/proof_runtime.py` | Typed health and image-content probes executed inside the exact runtime | `tests/structure/test_release_automation.py` |
| `scripts/prove_production_release.py` | Production installation proof; production secrets, platform, CLI, admin receipt and audit, two-agent exchange, and cleanup | `tests/tools/test_production_release_proof.py` |
| `scripts/prove_admin_upgrade.py` | Source CI; synthetic initial baseline and published v0.1.0 migration and recovery | `tests/tools/test_published_upgrade_proof.py`, `tests/structure/test_release_automation.py` |
| `scripts/prove_published_upgrade.py` | Source CI and exact-image upgrade checks for supported published predecessors | `tests/tools/test_published_runtime_upgrade_proof.py`, `tests/structure/test_release_automation.py` |
| `scripts/prove_activity_restart.py` | System proof; an active reader survives a web restart | `tests/structure/test_release_automation.py` |
| `scripts/run_full_load_profile.py` | Candidate load job; fixed workload, observations, identity, and cleanup | `tests/tools/test_load_observations.py`, `tests/structure/test_release_automation.py` |
| `tests/load/profile.py` and `tests/load/run_profile.py` | Load helper; fixture setup, traffic, drain, and cleanup | `tests/tools/test_load_profile.py`, load observation and diagnostic tests; hosted full proof |
| `scripts/load_observations.py` | Load runner; bounded safe numeric collection | `tests/tools/test_load_observations.py` |
| `scripts/proof_pool_reader.py` | Observation runner; read approved multiprocess pool fields | `tests/tools/test_load_observations.py` |
| `src/core/pool_metrics.py` and `src/core/metrics.py` | Opt-in worker pool samples and fixed database failure counters | `tests/unit/test_pool_metrics.py`, `tests/unit/test_dependency_metrics.py`, API contract and load observation tests |

Source CI checks restored data from each published predecessor listed by its
workflow. A version in `supported_from` also needs exact-image upgrade and
recovery evidence. Do not infer that a source-only test proves a packaged image.

## Shared code and diagnostics

| Module | Responsibility | Automated tests |
|---|---|---|
| `scripts/compose_isolation.py` | Approved project names, safe environment, image checks, and resource cleanup | Candidate promotion, release automation, disposable image cleanup, proxy, and observation tests |
| `scripts/proof_evidence.py` | Input fingerprints, report checksums, successful selection, and rejection of incomplete evidence | `tests/tools/test_proof_evidence.py`, candidate promotion tests |
| `scripts/proof_build.py` | Scoped cache import/export and current-source helper provenance | `tests/tools/test_proof_build.py`, evidence tests |
| `scripts/proof_timing.py` | UTC boundaries and monotonic phase time | `tests/tools/test_load_observations.py`, helper benchmark tests |
| `scripts/docker_stats.py` | Safe numeric Docker resource reads | Observation and resource measurement tests |
| `scripts/json_data.py` | Checked JSON values for host tools | Used by typed tool tests; no separate dedicated test module |
| `scripts/benchmark_proof_helpers.py` | Compare cold full, cold focused, and warm focused helpers on one host | `tests/tools/test_benchmark_proof_helpers.py` |
| `scripts/load_diagnostic.py` and `scripts/validate_load_diagnostic.py` | Hold a bounded lock and verify the expected diagnostic failure | `tests/tools/test_load_diagnostic.py` |
| `scripts/run_capacity_diagnostic.py` and `scripts/capacity_probe_overlay.py` | Short, non-promotable capacity investigation | `tests/tools/test_capacity_diagnostic.py` |
| `scripts/run_resource_measurement.py` | Idle CPU, memory, and recurring work | `tests/tools/test_resource_measurement.py`, release automation tests |

Diagnostics are separate from promotion evidence. Their presence does not add
an automatic extra release gate. Run the diagnostic that answers the current
question. Preserve its source, inputs, limits, results, and cleanup.

## Production operations used by release checks

| Module or command | Purpose | Automated tests |
|---|---|---|
| `scripts/start_production.py` | Validate configuration and start a selected immutable image | `tests/tools/test_production_entrypoint.py` |
| `scripts/upgrade_production.py` | Preview and apply a release-record upgrade | `tests/structure/test_versioned_releases.py`; restored-database proofs |
| `scripts/generate_production_secrets.py` | Create private independent secrets without replacement | `tests/tools/test_production_secrets.py`, production proof tests |
| `scripts/rotate_postgres_passwords.py` | Rotate runtime and admin passwords with recovery checks | `tests/tools/test_postgres_password_rotation.py` |
| `docker/app/entrypoint.sh` | Load mounted secrets and select the database role | `tests/tools/test_production_entrypoint.py`; production image proof |
| `scripts/backup-startunnel.sh` and `scripts/verify-startunnel-restore.sh` | Coordinated backup and disposable restore | `tests/tools/test_backup_script.py`; system and restored-database proofs |
| `scripts/container-status.sh` and `scripts/container-logs.sh` | Bounded operational inspection | No dedicated automated tests identified |

Shell files and Compose definitions do not receive Python type checks. Their
syntax, input boundaries, and behavior require their own tests and real isolated
checks. They must not be described as strictly typed Python.

## Workflow map

| Workflow | Calls and acceptance role | Automated checks |
|---|---|---|
| `.github/workflows/ci.yml` | Aggregate source gate; CLI matrix; PostgreSQL and upgrade/recovery; agents; examples; browsers | `actionlint`, release automation and type-gate tests; actual source CI |
| `.github/workflows/release-candidate.yml` | Exact source selection, one runtime build, required proofs, and retained candidate record | `actionlint`, candidate promotion, evidence, and versioned-release tests |
| `.github/workflows/release-validation.yml` | Security, system and production installation, fixed load; optional paid live-agent proof | `actionlint`, release automation and evidence tests; actual image proofs |
| `.github/workflows/release.yml` | Exact tagged evidence, digest promotion, optional Scribe summary, and draft | `actionlint`, candidate promotion and versioned-release tests |
| `.github/workflows/release-diagnostics.yml` | Helper build comparison and controlled lock run | `actionlint`, helper benchmark and lock diagnostic tests |
| `.github/workflows/capacity-diagnostic.yml` | Short fixed-budget capacity observation | `actionlint`, capacity diagnostic tests |
| `.github/actions/proof-build/action.yml` | Host-side build cache credentials and Buildx setup | `actionlint`, release automation and build tests |
| `.github/workflows/website.yml` | Independently publish the static site when enabled | `actionlint`, website publication structure tests and website build tests |

The workflow tests use controlled inputs. They do not prove GitHub permissions,
registry access, runner timing, or external service availability. A release
still needs the actual required run results.

## Type and test boundaries

`scripts/check_types.py` discovers tracked and nonignored Python. It uses strict
mypy settings with explicit, unimported, and decorated `Any` disabled. It checks
tools and tests as well as application code and migrations. The standalone CLI
also has its Python 3.10 check. The load driver has the additional
`disallow_any_expr` setting. A strict type check does not validate external JSON;
use the checked JSON readers and runtime contracts at those boundaries.

Only maintained repository source receives continuous test and type checks.
Generated proof reports are evidence output, not executable release tooling.

The aggregate coverage threshold applies to `src/`. It does not establish a
coverage percentage for host scripts. The tests listed above establish their
tested behaviors; full command success and cleanup also need integration proof.

## Supported execution paths

Use the maintained commands listed in this map. New reusable release logic
belongs in a repository module with regression tests and strict type checks.
Do not introduce a second executable release procedure in generated output.

Update this map when a tool, caller, test, or type boundary changes. Keep the
map limited to repository-owned source and its supported validation paths.
