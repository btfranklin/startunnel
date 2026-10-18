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
identity and the `admin.recovered` audit event before further changes.
Bootstrap and recovery require server access.

## Connect the existing client

Download the client from the instance's `/downloads/star_tunnel.py` route into a
dedicated client directory. It needs Python 3.10 or later and no extra packages.
The operator can select HTTP or HTTPS. Use HTTP on a trusted container, VPN,
or private network. Use HTTPS for public access.
On Windows, run the script with your Python 3.10+ executable and keep key files
in a directory that only your account can access. The `0600` file mode used on
Linux and macOS does not set Windows access-control lists.

```shell
export STARTUNNEL_BASE_URL='https://startunnel.example.com'
export STARTUNNEL_ADMIN_KEY_FILE='/private/path/admin.key'
curl -fSLo star_tunnel.py "$STARTUNNEL_BASE_URL/downloads/star_tunnel.py"
python3 star_tunnel.py install
export PATH="$HOME/.local/bin:$PATH"
startunnel admin me
startunnel admin capabilities
startunnel admin schema
startunnel admin status
startunnel admin doctor
```

After a server upgrade, run `startunnel update`, then `startunnel admin me`
and `startunnel admin doctor`. Keep your existing key. For an older script
without the installed command, use the download and installation above once.

`STARTUNNEL_ADMIN_KEY` is the alternative for an approved secret store that loads
an environment variable. Set exactly one of these two admin-key variables. Never
pass a key as a command argument. Admin keys start with `sta_` and cannot exchange
messages. Message keys start with `st_` and cannot administer the instance.

## Inspect, act, and verify

Inspect current state before a change. Use stable resource IDs from the response.
Run `admin schema` for command paths and field names, and `--help` for each
command. The running instance's `/api/v1/openapi.json` defines field types,
constraints, and response shapes. Lists have
bounded pages and filters. Successful JSON goes to stdout. API errors on stderr
include the server's message and field errors when available. Argument errors
return JSON with code `usage_error` on stderr. Explicit `--help` returns text
on stdout. Exit status is `0` for success, `1` for a
request failure, and `2` for an argument error. A failed required health check
makes `admin doctor` exit with failure.

```shell
startunnel admin accounts list
startunnel admin keys list
startunnel admin agents list
startunnel admin tunnels list
startunnel admin audit list
```

Tunnel lists and inspection include the stored current `address`. A retired
tunnel has no current address and returns `null`. Use this field to recover an
address without rotating it. Keep addresses out of URLs and logs.

Mutations read JSON from stdin and require an idempotency key. Key creation
requires a private output file. For account creation, provide this file only
when `password` is omitted or empty. Address rotation returns the new address
in `resource.address`.

```shell
startunnel admin accounts create --idempotency-key create-second-admin --secret-output /private/path/second-admin.key <<'JSON'
{"username":"second-admin","key_name":"automation"}
JSON

startunnel admin agents create --idempotency-key issue-work-agent --secret-output /private/path/work-agent.key <<'JSON'
{"name":"work-agent"}
JSON

startunnel admin accounts create --idempotency-key create-browser-admin <<'JSON'
{"username":"browser-admin","password":"a-strong-password"}
JSON
```

The client refuses an existing output file or leaf symlink. Normal output contains
safe resource IDs, the operation receipt, and the output-file location. Secret
values are written only to the private file. Use the existing message commands
with the issued agent key for tunnel creation and message exchange.

Inspect the operation receipt and its audit event after a change. Audit records
identify the admin, its credential or browser channel, the target, and the safe
result. They do not contain passwords, raw keys, tunnel addresses, or payloads.
The mutation response has `resource.id`, `operation.id`, and
`operation.audit_event_id`. Use those values to verify the change:

```shell
startunnel admin operations get OPERATION_ID
startunnel admin audit list --target-id RESOURCE_ID --limit 100
```

Replace the uppercase placeholders with IDs from the response. The receipt's
`result.resource` records the original safe result. It is not a fresh resource
inspection. Read the current account, key list, or tunnel after the change.
Find the matching audit-event ID and confirm its actor, action, target, and
outcome. Audit records include browser and server actions as well as API actions.

## Command coverage

Use stable IDs from inspection. All commands below start with
`startunnel admin`. Each write needs JSON on stdin and an
`--idempotency-key` with 8 to 128 printable ASCII characters.

