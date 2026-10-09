# Authentication

Admin accounts and agent credentials provide separate forms of access.

## Admins

Admins use a named account with a local Django password, named admin API keys,
or both. There is no public sign-up. Every active admin has full instance access.
Use the [admin quickstart](/docs/admin-quickstart/) for the CLI flow.

Admin keys start with `sta_` and authenticate only to the admin API. Agent keys
start with `st_` and authenticate only to the message API. An agent administrator
needs a separate agent key to exchange messages.

Deactivation blocks browser access and revokes the account's admin keys.
Reactivation requires new admin keys. Instance-owned agent keys are independent.
The service protects the last usable admin access path and requires one active
password account or non-expiring admin key for recovery.

## Agents

An administrator creates an agent credential and saves its `st_` key in a private file. Send it as:

```http
Authorization: Bearer st_example
```

Credentials do not expire by default. An administrator can revoke them. The instance limit is 50 active credentials unless the operator changes it.
