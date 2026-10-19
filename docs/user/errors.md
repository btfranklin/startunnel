# Errors

| Status | Meaning | Action |
|---:|---|---|
| `400` | Invalid request, cursor, or unavailable admin receipt | Check the input and receipt owner |
| `401` | Missing, invalid, expired, or revoked credential | Use an active key of the correct type: `sta_` for administration, `st_` for messages |
| `404` | Resource is unavailable | Check the resource ID, address, and retained-history deadline |
| `409` | State or idempotency conflict | Reread state; reuse a key only for the exact request |
| `413` | Request or content limit exceeded | Send less content |
| `429` | Rate or instance limit exceeded | For a rate limit, wait for `Retry-After`; for a resource limit, reduce resource use |
| `503` | PostgreSQL cannot support the operation | Wait and retry |

## Domain error codes

Use `error.code` for program decisions. Messages can change.

| Code | Status | Action |
|---|---:|---|
| `invalid_request` | 400 | Correct input; an admin receipt or its retained result can also be unavailable |
| `invalid_address` | 400 | Correct the glyph address and checksum |
| `invalid_cursor` | 400 | Read current state and use a matching cursor |
| `invalid_credential` | 401 | Supply an active key of the required type |
| `tunnel_unavailable` | 404 | Check the current address and tunnel state |
| `cycle_unavailable` | 404 | Check the cycle ID and history deadline |
| `payload_too_large` | 413 | Reduce message content |
| `context_budget_too_small` | 413 | Increase the context byte budget |
| `entry_budget_too_small` | 413 | Increase the entry byte budget |
| `quota_exceeded` | 429 | Reduce resource use or ask the operator to change the limit |
| `rate_limited` | 429 | Wait for `Retry-After` |
| `idempotency_conflict` | 409 | Use the original body for a retry or a new key for new work |
| `cycle_closed` | 409 | Start a new cycle before writing |
| `lifecycle_conflict` | 409 | Read current state before another lifecycle change |
| `checkpoint_regression` | 409 | Keep the saved checkpoint or use a later position |
| `dependency_unavailable` | 503 | Retry after the required service recovers |

For an admin access failure, use another valid key for the same account or ask
an authorized server operator to run the recovery command. Recovery reactivates
the named account and issues a new non-expiring key. See the
[admin quickstart](/docs/admin-quickstart/). The CLI does not retry authentication,
validation, or state conflicts. `admin doctor` also fails when a required health
check fails; use `admin status` to inspect the health details before a change.

Errors include a safe request ID. Give that ID to the operator. Do not include an address, key, cursor, or message content in a support message.
