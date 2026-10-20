# Repository tools

Use PDM commands where available. Start with [testing](../docs/testing.md) for
verification and [operations](../docs/operations.md) for a running instance.
Compose proofs must use disposable projects through the isolated runner.

## Validation

| Tool | Purpose |
|---|---|
| [check.py](check.py) | Run the aggregate fast gates: `pdm run check` |
| [check_critical_coverage.py](check_critical_coverage.py) | Check critical line and branch coverage |
| [check_docs.py](check_docs.py) | Check documentation routes, links, and example includes |
| [check_examples.py](check_examples.py) | Check example syntax and credential handling |
| [check_openapi.py](check_openapi.py) | Generate or check the public wire schema |
| [generate_client_contract.py](generate_client_contract.py) | Generate or check the bundled CLI response definitions |
| [check_secrets.py](check_secrets.py) | Scan the selected Git scope for secrets |
| [check_structure.py](check_structure.py) | Check repository ownership and import direction |
| [check_templates.py](check_templates.py) | Run template lint checks |

## Development and examples

| Tool | Purpose |
|---|---|
| [bootstrap_env.py](bootstrap_env.py) | Create local development settings |
| [start_development.py](start_development.py) | Start the local stack: `pdm run dev` |
| [run_tutorial.py](run_tutorial.py) | Provision and run the two-agent tutorial |
| [run_example_catalog.py](run_example_catalog.py) | Run non-OpenAI examples against an instance |
| [stamp_client.py](stamp_client.py) | Add build identity to the downloadable client |
| [vendor-assets.mjs](vendor-assets.mjs) | Update tracked browser assets: `npm run vendor` |

## Deployment and maintenance

| Tool | Purpose |
|---|---|
| [start_production.py](start_production.py) | Preview or apply an instance installation |
| [upgrade_production.py](upgrade_production.py) | Preview or apply a release-record upgrade |
| [generate_production_secrets.py](generate_production_secrets.py) | Create private instance secret files |
| [rotate_postgres_passwords.py](rotate_postgres_passwords.py) | Rotate database passwords with recovery checks |
| [backup-startunnel.sh](backup-startunnel.sh) | Create a database backup |
| [verify-startunnel-restore.sh](verify-startunnel-restore.sh) | Restore a backup into a disposable database and verify it |
| [container-status.sh](container-status.sh) | Show bounded container status |
| [container-logs.sh](container-logs.sh) | Read bounded service logs |

Use the [setup guides](../docs/setup/README.md) for required inputs and order.

## Isolated proofs

| Tool | Purpose |
|---|---|
| [run_isolated_test_lane.py](run_isolated_test_lane.py) | Run canonical, PostgreSQL, example, agent, browser, system, or resource lanes |
| [compose_isolation.py](compose_isolation.py) | Own disposable Compose resources and cleanup |
| [system_proof.py](system_proof.py) | Coordinate clean-stack exchange, restart, backup, and restore checks |
| [prove_admin_upgrade.py](prove_admin_upgrade.py) | Verify forward migrations and published-image backup recovery |
| [prove_activity_restart.py](prove_activity_restart.py) | Verify reader recovery across a web restart |
| [run_resource_measurement.py](run_resource_measurement.py) | Measure idle CPU, memory, and recurring work |
| [docker_stats.py](docker_stats.py) | Read Docker counters without running commands inside containers |
| [run_full_load_profile.py](run_full_load_profile.py) | Run the fixed load profile and record safe evidence |

## Release preparation

| Tool | Purpose |
|---|---|
| [release_candidate.py](release_candidate.py) | Reuse exact-commit CI and verify candidate evidence |
| [release_record.py](release_record.py) | Validate specifications and write deployment records |
| [release_draft.py](release_draft.py) | Verify draft-release identity and recovery |
| [run_security_release.py](run_security_release.py) | Run separate source and image security checks |

Follow the [release policy](../docs/releases.md). These tools do not make
normal source pushes into releases.
