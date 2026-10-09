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

Recovery records the action and writes a replacement key. There is no remote
unauthenticated bootstrap or recovery endpoint.

## Connect the existing client

Download the client from the instance's `/downloads/star_tunnel.py` route into a
dedicated client directory. It needs Python 3.10 or later and no extra packages.
Use HTTPS for a remote origin. Plain HTTP is permitted only on loopback.

```shell
export STARTUNNEL_BASE_URL='https://startunnel.example.com'
export STARTUNNEL_ADMIN_KEY_FILE='/private/path/admin.key'
python star_tunnel.py admin me
python star_tunnel.py admin capabilities
python star_tunnel.py admin schema
python star_tunnel.py admin doctor
```

`STARTUNNEL_ADMIN_KEY` is the alternative for an approved secret store that loads
an environment variable. Set exactly one of these two admin-key variables. Never
pass a key as a command argument. Admin keys start with `sta_` and cannot exchange
messages. Message keys start with `st_` and cannot administer the instance.

## Inspect, act, and verify

Inspect current state before a change. Use stable resource IDs from the response.
Run `admin schema` for the exact fields and `--help` for each command. Lists have
bounded pages and filters. Successful JSON goes to stdout. Safe JSON errors go
to stderr. A failed required health check makes `admin doctor` exit with failure.

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
Use `admin operations get --help` to select the receipt and `admin audit list`
to inspect the recorded activity.

## Retry and recover

The retry window is 24 hours. Repeat the same operation with the same JSON and
idempotency key after a lost response. A replacement key for the same admin can
inspect its earlier receipts. Changed input with the same key conflicts.

The client makes at most three attempts for transient failures. It respects
`Retry-After`. If the required wait exceeds ten seconds, it returns control
with `error.retry_after` instead of retrying early. Wait for that interval, then
repeat the exact request with its existing idempotency key.

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
