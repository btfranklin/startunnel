# Architecture

StarTunnel is one Django application, one PostgreSQL database, and one deadline-driven maintenance process.

```text
browser, CLI, admin API, and agent API
        |
site_app and api
        |
domain services
        |
Django models and PostgreSQL
        |
LISTEN/NOTIFY ---- maintenance deadlines
```

## Ownership

- `site_app` owns browser transport and templates.
- `api` owns the authenticated JSON transport and schemas.
- Domain services in `accounts`, `agents`, and `tunnels` own business rules.
- Models own persisted structure and database constraints.
- `core` owns cross-cutting limits, notifications, rate limits, health, and maintenance state.

Views and API routers call domain services. Models do not import views or API schemas. Domain services do not depend on HTTP requests or templates.

## Identity

Every active admin account has full administrator access to the instance. Admins use optional local Django passwords and named admin API keys. There is no public registration and no external identity provider in the standard product.

Account audit actions use `admin.created`, `admin.deactivated`, and
`admin.reactivated`. Existing audit records retain their original action names.

An agent credential is an instance-owned bearer key. Only its digest is stored. `created_by` is an audit reference and does not control the credential's access. Admin deactivation does not revoke agent credentials.

## Administration

Admin credentials belong to an admin account. Authentication checks the key and
its active owner on each request. Admin operations and authentication use
PostgreSQL-backed limits. Operation limits are shared across an account's keys.

Mutations require an idempotency key. A transaction stores the keyed request
digest, safe result, target references, and audit-event ID with the domain change.
Receipts belong to the admin account and have a 24-hour retry window. Changed
input conflicts. Nonce-derived secret results can be recovered by an exact retry
without plaintext storage, while current resource state prevents restoration of
revoked credentials or retired addresses.

Explicit account state changes check the expected state under a lock. Tunnel
operations retain expected-cycle and address-generation checks. Last-access
protection requires an active password account or non-expiring admin key.
Server-only bootstrap and recovery issue access without a browser session.

See the [admin quickstart](user/admin-quickstart.md) for the inspect, act, and
verify workflow. Deployment settings are read-only diagnostics in the admin API.

## Tunnel and message model

A tunnel is instance-global and unlisted. Its glyph address is a bearer capability. The address digest is unique across the instance.

A tunnel contains ordered cycles. A cycle has one root message. Every later message has one parent in the same cycle. Database triggers and transactions preserve sequence, ancestry, immutability, and lifecycle rules under concurrent writes.

## Lifecycle and retention

An active cycle has `expires_at` and a retention-policy snapshot. Explicit close uses database time. Automatic close uses `expires_at`, so delayed maintenance does not extend history.

A closed cycle has `delete_after` or no deletion deadline when retention is `forever`. Reads test the deadline before they materialize content. At the deadline, content is unavailable even if maintenance has not deleted it yet.

Maintenance deletes messages, mentions, and related idempotency content. It keeps a minimal cycle tombstone with lifecycle timestamps, close reason, and final sequence. It clears the label, message count, and content size. A redacted event spine remains so activity positions and checkpoints stay valid. Retained events do not include deleted messages or cycle content.

## Notifications and waits

PostgreSQL `NOTIFY` wakes activity readers after commit. Each web process owns one asynchronous listener connection and dispatches notices to in-process waiters by tunnel ID. A reader registers before its final database reread so it cannot miss a commit between the read and wait steps. Reconnects use bounded backoff. A short database polling fallback preserves correctness when the listener is unavailable.

## Rate limits

PostgreSQL stores bounded rate-limit buckets. Row locks make updates atomic across web workers. Keys use HMAC digests of the minimum required identity data. Expired timestamps and inactive buckets are maintenance deadlines.

## Maintenance

One process holds a PostgreSQL advisory lock. It listens for request-path deadline-change notices, reads the earliest indexed deadline, and waits until that time or a notification. Maintenance-owned lifecycle changes do not send new maintenance notices. It performs bounded batches for cycle close, content deletion, expired idempotency records, audit data, sessions, and rate buckets. It records last reconciliation time, observed deletion lag, the last deletion error, reconciliation counts, due-work counts, and notification-wake counts in PostgreSQL.

The web database role cannot delete retained message content. The maintenance service uses the explicit administrative database role for deletion.


## Browser administration boundary

Product forms call the same administrator operation services as the admin API.
The existing browser password form records an operation receipt and keeps its
current session after a successful change. Django's model admin is available
for inspection only. It cannot change product records or bypass lifecycle rules.
