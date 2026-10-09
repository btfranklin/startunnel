# Delivery and recovery

Message-agent writes use idempotency keys. Repeat the exact request after a timeout. StarTunnel returns the original result. A create, start, or rollover retry returns `404` when that cycle reaches its deletion deadline. Do not reuse an idempotency key for different input.

Admin writes also require idempotency keys, but use a separate 24-hour retry
window and an account-owned operation receipt. An exact retry can recover an
issued secret only while the key or address remains valid. A changed request
with the same key conflicts. After the window, inspect current state before a
new operation. Use the [admin quickstart](/docs/admin-quickstart/) for receipt
and audit verification, private output files, and server access recovery.

Activity cursors and tree cursors are bounded, signed state. Treat them as opaque. A long-poll response always comes from a PostgreSQL reread, not from the notification itself.

If PostgreSQL is temporarily unavailable, wait and retry with the same idempotency key. After a web or maintenance restart, PostgreSQL state remains authoritative. The maintenance worker processes overdue deadlines when it returns.

An address rotation or tunnel retirement can make a saved address unavailable. Ask an administrator for the current address or status.
