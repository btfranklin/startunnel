# Product and architecture decisions

These decisions define the product scope and architecture.

## One operator-managed instance

One operator-managed instance serves one trusted group. StarTunnel does not
implement tenants or separate team workspaces. All tunnels, credentials, and
limits belong to the instance.

## Local human administration

Django local username and password authentication is the standard product. There is no self-sign-up. Every active human is an administrator. The last active human cannot be deactivated or deleted.

This keeps the human surface small and works without an external identity provider. An operator can add SSO at a deployment boundary later, but it is not part of the core product.

## Instance agent credentials

Agent credentials are instance-owned bearer keys. `created_by` is only audit data. Credentials do not expire by default. Human deactivation does not revoke them.

## Unlisted tunnels

All tunnels use one instance-global address space. They are not listed to agents by default. Any active agent that has an address can use the tunnel. Topic search or agent discovery is possible follow-on work, not part of the current product.

## Django and PostgreSQL

Keep Django and PostgreSQL. The target is a small or medium shared service installed with Compose. The existing domain and transaction model is defensible. A rewrite to Go, SQLite, Turso, or Lambda would add migration risk without solving the main operating problem.

## PostgreSQL as the only dependency

PostgreSQL owns durable content, lifecycle state, rate limits, wake notifications, and maintenance coordination. Removing the external cache reduces setup, health checks, secrets, backup questions, and failure modes.

## Simple retention

The default closed-history retention is 30 days. `forever` is the only alternate policy. Content is inaccessible at its stored deletion deadline. Physical deletion should normally finish within about five minutes. A minimal tombstone remains.

## Deadline-driven maintenance

Maintenance uses indexed deadlines, PostgreSQL notifications, and an advisory lock. It does not run a fixed five-second full sweep. Request paths still enforce expiry and retention boundaries.

## Minimal monitoring

The application exposes liveness, readiness, safe metrics, and durable maintenance status. Rich observability belongs to the operator's platform. The standard product does not require an external monitoring service.
