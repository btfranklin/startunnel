# Authentication

Admin accounts and agent credentials provide separate forms of access.

## Admins

Admins sign in with a local Django username and password. There is no public sign-up. An administrator creates each account. Every active admin account can manage admin accounts, agent credentials, and tunnels.

An admin account can be used by a person or an agent. An agent API key does not grant administrator access. Administration requires an admin account and its sign-in credentials.

The last active admin cannot be deactivated or deleted. Deactivating an admin account prevents future sign-in but does not revoke agent credentials created with that account.

## Agents

An administrator creates an agent credential and copies its one-time `st_` key. Send it as:

```http
Authorization: Bearer st_example
```

Credentials do not expire by default. An administrator can revoke them. The instance limit is 50 active credentials unless the operator changes it.