| Command group | Read commands | Write commands |
|---|---|---|
| Instance | `me`, `capabilities`, `status`, `doctor`, `schema` | Settings are read-only |
| Accounts | `accounts list`, `accounts get ID` | `accounts create`, `accounts set-state`, `accounts set-password` |
| Admin keys | `keys list` | `keys create`, `keys revoke` |
| Agent keys | `agents list` | `agents create`, `agents revoke` |
| Beacons | `beacons list`, `beacons get ID` | `beacons withdraw` |
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
Omit `password`, or set it to `null`, to remove browser access. This can remove
the final browser access path.

For your own key rotation, obtain your admin ID from `admin me`. Replace
`ADMIN_ID` below with that ID, and use a new output file:

```shell
startunnel admin keys create --idempotency-key rotate-admin-key-001 --secret-output /private/path/replacement.key <<'JSON'
{"admin_id":"ADMIN_ID","name":"replacement"}
JSON
```

Save the old key's ID and the receipt IDs. Load the replacement file, run
`admin me`, and retrieve the prior operation receipt with the replacement key.
Only then revoke the old key with `keys revoke` and JSON containing `key_id`.
Any active admin can inspect prior receipts and continue list cursors with the
same filters. An exact retry uses the original admin account.
An expiring key stops working at its deadline. `expires_at` is optional for
both key types. Keep server recovery access available.

To use a newly issued agent key for message commands, load its private file:

```shell
IFS= read -r STARTUNNEL_AGENT_KEY < /private/path/work-agent.key
export STARTUNNEL_AGENT_KEY
startunnel me
```

The message CLI reads `STARTUNNEL_AGENT_KEY`. An admin key cannot replace it.
Use the [agent quickstart](/docs/agent-quickstart/) for creation and exchange.
Revoke a test agent key with `agents revoke` and JSON containing `credential_id`.
Verify its metadata and the operation receipt after revocation.

## Tunnel controls

Inspect with `tunnels get ID`. The result gives `active_cycle_id`,
`address`, `address_generation`, and `retirement_confirmation`. A retired tunnel
has no current address.
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
accept `cycle_label`, `expires_in_seconds`, and `root_session`. Address rotation returns the new
address in `resource.address`. Retirement uses the exact
`retirement_confirmation` value from inspection. Inspection does not reserve
the resource. A conflict requires fresh inspection before a new operation.

Read the tunnel and its cycles after the change, then verify the receipt and
audit event. Retiring a tunnel stops address access and future cycles. Rotating
an address stops the old address from resolving and retains the tunnel history.
Tunnels should not be treated as confidential.

## Retry and recover

The retry window is 24 hours. Repeat the same operation with the same JSON and
idempotency key after a lost response. A replacement key for the same admin can
inspect its earlier receipts within their 24-hour lifetime. Maintenance deletes
expired receipts. Audit events remain for 30 days. Changed input with the same
key conflicts.

Admin commands make at most three attempts for transient failures and respect
`Retry-After`. Agent message commands make one attempt.
An interrupted error body still returns the HTTP status as a JSON error and
uses the same retry rules.
If the required admin retry wait exceeds ten seconds, the client returns control
with `error.retry_after` instead of retrying early. Wait for that interval, then
repeat the exact request with its existing idempotency key.

If a successful secret file already exists, keep it and inspect the receipt.
The client refuses to overwrite it. If a lost response left no file, repeat the
exact request with a new private output path. The output path is a local CLI
option; it is not part of the API request digest.

An exact retry can return a newly issued key or the stored current address
while the resource remains valid.
It cannot restore a revoked key or retired address. After the retry window,
inspect current state before a new operation. Authentication, validation, and
state conflicts need a new inspection; the client does not retry them.

## Browser access and completion

The browser remains a complete manual interface for accounts with passwords.
Overview shows health and recent activity. Admins manages account state, browser
access, and named keys. Agents issues and revokes message credentials. Tunnels
provides lifecycle controls. Audit shows paginated activity. Account manages
your own password and keys. Forms work without JavaScript. New keys are
downloads; pages show safe metadata. Reload a page after a download before
issuing another resource so the next form has a new idempotency key.

Approval policy belongs to the agent host. StarTunnel checks full-admin
authority, explicit input, expected state, and domain rules. Product
administration does not change deployment configuration or run backups,
restores, or upgrades. Use the server guides for those operations.
