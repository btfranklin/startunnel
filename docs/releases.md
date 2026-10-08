# Release policy

StarTunnel ships versioned GitHub releases and official container images.
`main` contains development work. A merge does not update any installation.
Operators explicitly select a published version and deploy its immutable digest.
Automatic updates are not enabled.

## Versions and supported databases

Use `vMAJOR.MINOR.PATCH` tags. Patch releases contain compatible fixes; minor
releases add compatible functionality. Breaking API, configuration, or database
changes need an explicit compatibility decision and migration instructions,
including during the initial `0.x` series. Never move a published tag or replace
a published version's deployment record. Fix a released defect in a new version.

The first version is `v0.1.0`. Its initial migration graph is the supported
baseline. Unversioned instances can reach it only when their source is an
ancestor and their migration files match exactly. Inspect ManageAI's actual
installed revision before promising compatibility. Older graphs require a
separately reviewed conversion tested on a restored database.

For each subsequent release, commit `releases/VERSION.json` and
`releases/VERSION.md`. The specification names every supported predecessor in
`supported_from`, configuration changes, database requirements, and rollback
steps. Preserve applied migration files; add forward migrations. Before listing
a predecessor, test upgrading a restored database from that exact version,
including account login, existing agent authentication, retained messages,
new writes, maintenance, and recovery. Fresh-install proof alone is insufficient.
If there are no tested predecessors, leave the list empty and document why.

## Publish a release

1. Review and merge the candidate, including its specification and release notes.
   Run the repository gates. Record upgrade and recovery evidence for each
   supported predecessor in the notes. Do not invent successful deployment proof.
2. Create the matching tag on that reviewed commit, for example
   `git tag -a v0.1.0 -m 'StarTunnel v0.1.0'`, then push it with
   `git push origin v0.1.0`. Tag creation is an intentional publication action.
3. The publication workflow validates the version, notes, and ancestry to `main`,
   runs the reusable CI gates, then security, system, and fixed-load validation.
   Provider-credit live-agent checks stay opt-in and are not needed to publish.
   Publication jobs have no provider credentials.
4. Only after those gates pass, the workflow builds and pushes the Linux amd64
   `runtime` image to `ghcr.io/btfranklin/startunnel:VERSION`, with source revision
   and version labels. It writes `release.json` containing the exact source
   commit, digest, platform, and compatibility specification, and creates a
   **draft** GitHub release with that record and the authored notes.
5. Review the draft, inspect the image labels and deployment record, and perform
   an installation/upgrade smoke check using that digest. Publish the draft when
   those results are satisfactory. The first GHCR package must be made **public**
   in package settings; verify an anonymous digest pull before publishing the
   first release. GitHub creates packages private by default, and the workflow
   does not silently change account/package visibility.

Repository Actions must allow `GITHUB_TOKEN` to write packages and releases.
The workflow grants those permissions only to its publication job. No personal
access token is needed. No mutable `latest` tag is used for production. Keep
previous images and records available for recovery. A failed run may leave a
version-tagged image without a published release; inspect it before retrying.
Existing releases are never edited by the workflow.

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
