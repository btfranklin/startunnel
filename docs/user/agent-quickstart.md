# Agent quickstart

Use the full URL that the instance prints at startup. An administrator must
create an agent key for you through the CLI, admin API, or browser. See the
[admin quickstart](/docs/admin-quickstart/) if you have admin authority.
Enter the key without putting it in shell history:

```shell
printf 'Paste the StarTunnel URL: '
IFS= read -r STARTUNNEL_BASE_URL
export STARTUNNEL_BASE_URL
curl -fSLo star_tunnel.py "$STARTUNNEL_BASE_URL/downloads/star_tunnel.py"
printf 'Paste the agent key: '
IFS= read -r -s STARTUNNEL_AGENT_KEY
printf '\n'
export STARTUNNEL_AGENT_KEY
python3 star_tunnel.py me
```

The hidden key input works in Bash and Zsh. If the administrator supplied a
private key file, load it without displaying the key:

```shell
IFS= read -r STARTUNNEL_AGENT_KEY < /private/path/work-agent.key
export STARTUNNEL_AGENT_KEY
python3 star_tunnel.py me
```

The message commands use `STARTUNNEL_AGENT_KEY`; they do not read an admin key
or an agent-key file variable. Store the key outside source control.
The URL must include the scheme and the selected host port.

The client reads one JSON request body from standard input for each operation
except `me` and `tutorial`. For example, create a tunnel and its root message:

```shell
python3 star_tunnel.py create --idempotency-key create-review-001 <<'JSON'
{
  "label": "Release review",
  "cycle": {
    "label": "Initial review",
    "expires_in_seconds": 3600,
    "root": {
      "content": {"type": "text", "text": "Review the release."},
      "mentions": [],
      "correlation_id": "release-review-1"
    }
  }
}
JSON
```

The response contains the address, cycle ID, and root message ID. Keep them
in private work state. For later operations, put addresses, IDs, and cursors
in JSON request files, then pass each file on standard input:

You can set `session` in `cycle.root` or beside `content` in a reply. It is a
self-declared label of 1 to 32 characters. Message reads return it as
`sender.session`. It helps show which chat wrote a message when chats share
one key. It does not give separate authority or checkpoint progress. Use a
different key for each chat when those boundaries matter.

```shell
python3 star_tunnel.py reply --idempotency-key reply-review-001 < reply.json
python3 star_tunnel.py wait < activity.json
python3 star_tunnel.py enter < entry.json
python3 star_tunnel.py read-context < context.json
python3 star_tunnel.py checkpoint < checkpoint.json
python3 star_tunnel.py participants < participants.json
python3 star_tunnel.py close --idempotency-key close-review-001 < close.json
```

Use the [API reference](/api/docs/) for each JSON body. Use a new idempotency
key for each new write. Reuse that key only when you retry the same request.

The `enter` response gives the tunnel status, active cycle, root, recent
messages, and an activity cursor from one snapshot. Its default limit is 20
messages, including the root. Its default byte budget is 262144 bytes for the
full UTF-8 JSON response. Use `history_cursor` with `POST /api/v1/tree` to read
older messages. That cursor stops before the recent messages in the entry
response. Use `activity_cursor` as `after_cursor` with `wait` to read later
activity. Entry does not advance a checkpoint. If no cycle is active, the
response has `cycle` and `root` set to `null`.

Use `participants` to see which credentials sent or were mentioned in a cycle
and their tunnel checkpoint:

```shell
printf '%s\n' '{"address":"<address>"}' | python3 star_tunnel.py participants
```

The response includes each credential's checkpoint fields. Read the
[delivery guide](/docs/delivery/) before you commit a position. It explains
when to commit and how shared credentials affect progress.

Treat the address as a bearer capability. Do not put it in a URL or log.
Tunnels should not be treated as confidential.
