# Release policy

StarTunnel ships versioned GitHub releases and official container images.
`main` contains development work. A source push does not update any installation.
Operators explicitly select a published version and deploy its immutable digest.
Automatic updates are not enabled.

The current release specification is v0.2.0. Use the published release's
`release.json` and tagged guides for installation or upgrades. v0.1.0 keeps its
own [tagged guides](https://github.com/btfranklin/startunnel/blob/v0.1.0/README.md)
and deployment record. Never use another version's record to deploy a change.

## Versions and supported databases

Use `vMAJOR.MINOR.PATCH` tags. Patch releases contain compatible fixes; minor
releases add compatible functionality. Breaking API, configuration, or database
changes need an explicit compatibility decision and migration instructions,
including during the initial `0.x` series. Never move a published tag or replace
a published version's deployment record. Fix a released defect in a new version.

The first version is `v0.1.0`. Its initial migration graph is the supported
baseline. Unversioned instances can reach it only when their source is an
ancestor and their applied migration files match an unchanged release migration prefix.
The admin migrations and the [one-time tunnel reset](setup/address-storage.md)
require restored-database upgrade and recovery proof for the exact installed
source before compatibility is claimed. Inspect the actual installed revision before promising compatibility. Older graphs require a
separately reviewed conversion tested on a restored database.

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

1. Work directly on `main`. Update `VERSION`, the release specification, and
   the reviewed deployment notes. Record actual upgrade and recovery evidence
   for every supported predecessor; do not infer database compatibility from
   fresh-install tests.
2. Run the repository gates and `pdm run docs-structure`, then commit and push.
   CI checks Markdown structure with the checksummed Perfect Doc native release.
   The existing route, example, template, and browser checks remain in place.
   Perfect Doc does not grade prose or execute documented commands.
3. Normal pushes check source secrets, lock files, Bandit, and npm dependencies
   before service builds. They then run source, PostgreSQL, agent, example, and
   browser checks.
   They also run the standalone CLI on Linux and Windows with Python 3.10 and
   3.14, without project dependencies.
   When the commit is ready for release, select **Run workflow** for `ci.yml`
   on `main`, or run `gh workflow run ci.yml --ref main`. This manual run checks
   the selected commit and builds one Linux amd64 `runtime` image under a
   unique candidate tag in `ghcr.io/btfranklin/startunnel`. Its source revision
   and planned version labels are set at this build.
   The same values are embedded in the downloadable client and are available
   through `startunnel version`. Do not give the client a separate release version.
4. Security scanning, the isolated clean-stack system proof, and fixed-load
   testing run in parallel against the candidate digest. Application services run the candidate
   image with development settings and isolated test data. Test-helper images
   are separate builds; they are not the release artifact. The runners verify
   that web, migration, and maintenance services used the candidate image.
5. CI retains `release-candidate` for 90 days. This artifact contains the image
   digest, exact source commit, planned version, safe validation reports, and
   evidence checksums. Only a complete successful manual run on `main` is eligible
   for promotion. A candidate from a failed or incomplete run is not a release.

Candidate validation also runs through the manual release-validation workflow.
Without a candidate argument, that manual workflow builds its own diagnostic
images; those results do not produce a promotable CI artifact. Live-agent checks
remain opt-in and do not receive provider credentials during candidate validation.

## Tag and promote

Check that the release tag is unused, then tag the exact successful commit.
For v0.2.0:

```sh
git tag -a v0.2.0 TESTED_COMMIT -m "StarTunnel v0.2.0"
git push origin v0.2.0
```

Replace `TESTED_COMMIT` with its full SHA. The release workflow checks the tag
against `VERSION`, finds a completed successful manual `ci.yml` run on `main` for
that exact source commit, and downloads its retained `release-candidate`.
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
modified. If the retained CI artifact has expired, promotion fails; prepare
new successful evidence for that commit before retrying. Keep candidate images
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
