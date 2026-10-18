# StarTunnel code elegance review

Reviewed commit: `0cb1456` on `main`, 2026-10-15.

This is a historical report. Later changes can resolve its findings.
Use the [current implementation status](docs/implementation-plan.md) for current behavior.

## Review method

Four reviewers covered the codebase:

- cognitive clarity;
- Python and Django conventions;
- developer experience;
- composition and naming.

The lead reviewer checked the main claims against the code and dropped unsupported ones. No files were changed.

## First impression

The domain core is a pleasure to read:

- Tunnel, cycle, root, and address each mean one thing.
- Service functions take keyword-only arguments.
- Errors are small classes that carry their own safe message.

The cost sits at the edges. Typed domain results become `dict[str, Any]` at the API boundary. Two files are where a maintainer loses the thread: `src/tunnels/services.py` (1890 lines) and `src/api/router.py` (1591 lines).

Detailed checking confirmed this impression. It also added one theme: the same concept sometimes goes by two names.

## What is beautiful

- **Errors (`src/tunnels/errors.py`).**
  - Each class carries a `code`, an HTTP `status`, and a plain-language message that says what to do next, for example "This tunnel has no writable cycle. Start a new cycle and try again."
  - One API handler maps the whole hierarchy.
  - Missing and forbidden resources both say "not available", so an error never reveals whether something exists.
- **Small, single-purpose modules.**
  - `access.py`, `codec.py`, `retention.py`, and `idempotency.py` each do one job.
  - The idempotency protocol (`begin`, `find_replay`, `finish`) is small and easy to understand.
- **Service calls read like sentences.**
  - Keyword-only signatures and frozen, slotted, keyword-only result dataclasses make every argument visible at the call site, for example `post_reply(credential=..., idempotency_key=..., address=...)`.
- **Existing library capabilities are used where they fit.**
  - Cursors use Django's `Signer.sign_object`.
  - Search uses PostgreSQL full-text search through Django.
  - The glyph codec is custom for a real domain reason.
- **`scripts/check.py`.**
  - Each `Gate` names its owning document and its recovery command, so a failure tells the maintainer where to look and what to run. This is the best developer-experience detail in the repository.
- **Tests that read as specification.**
  - `test_create_is_atomic_and_builds_one_rooted_cycle` asserts domain facts: one root, no parent, sequence 1, depth 0, and ordered events. It explains the cycle model better than prose does.
- **Honest documentation.**
  - `docs/implementation-plan.md` says it is not a roadmap.
  - `cli/README.md` says there is no packaged release yet.
  - Each document states what it owns and what it does not own.

## What causes maintenance difficulty

The findings below describe the reviewed commit.

### 1. Typed results become untyped dictionaries at the API boundary

- `dict[str, Any]` appears 49 times in `src/api/router.py`, 26 times in `src/api/admin.py`, and 19 times in `src/accounts/admin_services.py`.
- Routes such as `create_tunnel_route` (`src/api/router.py:479`) are annotated `Status[dict[str, Any]]`, while `src/api/schemas.py` already declares matching response schemas.
- **Effect.** Adding a response field means editing a service dataclass, a hand-built dictionary, and a schema. The type checker cannot detect drift between them.

### 2. Cycle creation is written twice

- `create_tunnel` (`src/tunnels/services.py:439`) and `_start_cycle_locked` (`src/tunnels/services.py:1142`) repeat the same sequence:
  - the cycle with its retention snapshot;
  - the root message and mentions;
  - the counters;
  - the `CYCLE_STARTED` and `MESSAGE_POSTED` events;
  - the notification.
- The retention setting is read at three sites (lines 455, 1170, and 1489).
- **Effect.** A reader must compare about 60 lines to confirm the two paths agree. A change to what a new cycle contains has two locations and no signal that both must move.

### 3. Replay validation is written out twice

- `create_tunnel` (`src/tunnels/services.py:392`) and `_replayed_cycle` (`src/tunnels/services.py:1230`) repeat the same chain of `isinstance(x, int) and not isinstance(x, bool)` range checks over a stored response.
- The replay branch takes about 55 lines of a 190-line function, which buries the main flow.

### 4. "Admin" and "operator" name the same role

- The vision, the architecture document, the API, and the CLI all say "admin".
- Five public functions in `src/tunnels/services.py` say operator, while their private helpers in the same file say admin:
  - `close_cycle_as_operator`, `start_cycle_as_operator`, `rollover_cycle_as_operator`, `rotate_address_as_operator`, and `retire_tunnel_as_operator`;
  - their helpers are `_start_cycle_as_admin_locked` and `_retire_as_admin_locked`.
- **Effect.** A maintainer who searches for "admin" misses all five entry points.

### 5. Admin operations dispatch on strings over an untyped dictionary

- `_perform` (`src/accounts/admin_services.py:463`) branches on operation-name strings and probes `data.get(...)` by hand.
- The typed schemas that the API already validated are discarded.
- **Effect.** Finding what operation X does takes a text search instead of a jump to a function.
- **Example of drift.** One branch uses `data.get(...)` and another uses `int(data[...])` on the same kind of field.

