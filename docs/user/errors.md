# Errors

| Status | Meaning | Action |
|---:|---|---|
| `400` | Invalid request, cursor, or unavailable admin receipt | Check the input and receipt owner |
| `401` | Missing, invalid, expired, suspended, or revoked credential | Use an active key of the correct type: `sta_` for administration, `st_` for messages |
| `404` | Resource is unavailable | Check the resource ID, address, and retained-history deadline |
| `409` | State or idempotency conflict | Reread state; reuse a key only for the exact request |
| `413` | Request or content limit exceeded | Send less content |
| `429` | Rate limit exceeded | Wait for `Retry-After` |
| `503` | PostgreSQL cannot support the operation | Wait and retry |

For an admin access failure, use another valid key for the same account or ask
an authorized server operator to run the recovery command. Recovery reactivates
the named account and issues a new non-expiring key. See the
[admin quickstart](/docs/admin-quickstart/). The CLI does not retry authentication,
validation, or state conflicts. `admin doctor` also fails when a required health
check fails; use `admin status` to inspect the health details before a change.

Errors include a safe request ID. Give that ID to the operator. Do not include an address, key, cursor, or message content in a support message.
