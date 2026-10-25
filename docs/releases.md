# Release policy

StarTunnel ships versioned GitHub releases and official container images.
`main` contains development work. A source push does not update any installation.
Operators explicitly select a published version and deploy its immutable digest.
Automatic updates are not enabled.

The current release specification is v0.4.1. Use the published release's
`release.json` and tagged guides for installation or upgrades. v0.1.0 keeps its
own [tagged guides](https://github.com/btfranklin/startunnel/blob/v0.1.0/README.md)
and deployment record. Never use another version's record to deploy a change.

## Versions and supported databases

Use `vMAJOR.MINOR.PATCH` tags. Patch releases contain compatible fixes; minor
releases add compatible functionality. Breaking API, configuration, or database
changes need an explicit compatibility decision and migration instructions,
including during the initial `0.x` series. Never move a published tag or replace
a published version's deployment record. Fix a released defect in a new version.

The [v0.3.0 official CLI requirement](decisions.md#official-cli-release-compatibility)
is an explicit exception to compatible minor releases. It requires a separate
client update after the server update.

The first published baseline is `v0.1.0`. The standard released path to
`v0.4.1` passes through `v0.2.0`. For a v0.1.0 or unversioned installation,
start with the [tagged v0.2.0 update guide](https://github.com/btfranklin/startunnel/blob/v0.2.0/docs/setup/maintain.md#update-the-application).
That guide's baseline requirements still apply. This route does not make every
unversioned migration graph supported.

A direct upgrade from an unversioned source needs separate review and forward
migration and recovery proof on a restored database from the exact installed
source. The helper requires an ancestor source commit and unchanged applied
migration files. These structural checks do not prove compatibility. The admin
migrations and the [one-time tunnel reset](setup/address-storage.md) need the
same restored-database proof. Older graphs require a separately reviewed
conversion tested on a restored database.

For each release, update the root `VERSION` file and create
`releases/vMAJOR.MINOR.PATCH.json` and reviewed `releases/vMAJOR.MINOR.PATCH.md`.
The specification names every supported predecessor in
`supported_from`, configuration changes, database requirements, and rollback
steps. Preserve applied migration files; add forward migrations. Before listing
a predecessor, test upgrading a restored database from that exact version,
including account login, existing agent authentication, retained messages,
new writes, maintenance, and recovery. Fresh-install proof alone is insufficient.
If there are no tested predecessors, leave the list empty and document why.

The reviewed notes are the starting point for a user who only knows that a
release exists. Give direct links to installation and server update instructions
at that release's Git tag. State the separate team action: download the CLI from
the upgraded instance and repeat agent or admin authentication. List new
commands or changed request fields that require the updated client. The CLI is
a standalone script installed as `startunnel`. Users upgrade an installed copy
with `startunnel update`; v0.1.0 users first download and install it. Ordinary
commands do not check for updates. There is no package-manager release.

## Prepare and validate on main

1. Work on `main`. Update `VERSION`, the release specification, and the reviewed
   notes. Keep upgrade and recovery evidence for every supported predecessor.
2. Run the [local gates](testing.md#local-gates), then commit and push. Wait for
   CI to pass on that exact commit. CI checks source secrets, lock files, Bandit,
   and npm dependencies before service builds. It also checks documentation
   structure, PostgreSQL, agents, examples, browsers, and the standalone CLI on
   Linux, macOS, and Windows with Python 3.10 and 3.15. Main CI checks restored
   databases from the synthetic baseline and the published v0.1.0, v0.2.0, v0.3.0, and v0.4.0 images.
3. Select **Run workflow** for `release-candidate.yml` on `main`, or run:

   ```sh
   gh workflow run release-candidate.yml --ref main
   ```

   Preparation reuses successful source CI for the same commit. If a validated
   candidate already has retained evidence, it checks those reports and skips
   the build and image tests. No successful source CI means preparation stops;
   wait for CI or rerun `ci.yml` first.
4. A new candidate builds one Linux amd64 `runtime` image under a unique GHCR
   candidate tag. The build reuses cached layers. Version and source labels are
   also embedded in the downloadable client for `startunnel version`.
   Image scanning, the system proof, and fixed-load testing then run in parallel
   against that digest. All must pass. Browser and example suites stay in source
   CI; the system proof checks runtime contents, restart behavior, and recovery.
5. The candidate workflow retains `release-candidate-RUN-ATTEMPT` for 90 days.
   It records the digest, exact commit, version, source CI run, selected proof
   artifacts, earlier attempts, helper inputs, cleanup, and evidence checksums. Only a successful manual candidate run on `main` with retained
   evidence can be promoted. Expired evidence requires a new candidate run.

A fresh candidate includes a fixed five-minute load test plus setup. Prepare it
before tagging. Tag promotion reuses completed validation and the tested image.

Candidate validation also runs through the manual release-validation workflow.
Without a candidate argument, that manual workflow builds its own diagnostic
images; those results do not produce a promotable CI artifact. Live-agent checks
remain opt-in and do not receive provider credentials during candidate validation.

## Inspect time and retry a failed proof

Use the read-only status command for the current source:

```sh
pdm run release-status --run-id RUN_ID
pdm run release-status --run-id RUN_ID --json
```

The report shows job results, run attempts, artifact IDs, elapsed time, and the
longest observed dependency path. Queue time is shown only when Actions supplies
its boundary. The candidate summary also includes source CI, the immutable
image, helper cache results, and proof phase times. Parallel jobs are not added
as serial work. Missing counters and timestamps remain unavailable.
Promotion timing records the dispatch source separately from the tagged release
source. Source CI, candidate identity, and proof reuse still require the exact
tagged source commit.

Proof artifacts include their run and attempt. A failed report is retained when
a later attempt passes. Selection uses explicit manifests and file checksums.
Source, image digest, platform, proof inputs, profiles, load settings, helper
inputs, and cleanup must agree before reuse. Artifact lookup errors stop the
operation. Expired or incomplete evidence cannot pass promotion.

One reviewed diagnostic retry is permitted for unchanged inputs. Check the
status and preserve the failed report, then rerun only failed jobs:

```sh
gh run rerun RUN_ID --failed
```

The workflow permits two attempts. It does not retry a proof automatically.
A source change requires source CI and a new candidate for that commit. A
passing retry does not identify the cause of the earlier failure.

The manual `release-diagnostics.yml` workflow can compare cold and warm helper
builds or run a controlled database-lock failure. Select the exact candidate
digest for the lock test. These reports cannot promote a release. The tests use
disposable resources and keep the standard load and worker budgets.

## Tag and promote

Check that the release tag is unused, then tag the exact successful commit.
For example, after preparing v0.2.1:

```sh
git tag -a v0.2.1 TESTED_COMMIT -m "StarTunnel v0.2.1"
git push origin v0.2.1
```

Use the prepared version and replace `TESTED_COMMIT` with its full SHA. The
release workflow checks the tag against `VERSION`, finds retained evidence from
a successful `release-candidate.yml` run on `main` for that exact commit, and
downloads its selected `release-candidate-RUN-ATTEMPT` artifact.
It checks report status, candidate identity, runtime image IDs, cleanup, and
retained evidence checksums before promotion.

The workflow assigns the version tag to the tested registry digest without
building or testing another image. It checks that the new registry tag resolves
to exactly the same digest. Production continues to use the digest reference.
There is no mutable `latest` tag.

[Release Notes Scribe](https://github.com/btfranklin/release-notes-scribe) can
generate a change summary from source history. Set the repository Actions secret
`OPENAI_API_KEY` to enable it. The key is passed only to that action step. If the
key is missing, the action fails, or it returns no text, the draft uses the
reviewed notes and validation run link. If it returns a summary, the workflow
adds it before the reviewed notes. The notes include configuration, database,
and rollback instructions from `releases/VERSION.md`, plus the exact source,
image digest, and validation run link. A generated summary does not establish
migration compatibility.

The workflow creates a **draft** GitHub release with `release.json`. Use manual
dispatch with the existing tag to retry or recover a draft. Recovery uses the
retained exact-commit CI evidence and does not rebuild. An existing draft must
keep the same source commit and image digest. Published releases are never
modified. If the retained candidate artifact has expired, promotion fails;
prepare new successful evidence for that commit before retrying. Keep candidate images
in the registry until promotion completes.

The official workflow writes a Linux amd64 record for its tested GHCR image.
A custom deployment can write a record for another digest-pinned registry image
and Linux platform from the matching source checkout. The upgrade helper uses
the selected Compose project, files, profiles, and config file. It still checks
the record, source, backup, installed image, candidate image, and database
compatibility before it changes services.

## Publish and verify

Review the draft notes, candidate evidence, image labels, and deployment record.
Perform an installation/upgrade smoke check using the exact digest. Publish the
draft after those checks pass, then verify pulling the public image and repeat
external deployment verification. Record the version, source, digest, and results.

The first GHCR package must be made **public** in package settings. Verify an
anonymous digest pull before publishing the first release. GitHub creates
packages private by default; the workflows do not change package visibility.

Repository Actions must allow package and release writes. `GITHUB_TOKEN`
provides registry access; no personal token is needed. Package-write permission
is limited to the candidate build and promotion jobs. Provider credentials
never enter the image or validation jobs. Keep old images and deployment records
available for recovery. A failed draft run may leave a version tag in the registry;
inspect its digest and retry the draft rather than rebuilding an image.

## Install, upgrade, and recover

Use [the image guide](setup/image.md), [installation](setup/install.md), and
[maintenance](setup/maintain.md#update-the-application). The upgrade helper
previews by default; applying requires a fresh verified off-server backup.
It checks compatibility before stopping writers and keeps the previous private
configuration. It does not regenerate credentials or remove database volumes.

A compatible rollback can restore the old checkout and image. After schema or
data changes, follow the release-specific instructions; recovering the matching
database backup and secrets may be required. Do not infer reverse migration
support from a forward migration. Use [backup recovery](setup/backups.md).
