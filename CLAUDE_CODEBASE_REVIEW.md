# StarTunnel deep codebase review

Reviewed commit: `0cb1456` on `main`, 2026-10-15.

This is a historical report. Later changes can resolve its findings.
Use the [current implementation status](docs/implementation-plan.md) for current behavior.

## Review method

The review covered the whole repository. Four specialist reviewers examined correctness and lifecycle, architecture and boundaries, tests and contracts, and security and operations. The lead reviewer checked findings 1 through 6 directly against the code. Findings 7 and later, the test gaps, and the structural risks rely on the specialists' code tracing and were not re-traced by the lead reviewer. No files were changed, and no Docker or PostgreSQL lanes were run.

The core design holds up well:

- The tree invariants hold: one root, one parent, and immutable messages.
- Locks are taken in a consistent order, and event positions are allocated without gaps.
- Last-admin lockout protection works.
- Addresses stay out of URLs and logs.
- The code mostly follows the dependency direction in `AGENTS.md`.

The defects sit at the edges: what the system retains, what it reports, and which errors reach clients.

## Findings

The findings below describe the reviewed commit.

### 1. [High] Admin operation receipts are never purged, so issued secrets can be re-derived indefinitely

- **How the secrets are made.** Admin keys, admin-created agent keys, and admin-rotated addresses come from `_derived_secret` (`src/accounts/admin_services.py:295`). It computes an HMAC of `IDEMPOTENCY_SECRET`, the receipt ID, and a stored `derivation_nonce`.
- **Nothing deletes the receipts.** No code deletes `AdminOperation` rows. `cleanup_once` (`src/tunnels/maintenance.py`) purges idempotency records, audit events, sessions, and rate buckets, but not admin receipts.
- **Backups expose them.** Backups bundle the database dump with the secret files (`docs/setup/backups.md`). Anyone with a backup can rebuild every admin key, agent key, and address that is still in use.
- **The docs promise otherwise.** This contradicts the "only peppered digests are stored" claim in `docs/security.md` and the one-time address page wording.
- **Admin passwords are exposed as well.** The same receipts store `request_digest`, a fast HMAC over request data that includes plaintext admin passwords for `accounts.create` and `accounts.password` (`src/accounts/admin_services.py:414-423`). With the database and the secret, an attacker can test password guesses at SHA-256 speed instead of Argon2 speed.
- **The table grows without bound.**
- **Fix:**
  - Purge expired receipts in maintenance, or at least null `derivation_nonce` after `expires_at`.
  - Leave passwords out of the canonical request digest.

### 2. [Medium] Agent-credential validation errors return 500 from the admin API without a failure audit

- **The error type.** `CredentialError` (`src/agents/services.py:21`) is a plain `ValueError`. It is raised for an empty name, a name over 100 characters, an inactive actor, and the instance credential limit.
- **No handler maps it.** `src/api/router.py:146-206` registers no handler for it. The middleware turns the request into `internal_error` (500).
- **No failure audit.** `execute_admin_operation` catches only `TunnelDomainError` (`src/accounts/admin_services.py:375`), so no `admin.operation_failed` audit row is written.
- **The browser behaves differently.** It catches `CredentialError` explicitly (`src/site_app/views.py:235,268`), so the two channels respond differently to the same domain failure.
- **Fix:** raise `InvalidRequest` and `QuotaExceeded` from `create_credential` and `revoke_credential`, as `issue_admin_key` already does (`src/accounts/admin_access.py:95`). Add admin API tests for an empty name and the limit.

### 3. [Medium] Operator close and retirement of an expired cycle extend its history and can re-expose content

