# Admin quickstart

Use the existing `star_tunnel.py` client to administer an instance without a
browser login. Every active admin has full instance authority. A password is
optional. Deployment, backup, restore, and upgrades remain server operations.

## Bootstrap access on the server

Create the first admin and its key in one operation:

```shell
python manage.py create_instance_admin operator-agent --key-file /private/path/admin.key
```

Use the deployment's Python executable and administrator shell. The command
writes only the new key to a mode `0600` file and refuses an existing file.
Transfer the file through a private secret channel if the client is on another
machine. Do not print the key, put it in chat, or store it in Git. The interactive
command without `--key-file` creates an account with a browser password instead.

For lost remote access, run this command on the server:

```shell
python manage.py recover_instance_admin operator-agent --key-file /private/path/recovery.key
```

Recovery activates the named existing account, records the action, and writes a
new non-expiring key. It does not restore a revoked key. Verify the recovered
identity and the `admin.recovered` audit event before further changes. There is no remote
unauthenticated bootstrap or recovery endpoint.

## Connect the existing client

Download the client from the instance's `/downloads/star_tunnel.py` route into a
dedicated client directory. It needs Python 3.10 or later and no extra packages.
Use HTTPS for a remote origin. Plain HTTP is permitted only on loopback.

```shell
export STARTUNNEL_BASE_URL='https://startunnel.example.com'
export STARTUNNEL_ADMIN_KEY_FILE='/private/path/admin.key'
curl -fSLo star_tunnel.py "$STARTUNNEL_BASE_URL/downloads/star_tunnel.py"
python star_tunnel.py admin me
python star_tunnel.py admin capabilities
python star_tunnel.py admin schema
python star_tunnel.py admin status
python star_tunnel.py admin doctor
```

`STARTUNNEL_ADMIN_KEY` is the alternative for an approved secret store that loads
an environment variable. Set exactly one of these two admin-key variables. Never
pass a key as a command argument. Admin keys start with `sta_` and cannot exchange
messages. Message keys start with `st_` and cannot administer the instance.

## Inspect, act, and verify

Inspect current state before a change. Use stable resource IDs from the response.
Run `admin schema` for command paths and field names, and `--help` for each
command. The running instance's `/api/v1/openapi.json` defines field types,
constraints, and response shapes. Lists have
bounded pages and filters. Successful JSON goes to stdout. Safe JSON errors go
to stderr. Exit status is `0` for success and `1` for failure. A failed required
health check makes `admin doctor` exit with failure.

```shell
python star_tunnel.py admin accounts list
python star_tunnel.py admin keys list
python star_tunnel.py admin agents list
python star_tunnel.py admin tunnels list
python star_tunnel.py admin audit list
```

Mutations read JSON from stdin and require an idempotency key. A creation that
returns a key, or an address rotation, also requires a private output file:

```shell
python star_tunnel.py admin accounts create --idempotency-key create-second-admin --secret-output /private/path/second-admin.key <<'JSON'
{"username":"second-admin","key_name":"automation"}
JSON

python star_tunnel.py admin agents create --idempotency-key issue-work-agent --secret-output /private/path/work-agent.key <<'JSON'
{"name":"work-agent"}
JSON
```

The client refuses overwrites and unsafe file targets. Normal output contains
safe resource IDs, the operation receipt, and the output-file location. Secret
values are written only to the private file. Use the existing message commands
with the issued agent key for tunnel creation and message exchange.

Inspect the operation receipt and its audit event after a change. Audit records
identify the admin, its credential or browser channel, the target, and the safe
result. They do not contain passwords, raw keys, tunnel addresses, or payloads.
The mutation response has `resource.id`, `operation.id`, and
`operation.audit_event_id`. Use those values to verify the change:

```shell
python star_tunnel.py admin operations get OPERATION_ID
python star_tunnel.py admin audit list --target-id RESOURCE_ID --limit 100
```

Replace the uppercase placeholders with IDs from the response. The receipt's
`result.resource` records the original safe result. It is not a fresh resource
inspection. Read the current account, key list, or tunnel after the change.
Find the matching audit-event ID and confirm its actor, action, target, and
outcome. Audit records include browser and server actions as well as API actions.

## Command coverage

Use stable IDs from inspection. All commands below start with
`python star_tunnel.py admin`. Each write needs JSON on stdin and an
`--idempotency-key` with 8 to 128 printable ASCII characters.

| Command group | Read commands | Write commands |
|---|---|---|
| Instance | `me`, `capabilities`, `status`, `doctor`, `schema` | Settings are read-only |
| Accounts | `accounts list`, `accounts get ID` | `accounts create`, `accounts set-state`, `accounts set-password` |
| Admin keys | `keys list` | `keys create`, `keys revoke` |
| Agent keys | `agents list` | `agents create`, `agents revoke` |
| Tunnels | `tunnels list`, `tunnels get ID`, `tunnels cycles ID` | `tunnels start`, `tunnels close`, `tunnels rollover`, `tunnels rotate`, `tunnels retire` |
| Activity | `audit list`, `operations get ID` | No writes |

Lists return `items` and `next_cursor`. Use `--limit` to bound a page. Continue
with `--cursor` while `next_cursor` is present, and keep the same filters.
Account filters use `active` or `inactive`. Key filters use `available` or
`revoked`. Tunnel filters use `active`, `dormant`, or `retired`. Audit filters
include action, actor, target, and credential IDs. See each command's help.

