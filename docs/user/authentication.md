# Authentication

## Humans

Humans sign in with a local Django username and password. There is no public sign-up. An administrator creates each account. Every active human can manage humans, agent credentials, and tunnels.

The last active human cannot be deactivated or deleted. Deactivating a human ends that person's future sign-in access but does not revoke agent credentials that the person created.

## Agents

An administrator creates an agent credential and copies its one-time `st_` key. Send it as:

```http
Authorization: Bearer st_example
```

Credentials do not expire by default. An administrator can revoke them. The instance limit is 50 active credentials unless the operator changes it.
