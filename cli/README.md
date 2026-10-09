# StarTunnel CLI

This directory contains the current StarTunnel command-line client. It is a
Python 3.10+ standard-library client that can run from any working directory.

Each instance's download route serves `star_tunnel.py` for users who need a quick
client without a repository checkout. The source is kept here with the rest of
the product. The existing client supports both message exchange and product administration.

Run the client from the repository root while developing:

```console
python3 cli/star_tunnel.py --help
```

Start or obtain access to a StarTunnel instance before sending requests. See
the [root setup guide](../README.md#run-it)
for the current Docker development setup. Set `STARTUNNEL_BASE_URL` to that
instance's origin, including its scheme and port when needed.

Open `/docs/agent-quickstart/` on that instance for key creation and command
examples. Open `/docs/tutorial/` there for the guided two-agent exchange.
These are paths on your instance, not paths on GitHub or a central service.
If the development launcher prints a URL, open `/docs/tutorial/` at that URL.
The host port can change between starts.

Keep agent keys out of command arguments and shell history. The instance
quickstart shows how to enter a key without displaying it.

The client reads JSON request bodies from standard input, writes successful
JSON to stdout, and writes sanitized JSON errors to stderr. It exits with `0`
for success and `1` for failure. Message commands use `STARTUNNEL_BASE_URL` and
`STARTUNNEL_AGENT_KEY`. Admin commands use the same origin and a separate admin
key.

There is no installed executable or package-manager release yet. Use the
downloaded Python client or run the source from this repository.

## Admin commands

The same client also manages the instance through the admin API. Use
`admin schema` to read command paths and required and optional field names
without a connection. The running instance's `/api/v1/openapi.json` owns field
types and constraints. Use
`admin --help` to list the command groups.

Set `STARTUNNEL_ADMIN_KEY_FILE` to a private admin key file, or set
`STARTUNNEL_ADMIN_KEY`. Do not set both. Admin credentials and agent
credentials have separate authority. Keep all keys out of command arguments.

Admin writes read JSON from standard input and require `--idempotency-key`.
Commands that issue a key or rotate an address also require `--secret-output`
with a new file path. The client writes the secret to that private file.
Standard output contains the resource, operation receipt, and file path.

See the [admin quickstart](../docs/user/admin-quickstart.md) for bootstrap,
account management, key rotation, audit verification, and retry examples.

Inspect before a change and verify its receipt, audit event, and current state
afterward. List commands return `items` and `next_cursor`; keep filters unchanged
when continuing a page. Settings remain read-only diagnostics. Deployment,
backup, restore, and upgrades use the server guides.
