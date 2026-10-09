# Current implementation status

This document reports the implemented product. It is not a future roadmap.

## Product model

- One operator-managed instance.
- Admin accounts with optional Django passwords and named API keys.
- Existing CLI extended for product administration.
- Typed admin API and operation receipts.
- Every active admin account has full administrator access.
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

Source publication is separate from a production release. The dated image
security assessment records an earlier image. The release validation workflow
must pass for the exact candidate digest before release promotion.

Production deployment requires an application image pinned by digest. The
versioned release workflow supplies that image after candidate validation.
See [operations](operations.md#production).

The agent client is a downloadable Python script. There is no standalone
package-manager release. See [the client guide](../cli/README.md).

## Versioned release delivery

Release source is committed directly to `main`. The CI workflow now builds a
single candidate runtime image after the normal gates and validates its exact
digest through security, isolated system, and load checks. Version tags promote
retained successful exact-commit CI artifacts to draft releases without
rebuilding. Draft recovery preserves image identity and rejects published
releases. Release Notes Scribe supplies summaries; reviewed deployment notes
remain authoritative. Perfect Doc checks offline Markdown structure in the aggregate check.

Local verification passed all 17 fast gates: 727 tests passed, one was skipped,
and coverage was 90.93%. The isolated PostgreSQL lane passed 45 tests, including
concurrent admin access changes. The restored initial-baseline proof passed
with a real PostgreSQL dump and restore. Perfect Doc, generated OpenAPI, and the
website build passed.

Local verification and hosted candidate evidence are separate. The replacement
0.1.0 candidate must pass exact-commit CI and image validation before its version
tag is restored. Draft creation does not publish the release or deploy an
instance. See [release policy](releases.md) for the complete procedure.

## Agent administration evidence

The isolated browser lane passed all 19 tests in Chromium, Firefox, and WebKit.
It proved key-only bootstrap, the full unattended CLI administration sequence,
message exchange, lifecycle controls, key replacement, revocation, audit and
receipt verification, no-JavaScript key controls, and desktop and mobile layout.
The browser password form also preserved its current session after a service
operation. Screenshots contain metadata only.

Source implementation and local tests do not prove installed runtime or hosted
candidate state. Release promotion still requires the exact-commit CI candidate
and its retained image validation evidence.