- **No expiry check.** `close_cycle_as_operator` (`src/tunnels/services.py:1612`) and `_retire_locked_tunnel` (`src/tunnels/services.py:1805`) select the active cycle without checking `expires_at`.
- **The current setting replaces the snapshot.** For any reason other than `expired`, `_close_locked_cycle` (`src/tunnels/services.py:1484-1498`) sets `closed_at = now` and applies the current retention setting instead of the cycle's snapshot.
- **Late maintenance extends history.** If maintenance is late, the operator close resets `delete_after`. That breaks the documented rule that delayed maintenance does not extend history.
- **Hidden content can come back.** `cycle_is_readable` hides an active cycle after `expires_at + retention` (`src/tunnels/access.py:147-149`). With a short retention setting, an operator close turns that hidden cycle into a closed cycle with a future deadline, and its content becomes readable again.
- **The close reason is wrong.** It records `instance_operator` or `operator_retirement` instead of `expired`.
- **Fix:** have operator paths close an already-expired cycle with reason `expired`, so it keeps its snapshot.

### 4. [Medium] The web database role can steer retention deletion and rewrite audit records

- **Only DELETE is revoked.** `docker/postgres/runtime-grants.sql` and `src/tunnels/migrations/sql/guards.py:367-383` revoke DELETE only. The web role keeps UPDATE on `tunnels_cycle` and `tunnels_auditevent`.
- **No trigger guards the key columns.** Nothing protects `cycle.state`, `delete_after`, or `auditevent.created_at` and `metadata`.
- **How the attack works.** A compromised web container can move deadlines earlier or backdate audit rows. The maintenance worker then deletes that history.
- **Maintenance has more privilege than it needs.** The worker runs as the PostgreSQL superuser (`compose.yaml:163`, `STARTUNNEL_DATABASE_ROLE: admin`), not a role limited to retention deletion.
- **Fix:**
  - Revoke UPDATE on audit events.
  - Add a cycle lifecycle trigger that blocks moving a deadline earlier and blocks invalid state changes.
  - Give maintenance a dedicated retention role.

### 5. [Medium] The maintenance loop exits instead of backing off on database errors

- **Wrong exception type.** `src/tunnels/management/commands/maintenance_loop.py:85` catches only `psycopg.Error`. ORM work inside `_reconcile` raises `django.db.utils.*` errors, which are not psycopg subclasses.
- **Effect.** A database restart ends the process without recording `last_error` or backing off. Recovery depends on the container restart policy.
- **Fix:** also catch `django.db.DatabaseError` and record it.

### 6. [Medium-Low] The credential limit reported to admins differs from the enforced limit

- **Two sources.** Enforcement reads `settings.STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT` (`src/agents/services.py:57`). Admin status, `/admin/capabilities`, and `inspect_runtime` all report the hard-coded `Limits.active_credentials = 50` (`src/core/limits.py:11`).
- **Effect.** An operator who sets 200 sees 50, so capacity planning through the API is wrong.
- **Fix:** build `Limits` from the setting in `InstanceLimitProvider` and enforce from the same value.

### 7. [Low] Smaller defects

