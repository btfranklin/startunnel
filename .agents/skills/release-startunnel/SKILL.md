---
name: release-startunnel
description: Prepare, inspect, validate, or publish a StarTunnel release with the repository's tested commands and exact source and image evidence. Use for maintaining the StarTunnel release process too.
---

# Release StarTunnel

Use this skill in the StarTunnel repository. Read the
[release policy](../../../docs/releases.md) and
[tooling map](../../../docs/release-tooling.md) before selecting commands.
The policy owns compatibility, acceptance limits, retries, and publication.
The map owns tool locations, callers, tests, and type-check boundaries.

## Establish the requested work

Distinguish process maintenance, inspection, candidate preparation, and release
publication. A request to improve this skill or a proof helper does not authorize
a release. An explicit request to release authorizes the documented publication
steps after their checks pass. Respect a later stop or abandonment instruction.

Inspect `git status --short --branch`, `VERSION`, the release specification and
notes, recent releases, tags, and Actions runs. Record the exact source commit,
source CI run, candidate run and attempt, immutable image digest, and platform.
Use values from checked records. Do not infer them from a tag name or the most
recent run alone. Work and commit on `main` as required by `AGENTS.md`.

## Use maintained commands

Use the repository tools and workflow entry points. Do not write executable
release logic in temporary directories, generated output, workflow heredocs, or a chat command.
Private credentials, disposable checkout files, and retained reports can use
temporary storage. Reusable logic belongs in typed repository modules with
regression tests. Review and test a helper before its first real stack run.

Keep one maintained implementation for local and hosted checks.
Read [testing](../../../docs/testing.md) for the applicable focused gates.

## Prepare and validate

1. Prepare the version, reviewed notes, compatibility specification, and current
   guides. List a predecessor only after its restored-database upgrade and
   recovery proof passes. Keep required host Python and standalone CLI Python
   requirements separate.
2. Run the focused tests, `pdm run check`, `pdm run docs-structure`, and
   `actionlint`. Include new Python modules in the existing strict type gate.
   Test orchestration through its complete successful path and its failure
   paths. Function tests and a type check alone do not prove orchestration.
3. Commit and push. Wait for source CI on that exact commit. Do not replace a
   failed gate with a local result or a result from another source.
4. Run `release-candidate.yml` on `main`. Its checked selection can reuse
   matching evidence. Otherwise it builds one immutable runtime image and runs
   the required image proofs. Do not rebuild during promotion.
5. Inspect the read-only status command and retained attempt manifests. Verify
   checksums and all required proof results, including production installation,
   cleanup, and helper provenance. Preserve the failed reports. A source,
   version, image, platform, profile, or workload change requires new matching
   evidence.

The production proof uses the repository command, production Compose files,
an explicit target platform, private secrets, and a disposable project. Its
bootstrap command loads secrets through the production entrypoint and removes
the metrics setting only from that command process. It transfers private
container files through captured output. It must not clear live worker metrics
or depend on `docker cp` for a file on a container temporary mount.

## Handle a failed check

Before a fix, tell the owner exactly which check failed and what the retained
evidence shows. Separate application traffic, observation completeness, runtime
identity, and cleanup. A passing workload with incomplete observations is a
failed release proof. Preserve unknown causes instead of attributing a result
to runner variance.

Keep the existing load and collection limits. Controlled lock diagnostics and
partial reports cannot promote a release. Do not add automatic retry loops.
Use the policy's one bounded manual repeat only when the relevant inputs match
and the failed report is retained. Rerun only failed jobs. If another fix is
needed, test it before starting a new source validation cycle.

## Promote and publish

Proceed only while the owner's release authorization remains active. Confirm
that the tag is unused and that source, version, candidate digest, selected
proofs, checksums, and cleanup agree. Tag the exact tested commit. Let
`release.yml` promote the tested digest and create or recover its draft.

Review the actual draft and deployment record. Follow the policy's installation,
upgrade, anonymous asset, registry identity, and post-publication checks. Use
the same production proof command against the published digest. Keep each
report under a new project and attempt name. Do not replace a published tag,
release record, or image.

Report the published version and URL, source commit, immutable digest, evidence
links, verification limits, and cleanup. A prepared tag, draft, or passing CI
run alone is not a published release.

## Improve the process

When a release exposes a tooling defect, retain its safe evidence and add a
regression test to the owning repository module. Update the command, its tests,
the tooling map, and this skill when its decisions change. Keep detailed rules
in their owning guide. Remove obsolete callers when replacing internal logic.
Keep one supported execution path for each proof.
