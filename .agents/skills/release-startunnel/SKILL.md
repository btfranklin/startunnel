---
name: release-startunnel
description: Prepare, inspect, validate, or publish a StarTunnel release with the repository's tested commands and exact source and image evidence. Use for maintaining the StarTunnel release process too.
---

# Release StarTunnel

Read the [release policy](../../../docs/releases.md) for acceptance, compatibility,
retries, and publication. Use the [tooling map](../../../docs/release-tooling.md)
for command owners, callers, tests, and type boundaries. Keep those rules in
these owning guides; this skill gives the execution order.

## Establish the requested work

Distinguish maintenance, inspection, candidate preparation, and publication.
Maintenance does not authorize a release. An explicit release request authorizes
the documented publication steps after their checks pass. Respect a later stop
or abandonment instruction. Work on `main` as required by `AGENTS.md`.

Inspect Git state, `VERSION`, reviewed notes, the release specification, and
`pdm run release-status`. Use checked source, run, image, and attempt identities.
The readiness report gives technical blockers, not publication authorization.
A local unpushed commit still needs exact-source hosted CI before a candidate.

## Use maintained tools

Keep reusable release logic in tested, strictly typed repository modules. Do not
write executable release logic in temporary directories, generated output,
workflow heredocs, or chat commands. Use the same tools locally and in CI.
Test complete orchestration and failure paths before a real stack run.

Preview local cache removal with `pdm run release-cleanup`. Apply it only within
the requested cleanup scope. Keep failed reports and unknown files. The isolated
proof runner owns disposable containers and volumes.

## Follow the release sequence

1. Prepare the version, reviewed notes, compatibility specification, and current
   guides. Use the policy's restored-database proofs before listing predecessors.
2. Run the applicable [local gates](../../../docs/testing.md#local-gates), including
   focused tests, `pdm run check`, `pdm run docs-structure`,
   `pdm run security-release source`, and `actionlint`. Source security is separate
   from the aggregate check.
3. Commit and push only within the owner's requested scope. Wait for successful
   source CI on that exact commit. Use `pdm run release-status` to check blockers.
4. Run `release-candidate.yml` on `main`. Keep one immutable application image.
   Use the existing proof selection and checksum checks; do not rebuild for promotion.
5. Inspect readiness and retained evidence. An image identity or passing workload
   alone is insufficient. Required observations, proofs, provenance, and cleanup
   must pass for the same source and inputs.
6. While release authorization remains active, follow the policy to tag the exact
   tested commit, promote its digest, review the workflow-owned draft, and publish.
   Use the maintained production proof and external checks required by the policy.
7. Report the version, release URL, source, digest, evidence, limits, and cleanup.
   A tag, draft, or green CI run alone is not a published release.

## Handle failures and improve the process

Before a fix, tell the owner which check failed and what the retained evidence
shows. Separate traffic, observations, identity, and cleanup. Keep unknown causes
unknown. Preserve failed reports and acceptance limits. Use only the policy's
bounded manual repeat; do not add automatic retry loops.

Add a regression test for a tooling defect before another real run. Update the
owning command and guide, then the map or skill if its instructions change.
Remove obsolete callers and keep one supported path for each proof.