- **Expired idempotency key race.** Two requests reusing the same expired key at the same time can get a 500. One request deletes and recreates the row, and the other's `select_for_update().get()` raises `DoesNotExist`. The pattern is in `src/tunnels/idempotency.py:98-110` and `src/accounts/admin_services.py:424-432`.
- **`start_cycle` replay after retention deletion.** Retention deletion removes the receipt (`src/tunnels/retention.py:42`). A retry inside the 24-hour key window can then start a second cycle on a dormant tunnel.
- **Agent replays skip the address check.** Replays of `create_tunnel` and `post_reply` do not re-check that the address is current or that the tunnel is not retired (`src/tunnels/services.py:373-421,557-564`). Admin replays do check.
- **Labels past their deadline.** The admin cycle listing returns labels for cycles past `delete_after` until maintenance deletes them (`src/accounts/admin_services.py:273-279`).
- **Inconsistent retention on close.** Close by expiry uses the snapshot, while creator, rollover, and operator closes use the current setting.
- **Database time.** Lifecycle timestamps use application time, although `docs/architecture.md` says database time. With clock skew, a reply can be accepted after maintenance records `closed_at = expires_at`.
- **Miss limit checked after the lookup.** The address-miss rate limit runs after the lookup (`src/api/router.py:215`), so a correct guess still succeeds once the limit is reached. The risk is small because addresses are 128-bit random.
- **One failing deletion slows maintenance.** A single cycle whose deletion keeps failing drops maintenance to 60-second passes of 100 items. A tunnel with an expired but unclosed cycle cannot start or roll over until maintenance catches up.
- **Refreshed buckets can be deleted.** Batch deletes select IDs and then delete them without re-checking expiry (`src/tunnels/maintenance.py:136-140`). A rate bucket or session refreshed in between is still deleted.
- **Missed wakeup after reconnect.** A commit landing between the reread and LISTEN after a listener reconnect delays a long poll until timeout. Correctness is not affected (`src/core/activity_listener.py:71-93`).
- **Metrics token 500.** A non-ASCII `X-Metrics-Token` causes an unauthenticated 500 (`src/core/views.py:43-44`).
- **Agent revoke 500.** Revoking an agent with a malformed ID in the browser causes a 500 (`src/site_app/views.py:257-260`).
- **Case-only username differences.** `accounts.create` allows usernames that differ only by case (`src/accounts/admin_services.py:467-471`), while `create_instance_admin` rejects them.
- **Login limit scope.** The limit is keyed only on IP plus username, so there is no cross-username or cross-IP bound (`src/api/rate_limits.py:42-50`).
- **Rotated secret file ownership.** `scripts/rotate_postgres_passwords.py:341-360` writes secret files owned by whoever runs it, not by the documented `10001:10001`. Services may then fail to read them.
- **Restore trust.** Restore runs archive SQL as superuser, and the only integrity check is a co-located checksum manifest.
- **Diagnostic logging.** The redacting formatter drops exception detail, so operators get no diagnostics for 500s or deletion failures (`src/core/logging.py:66-78`).
- **Container hardening.**
  - Code under `/app` is owned by the app user.
  - Caddy has no `cap_drop`.
  - No service has memory or pids limits.
- **CI actions pinned by tag.** CI actions are pinned to mutable tags, while the release job has `packages: write` (`.github/workflows/release.yml`).
- **Body-size drift.** Caddy hard-codes a 128 KB body limit, but `STARTUNNEL_MAX_REQUEST_BODY_BYTES` can be raised to 1 MiB.
- **Long admin sessions.** Admin sessions use Django's two-week default with no idle timeout.

## Test gaps (not defects)

- **`pdm run test` fails without PostgreSQL.** It runs on in-memory SQLite. Three integration tests assert PostgreSQL and fail, and the other 42 skip. In the postgres lane, a bad URL would silently turn 42 proofs into skips. One conftest rule should deselect `postgres` tests when no URL is set and fail `-m postgres` on SQLite.
- **Coverage gates run only on SQLite.** The 90% coverage gate and the critical-function gate are measured on SQLite. The PostgreSQL recursive subtree query (`_postgresql_subtree_page`) is tested only on flat trees. Nothing covers `max_depth` truncation, continuation, or deep branches.
- **Maintenance takeover is untested.** The advisory-lock test calls `pg_try_advisory_lock` directly, so it proves PostgreSQL, not the code.
- **Notification tests would pass with the order reversed.** The register-before-read test would still pass if the two steps were swapped. The reconnect test never reconnects.
- **The web-role protection relies on proxies.**
  - The production grant file is never exercised.
  - The system proof and restore check count any psql failure as "denied". A deferred foreign key would also make the probe DELETE fail.
  - The protected-table list is kept by hand in three places.
- **Idempotency races are under-tested on PostgreSQL.** Concurrent changed-input conflicts and concurrent expired-key reuse are tested only on SQLite, where `select_for_update` does nothing.
- **The post-versus-close race test does not force both orders.** The race between maintenance expiry-close and a new post is not tested on PostgreSQL.
- **Retention deletion is tested on a single message.** On PostgreSQL it is only tested on a one-message cycle.
- **The address-in-URL test misses query parameters.** It checks only path segments, so a future `?address=` parameter would pass.
- **Some structure tests check text instead of behavior.** Examples in `tests/structure/test_release_automation.py`:
  - a YAML comment, including its indentation;
  - substrings in shell scripts;
  - exact job and step names.

  These raise the cost of change without proving behavior.