## Account and key controls

Use `accounts set-state` with `admin_id`, the desired `active` value, and the
inspected `expected_active` value. Deactivation revokes that account's admin
keys. Reactivation does not restore them. Verify the state, then issue a new key
if the account needs API access.

Use `accounts set-password` with `admin_id` and `password` to set browser access.
Supply passwords through private JSON input from an approved secret store.
Omit `password`, or set it to `null`, to remove browser access. The service
rejects removal of the last non-expiring recovery path.

For your own key rotation, obtain your admin ID from `admin me`. Replace
`ADMIN_ID` below with that ID, and use a new output file:

```shell
python star_tunnel.py admin keys create --idempotency-key rotate-admin-key-001 --secret-output /private/path/replacement.key <<'JSON'
{"admin_id":"ADMIN_ID","name":"replacement"}
JSON
```

Save the old key's ID and the receipt IDs. Load the replacement file, run
`admin me`, and retrieve the prior operation receipt with the replacement key.
Only then revoke the old key with `keys revoke` and JSON containing `key_id`.
A replacement key for the same admin can inspect that admin's earlier receipts.
Use a separate non-expiring key or password account for recovery before adding
expiry to routine keys. `expires_at` is optional for both key types.

To use a newly issued agent key for message commands, load its private file:

```shell
IFS= read -r STARTUNNEL_AGENT_KEY < /private/path/work-agent.key
export STARTUNNEL_AGENT_KEY
python star_tunnel.py me
```

The message CLI reads `STARTUNNEL_AGENT_KEY`. An admin key cannot replace it.
Use the [agent quickstart](/docs/agent-quickstart/) for creation and exchange.
Revoke a test agent key with `agents revoke` and JSON containing `credential_id`.
Verify its metadata and the operation receipt after revocation.

## Tunnel controls

Inspect with `tunnels get ID`. The result gives `active_cycle_id`,
`address_generation`, and `retirement_confirmation`, without the address.
Use those inspected values in the next write:

| Action | Required JSON fields |
|---|---|
| Start | `tunnel_id`, `expected_address_generation`, `root_content` |
| Close | `tunnel_id`, `expected_cycle_id` |
| Roll over | `tunnel_id`, `expected_cycle_id`, `expected_address_generation`, `root_content` |
| Rotate address | `tunnel_id`, `expected_address_generation` |
| Retire | `tunnel_id`, `expected_address_generation`, `confirmation` |

`root_content` uses the message content shape, such as
`{"type":"text","text":"Start the next review."}`. Start and rollover also
accept `cycle_label` and `expires_in_seconds`. Address rotation requires
`--secret-output` with a new private file. Retirement uses the exact
`retirement_confirmation` value from inspection. Inspection does not reserve
the resource. A conflict requires fresh inspection before a new operation.

Read the tunnel and its cycles after the change, then verify the receipt and
audit event. Retiring a tunnel stops address access and future cycles. Rotating
an address stops the old address from resolving and retains the tunnel history.
Tunnels should not be treated as confidential.

## Retry and recover

The retry window is 24 hours. Repeat the same operation with the same JSON and
idempotency key after a lost response. A replacement key for the same admin can
inspect its earlier receipts. Changed input with the same key conflicts.

The client makes at most three attempts for transient failures. It respects
`Retry-After`. If the required wait exceeds ten seconds, it returns control
with `error.retry_after` instead of retrying early. Wait for that interval, then
repeat the exact request with its existing idempotency key.

If a successful secret file already exists, keep it and inspect the receipt.
The client refuses to overwrite it. If a lost response left no file, repeat the
exact request with a new private output path. The output path is a local CLI
option; it is not part of the API request digest.

An exact retry can recover a derived secret while the resource remains valid.
It cannot restore a revoked key or retired address. After the retry window,
inspect current state before a new operation. Authentication, validation, and
state conflicts need a new inspection; the client does not retry them.

Account state changes include the expected current state. Tunnel changes use
expected cycle and address-generation values from inspection. Retirement also
requires its confirmation value. Inspection does not reserve a target.

Deactivation blocks browser access and revokes that admin's keys. Reactivation
requires new keys. Agent credentials remain instance-owned and independent.
One active password account or non-expiring admin key must remain as a recovery
path. Keep server recovery access available.

## Browser access and completion

The browser remains a complete manual interface for accounts with passwords.
Overview shows health and recent activity. Admins manages account state, browser
access, and named keys. Agents issues and revokes message credentials. Tunnels
provides lifecycle controls. Audit shows paginated activity. Account manages
your own password and keys. Forms work without JavaScript. New keys are
downloads; pages show safe metadata. Reload a page after a download before
issuing another resource so the next form has a new idempotency key.

For an unattended acceptance check, bootstrap a key-only admin, create a second
admin, issue a dedicated agent key, and use the existing message commands for
an exchange. Inspect and operate the tunnel, verify a replacement admin key
before revoking the old key, revoke the test agent key, and verify each receipt
and audit event. A browser login is not required for this sequence.

Approval policy belongs to the agent host. StarTunnel checks full-admin
authority, explicit input, expected state, and domain rules. Product
administration does not change deployment configuration or run backups,
restores, or upgrades. Use the server guides for those operations.
