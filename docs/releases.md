# Release policy

StarTunnel ships versioned GitHub releases and official container images.
`main` contains development work. A source push does not update any installation.
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

The root `VERSION` file owns the next release version. For each subsequent
release, update it and commit `releases/VERSION.json` and `releases/VERSION.md`. The specification names every supported predecessor in
`supported_from`, configuration changes, database requirements, and rollback
steps. Preserve applied migration files; add forward migrations. Before listing
a predecessor, test upgrading a restored database from that exact version,
including account login, existing agent authentication, retained messages,
new writes, maintenance, and recovery. Fresh-install proof alone is insufficient.
If there are no tested predecessors, leave the list empty and document why.

## Prepare and validate on main

1. Work directly on `main`. Update `VERSION`, the release specification, and
   the reviewed deployment notes. Record actual upgrade and recovery evidence
   for every supported predecessor; do not infer database compatibility from
   fresh-install tests.
2. Run the repository gates and `pdm run docs-structure`, then commit and push.
   CI checks Markdown structure with the checksummed Perfect Doc native release.
   The existing route, example, template, and browser checks remain in place.
   Perfect Doc does not grade prose or execute documented commands.
3. Wait for the complete `ci.yml` push run on that exact commit. After the normal
   CI jobs pass, it builds one Linux amd64 `runtime` image and stores it under a
   unique candidate tag in `ghcr.io/btfranklin/startunnel`. Its source revision
   and planned version labels are set at this build.
4. The candidate digest passes security scanning, the isolated clean-stack
   system proof, and fixed-load testing. Application services run the candidate
   image with development settings and isolated test data. Test-helper images
   are separate builds; they are not the release artifact. The runners verify
   that web, migration, and maintenance services used the candidate image.
5. CI retains `release-candidate` for 14 days. This artifact contains the image
   digest, exact source commit, planned version, safe validation reports, and
   evidence checksums. Only a complete successful push run on `main` is eligible
   for promotion. A candidate from a failed or incomplete run is not a release.

Candidate validation also runs through the manual release-validation workflow.
Without a candidate argument, that manual workflow builds its own diagnostic
images; those results do not produce a promotable CI artifact. Live-agent checks
remain opt-in and do not receive provider credentials during candidate validation.

## Tag and promote

Check that the version tag is unused, then tag the exact successful commit:

```sh
git tag -a v0.1.0 TESTED_COMMIT -m "StarTunnel v0.1.0"
git push origin v0.1.0
```

Replace `TESTED_COMMIT` with its full SHA. The release workflow checks the tag
against `VERSION`, finds a completed successful `ci.yml` push run on `main` for
that exact source commit, and downloads its retained `release-candidate`.
It checks report status, candidate identity, runtime image IDs, cleanup, and
retained evidence checksums before promotion.

The workflow assigns the version tag to the tested registry digest without
building or testing another image. It checks that the new registry tag resolves
to exactly the same digest. Production continues to use the digest reference.
There is no mutable `latest` tag.

[Release Notes Scribe](https://github.com/btfranklin/release-notes-scribe) generates
the change summary from source history. Set the repository Actions secret
`OPENAI_API_KEY`; it is passed only to that action step. For the first version,
the action compares with the empty tree. Reviewed configuration, database, and
rollback instructions from `releases/VERSION.md` remain in the notes, followed
by the exact source, image digest, and validation run link. Generated summaries
do not establish migration compatibility.

The workflow creates a **draft** GitHub release with `release.json`. Use manual
dispatch with the existing tag to retry or recover a draft. Recovery uses the
retained exact-commit CI evidence and does not rebuild. An existing draft must
keep the same source commit and image digest. Published releases are never
modified. If the retained CI artifact has expired, promotion fails; prepare
new successful evidence for that commit before retrying. Keep candidate images
in the registry until promotion completes.

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