- **`--reuse-db` keeps old triggers.** Local PostgreSQL runs with `--reuse-db` keep triggers from old migrations after `guards.py` is edited.

## Structural future risks and likely refactors

- **`src/api/router.py` (1591 lines) duplicates the cursor protocol.**
  - There are four near-identical snapshot parsers, `_tree_cursor_values`, `_reply_cursor_values`, `_subtree_cursor_values`, and `_leaves_cursor_values`, and a 16-copy `_record_miss` block.
  - Cycle mismatches return inconsistent error codes: `InvalidRequest`, `InvalidCursor`, or `TunnelUnavailable`.
  - Consolidate these the next time a paged endpoint or the cursor format changes. Keep error codes byte-identical.
- **Limits are retyped as literals.** They appear in about six places across schemas, forms, services, and error text. Move them to constants in `src/core/limits.py` in the next change that touches a limit, together with finding 6.
- **Some rules have more than one copy, and some code is dead:**
  - The admin-key availability rule is written four times. The list filter's `state=revoked` misses expired and suspended keys.
  - The synchronous `read_activity` and `read_activity_once` have no production caller but duplicate the validation rules.
  - `src/accounts/services.py` holds dead admin functions whose audit names are the ones `docs/architecture.md:31` documents. Production records operation names such as `accounts.create`.
  - `_tunnel_metadata` and `get_tunnel` are dead or test-only.
- **Adding an admin operation touches about seven files.** These include the hand-copied field lists in `cli/star_tunnel.py` `ADMIN_COMMANDS`. A test that derives CLI fields from the generated OpenAPI would catch drift without adding a registry.
- **Some dependencies run the wrong way.** `src/accounts/forms.py` imports from `api.rate_limits`, and the API imports the private `core.rate_limits._consume`. Move the named rate-limit rules into `src/core/rate_limits.py`.
- **Admin listings issue N+1 queries.** `tunnel_resource` makes 2 queries per row on pages of up to 1000 rows, and the browser tunnels view makes 3 per row.
- **Cycle-snapshot cursors are returned but not accepted.** The cycle list returns a `cycle-snapshot` cursor that no request accepts.
- **`src/tunnels/services.py` (1890 lines) has three jobs.** It mixes tree reads, lifecycle writes, and operator functions. Defer a split until the operator surface grows again.

## Roadmap and design alignment

- `docs/architecture.md` says explicit close uses database time. The code uses `timezone.now()` everywhere except rate limits. Either fix the document or move lifecycle timestamps into SQL.
- `docs/security.md` says only peppered digests are stored. Finding 1 contradicts this.
- `docs/architecture.md:31` documents audit action names that only dead code writes.
- `src/billing/` exists locally with only `__pycache__`. It is untracked, but it still imports as an empty namespace package. Delete it locally.

## Open questions

- Should close-by-expiry keep the original retention snapshot after a policy change? Explicit closes use the current setting today.
- Is it intended that cursors returned on an idempotent replay are already expired when the replay comes more than an hour after creation?
- Can any `uvicorn.error` message carry request content? Uvicorn's own loggers bypass the redacting formatter.

## Recommended follow-up order

1. Purge admin receipts and remove passwords from the request digest (finding 1).
2. Map credential errors to the API error contract and record failure audits (finding 2).
3. Make operator close honor an expired cycle's snapshot (finding 3).
4. Revoke UPDATE on audit events, add a cycle lifecycle trigger, and give maintenance a dedicated role (finding 4).
5. Catch Django database errors in the maintenance loop and report the configured credential limit (findings 5 and 6).
6. Add the PostgreSQL lane guard in conftest.
