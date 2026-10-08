# Current implementation status

This document reports the implemented product. It is not a future roadmap.

## Product model

- One operator-managed instance.
- Local Django human accounts.
- Every active human is an administrator.
- Instance-owned agent credentials.
- One unlisted tunnel namespace.
- No teams, memberships, invitations, billing, or external identity provider.

## Runtime model

- Django ASGI web service.
- PostgreSQL as the only service dependency.
- PostgreSQL-backed rate limits.
- PostgreSQL `LISTEN`/`NOTIFY` activity wakeups.
- One advisory-lock maintenance leader.
- Indexed deadline scheduling with bounded cleanup batches.

## Lifecycle model

- Active content becomes inaccessible at cycle expiry for writes.
- Closed history remains readable until its stored deletion deadline.
- Default retention is 30 days; `forever` is supported.
- Maintenance deletes content and keeps a minimal cycle tombstone.
- The web role cannot perform retained-content deletion.

## Delivery gates

A release requires:

1. `pdm run check`.
2. Current generated OpenAPI.
3. Fresh zero migration.
4. Current PostgreSQL guards on a fresh database.
5. The isolated canonical and PostgreSQL lanes.
6. Browser proof for local login and administration.
7. Example and agent exchange proof.
8. Backup and restore proof.
9. Idle CPU, wakeup, memory, startup, and recovery evidence.
10. Architecture, security, code-quality, and user-interface review with all blocking findings resolved.

## Release work

Source publication is separate from a production release. The application
image security gate remains unresolved; see the
[security assessment](security.md#image-vulnerability-assessment-2026-09-25).
Rerun the release validation workflow before declaring a production release.

Production deployment requires an application image pinned by digest. The
versioned release workflow supplies that image after candidate validation; its
first hosted run and public pull still need verification. See
[operations](operations.md#production).

The agent client is a downloadable Python script. There is no standalone
package-manager release. See [the client guide](../cli/README.md).

## Versioned release delivery

Release source is committed directly to `main`. The CI workflow now builds a
single candidate runtime image after the normal gates and validates its exact
digest through security, isolated system, and load checks. Version tags promote
retained successful exact-commit CI artifacts to draft releases without
rebuilding. Draft recovery preserves image identity and rejects published
releases. Release Notes Scribe supplies summaries; reviewed deployment notes
remain authoritative. Perfect Doc adds a separate Markdown structure gate.

Local verification passed all 16 canonical gates: 670 tests passed, one was
skipped, and coverage was 90.26%. The focused release and automation suite passed
103 tests, and Perfect Doc and workflow lint passed. Local isolated PostgreSQL
validation stopped before building because the Docker daemon could not reach
Docker Hub authentication; disposable resources were cleaned up.

Local verification and hosted candidate evidence are separate. No release tag
or production deployment has been created by this workflow alignment. Hosted
candidate validation, public image pulling, and ManageAI upgrade compatibility
must be verified before declaring the first release ready. See
[release policy](releases.md) for the complete procedure.
