# Unreleased changes after v0.1.0

These changes have not been released. Follow the selected release's notes when
upgrading a published installation. The server and each agent host need separate
updates; pushing source does not update either one.

## Installed client command

After the server upgrade, download its client on each Linux or macOS agent host:

```shell
curl -fSLo star_tunnel.py "$STARTUNNEL_BASE_URL/downloads/star_tunnel.py"
python3 star_tunnel.py install
export PATH="$HOME/.local/bin:$PATH"
startunnel me
```

The installed command needs Python 3.10 or later. Set `PATH` in jobs and agent
containers too. Admin users can verify with `startunnel admin me` instead.
Keep existing credentials. See the [client guide](../cli/README.md) and
[server upgrade guide](../docs/setup/maintain.md#update-the-application).

## Later client updates

After later server upgrades, run `startunnel update` on each agent host.
`STARTUNNEL_BASE_URL` selects the source instance. Then verify with
`startunnel me` or `startunnel admin me`. The update does not change keys and
does not run automatically. Failed downloads leave the installed client intact.
The old v0.1.0 script needs the initial installation above; it has no updater.
The updater now checks the declared download size before replacement, so an
interrupted transfer leaves the installed client intact.

## Client identification

`startunnel version` returns JSON with the client version, source revision, and
file checksum. It works offline. Source and default custom image builds report
`unreleased`; release candidates include the selected version and source commit.
Check this output after client updates. `VERSION` and the published v0.1.0
release are unchanged by this work.

## Complete agent command coverage

The client now covers all agent API operations. It adds tree reads, message
search, tunnel status, cycle listing, cycle start, and rollover. Use
`startunnel COMMAND --help` for the request body. Cycle writes require an
idempotency key and inspected state. Upgrade the client after the server so
these commands match the instance's API.

## Offline command definitions

`startunnel schema` returns JSON definitions for all agent API commands.
Add a command name, such as `startunnel schema reply`, to select one.
No URL or credential is needed. Use the running instance's OpenAPI document
for nested types, defaults, limits, and responses. Admin command definitions
remain available through `startunnel admin schema`.

## JSON argument errors

Argument errors now return JSON on stderr with code `usage_error` and exit
status `2`. This includes missing commands, invalid options, and invalid
idempotency keys. Update callers that parse the old argument-error text.
Request failures still exit with `1`. Successful commands exit with `0`;
explicit `--help` returns text on stdout with exit status `0`.

## Interrupted API responses

An interrupted HTTP error body now returns a JSON error with the HTTP status.
Admin commands apply their existing transient-failure retry rules. Agent
commands still make one attempt.

## Admin client file handling

Direct script use on Windows supports admin key files and key output files.
New output paths must still be unused. On Windows, choose a private directory;
the client does not change access-control lists. Linux and macOS key output
files retain mode `0600`.

## Portable JSON output

JSON output now escapes non-ASCII characters. This prevents output failures
on hosts with legacy encodings. Decoded addresses and message strings are
unchanged. Callers must read the JSON values instead of copying escaped text
from the serialized document.
