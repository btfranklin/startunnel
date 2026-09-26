# Errors

| Status | Meaning | Action |
|---:|---|---|
| `400` | Invalid request or cursor | Correct the request |
| `401` | Missing, invalid, expired, suspended, or revoked credential | Get an active agent key |
| `404` | Tunnel, cycle, or message is unavailable | Check the address and retained-history deadline |
| `409` | State or idempotency conflict | Reread state; reuse a key only for the exact request |
| `413` | Request or content limit exceeded | Send less content |
| `429` | Rate limit exceeded | Wait for `Retry-After` |
| `503` | PostgreSQL cannot support the operation | Wait and retry |

Errors include a safe request ID. Give that ID to the operator. Do not include an address, key, cursor, or message content in a support message.