### 6. `tunnels` has become the shared-concept home

- `src/tunnels/models.py` holds `AuditEvent` and `IdempotencyRecord` alongside the tunnel and message models.
- `src/core/audit.py` writes audit events through `apps.get_model`.
- `accounts` imports its errors from `tunnels.errors`, so `TunnelDomainError` is the shared domain error in practice, but its name and location do not say so.
- Maintenance is split between `src/tunnels/maintenance.py` and `src/core/maintenance_*.py`.
- Agent retries use `IdempotencyRecord`, while admin retries use `AdminOperation`. These are the same concept under different names.

### 7. Developer experience: proportion and first run

- `scripts/` holds 33 tracked files in one flat directory. They mix everyday checks, proofs, and release tooling, and there is no index.
- `pdm run test` is the documented first command. It fails or skips without PostgreSQL, and its output does not point to the isolated runner.
- The redacting log formatter lets only "Request completed." through and replaces every other message with `[REDACTED]`. The safety intent is right, but incidents become hard to read. A stable event-code field would keep the logs safe and make them readable.
- `tests/unit/` mixes infrastructure tests with domain tests. A file name such as `test_phase2_navigation_api.py` carries planning history into the test tree.

## Review-area summary

- **Cognitive clarity.**
  - Strongest in the small domain modules.
  - Weakest in `services.py`, `router.py`, and `src/tunnels/activity.py`.
  - In `activity.py`, `_read_activity_for_known_tunnel` encodes three read modes through which of its arguments are `None`. A reader must know which combination a caller uses.
- **Language and conventions.**
  - Modern and strict: mypy in strict mode with Django stubs, a broad ruff rule set, `TextChoices` states, and dataclasses with `frozen`, `slots`, and `kw_only`.
  - Five places use the Python 3.14 `except A, B:` form without parentheses. The rest of the code uses parentheses. The bare form reads like the old Python 2 binding syntax.
  - `src/api/router.py:18-21` imports cursor helpers under private-looking aliases.
  - Inner route functions such as `_create` have one caller, so the inner and outer split only adds a hop.
- **Naming and composition.**
  - Good in the domain, with drift at the boundaries.
  - A bearer key is called credential, key, or agent depending on the layer.
  - The receipt model is `AdminOperation` and also has a column named `operation`.
  - Tunnel lookup appears as `get_tunnel`, `get_tunnel_status`, and `agent_tunnel_status`.
  - `parse_key` returns nothing; it only validates.
- **Developer experience.**
  - High confidence at the surface: entry-point tables, gate recovery text, and humane error messages.
  - Heavier release machinery underneath.

## What to protect

- The error hierarchy and its "not available" convention.
- Keyword-only service signatures and frozen result dataclasses.
- The `begin` / `find_replay` / `finish` idempotency protocol.
- The revoke-versus-retire vocabulary: credentials are revoked; tunnels and addresses are retired.
- The explicit per-operation admin wrappers. A generic dispatcher table would read worse.
- POST bodies for reads, which keep addresses out of URLs.
- Gate recovery text in `scripts/check.py`, and the ownership statements in the docs.

## Guidance

1. **Return response schemas from routes, one endpoint at a time.**
   - The generated OpenAPI artifact should not change, which makes it a free check.
   - Keep plain dictionaries only where the shape is deliberately open, such as message payloads and limit tables.
2. **Build new cycles in one `_open_cycle` helper.**
   - Tunnel creation and cycle start both call it.
   - A small typed replay model replaces the duplicated type checks.
   - Tunnel quota, address allocation, and idempotency handling stay where they are. No public interface changes.
3. **Rename the five `*_as_operator` functions to `*_as_admin`.**
   - Keep "operator" for server-side work such as deploy and recovery.
   - Also have `close_cycle_as_operator` use the existing `_locked_operator_tunnel` helper instead of inlining the same lock.
4. **Delete the admin functions in `src/accounts/services.py`.**
   - `create_admin`, `set_admin_active`, and `require_active_admin` have no production caller. Only `tests/unit/test_accounts.py` uses them.
   - Deletion removes a second name for the admin state change and the `AccountError` vocabulary. Production behavior lives in `change_admin_state` and is unchanged.
5. **Small consistency fixes.**
   - Pick one `except` style.
   - Import cursor helpers as a module (`cursors.encode(...)`) instead of through private aliases.
   - Define the `Idempotency-Key` header once as an `Annotated` alias.
6. **Developer experience.**
   - Add a short change-type-to-lane table in `docs/testing.md`.
   - Add a PostgreSQL hint where integration tests fail.
   - Add a brief index or grouping for `scripts/`.
7. **Wait on the bigger moves.**
   - Split `services.py` into reads, cycles, and admin actions when the admin surface next grows.
   - Move audit and idempotency models toward `core`, and rename `AdminOperation` to `AdminReceipt`, only when a related migration is already planned. Model moves and renames are expensive to reverse.
