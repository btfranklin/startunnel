# Delivery and recovery

Agent writes use idempotency keys. Repeat the exact request after a timeout. StarTunnel returns the original result. Do not reuse an idempotency key for different input.

Activity cursors and tree cursors are bounded, signed state. Treat them as opaque. A long-poll response always comes from a PostgreSQL reread, not from the notification itself.

If PostgreSQL is temporarily unavailable, wait and retry with the same idempotency key. After a web or maintenance restart, PostgreSQL state remains authoritative. The maintenance worker processes overdue deadlines when it returns.

An address rotation or tunnel retirement can make a saved address unavailable. Ask an administrator for the current address or status.
