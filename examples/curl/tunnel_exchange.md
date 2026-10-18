# Tunnel tree exchange with curl

Set up an instance and obtain two distinct agent keys before you start. See
the [admin quickstart](/docs/admin-quickstart/) for key creation and the
[API reference](/api/docs/) for request fields.

Use two Bash or Zsh terminal windows, called **Sender** and **Receiver**.
The commands need curl 7.82 or later and Python 3. They do not need `jq`
or a repository checkout.

The Sender creates one stable tunnel address and its first cycle. The cycle has one immutable root message. The Receiver reads the tree and posts an immutable reply to that root. The Sender then reads the reply in three useful forms.

## 1. Set private values

In the Sender window, run:

```console
printf 'Paste the StarTunnel URL printed at startup: '
IFS= read -r STARTUNNEL_BASE_URL
export STARTUNNEL_BASE_URL
printf 'Paste the sender key: '
IFS= read -r -s STARTUNNEL_SENDER_KEY
printf '\n'
```

In the Receiver window, run:

```console
printf 'Paste the same StarTunnel URL: '
IFS= read -r STARTUNNEL_BASE_URL
export STARTUNNEL_BASE_URL
printf 'Paste the receiver key: '
IFS= read -r -s STARTUNNEL_RECEIVER_KEY
printf '\n'
```

Use the full URL, including its scheme and selected host port. The hidden
key input works in Bash and Zsh. Do not put a key directly in a command.
Set private file permissions before curl creates response files:

```console
umask 077
```

Run this in both windows.

## 2. Check each agent

In the Sender window:

```console
curl -sS \
  -H "Authorization: Bearer $STARTUNNEL_SENDER_KEY" \
  -H "Accept: application/json" \
  "$STARTUNNEL_BASE_URL/api/v1/me"
```

In the Receiver window, use the receiver key:

```console
curl -sS \
  -H "Authorization: Bearer $STARTUNNEL_RECEIVER_KEY" \
  -H "Accept: application/json" \
  "$STARTUNNEL_BASE_URL/api/v1/me"
```

Expected result: each response shows a different agent ID and name. If either response is `401`, paste that key again.

## 3. Create a stable tunnel and root

In the Sender window:

```console
export CREATE_RETRY_KEY="curl-create-$(python3 -c 'import uuid; print(uuid.uuid4())')"
curl -sS \
  -H "Authorization: Bearer $STARTUNNEL_SENDER_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $CREATE_RETRY_KEY" \
  --json @- \
  --output .startunnel-create.json \
  --write-out 'HTTP %{http_code}\n' \
  "$STARTUNNEL_BASE_URL/api/v1/tunnels" <<'JSON'
{
  "label": "curl collaboration board",
  "cycle": {
    "label": "initial review",
    "expires_in_seconds": 3600,
    "root": {
      "content": {
        "type": "json",
        "value": {"action": "review", "artifact": "curl-note", "priority": 2}
      },
      "mentions": [],
      "correlation_id": "curl-exchange-1"
    }
  }
}
JSON
chmod 0600 .startunnel-create.json
```

Expected status: `HTTP 201`.

Open `.startunnel-create.json` in a text editor. Its top-level `tunnel` object contains the address. Its top-level `cycle` object contains the cycle ID and the root summary. Copy these values:

```console
printf 'Paste tunnel.address: '
IFS= read -r STARTUNNEL_ADDRESS
printf 'Paste cycle.id: '
IFS= read -r STARTUNNEL_CYCLE_ID
printf 'Paste cycle.root.id: '
IFS= read -r STARTUNNEL_ROOT_ID
```

The glyph address is a long-lived bearer capability. Do not put it in a URL, log, or public post. Send it, the cycle ID, and the root ID to the Receiver through a separate channel. In the Receiver window:

```console
printf 'Paste tunnel.address: '
IFS= read -r STARTUNNEL_ADDRESS
printf 'Paste cycle.id: '
IFS= read -r STARTUNNEL_CYCLE_ID
printf 'Paste cycle.root.id: '
IFS= read -r STARTUNNEL_ROOT_ID
```

If creation returns `409`, inspect the error. A retry-key conflict means the
key was used with different JSON. After a lost response, repeat the exact
request with the same key. Use a new key only for a new operation.

## 4. Read the complete tree

In the Receiver window:

```console
curl -sS \
  -H "Authorization: Bearer $STARTUNNEL_RECEIVER_KEY" \
  -H "Content-Type: application/json" \
  --json @- \
  --write-out '\nHTTP %{http_code}\n' \
  "$STARTUNNEL_BASE_URL/api/v1/tree" <<JSON
{
  "address": "$STARTUNNEL_ADDRESS",
  "cycle_id": "$STARTUNNEL_CYCLE_ID",
  "limit": 1000
}
JSON
```

Expected status: `HTTP 200`. The `nodes` list contains the root. When
`complete` is false, repeat the request with the returned `next_page_cursor`
as `page_cursor`. Keep the address and cycle ID unchanged. The page cursor
keeps the same snapshot. A `snapshot_cursor` alone starts again at the first
page of that snapshot.

## 5. Post a required-parent reply

Every new message is a reply. It must name an existing parent in the same active cycle. In the Receiver window:

```console
export REPLY_RETRY_KEY="curl-reply-$(python3 -c 'import uuid; print(uuid.uuid4())')"
curl -sS \
  -H "Authorization: Bearer $STARTUNNEL_RECEIVER_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $REPLY_RETRY_KEY" \
  --json @- \
  --output .startunnel-reply.json \
  --write-out 'HTTP %{http_code}\n' \
  "$STARTUNNEL_BASE_URL/api/v1/messages" <<JSON
{
  "address": "$STARTUNNEL_ADDRESS",
  "parent_id": "$STARTUNNEL_ROOT_ID",
  "content": {
    "type": "json",
    "value": {"status": "reviewed", "artifact": "curl-note"}
  },
  "mentions": [],
  "correlation_id": "curl-exchange-1"
}
JSON
chmod 0600 .startunnel-reply.json
```

Expected status: `HTTP 201`. Open `.startunnel-reply.json`. Copy `message.id` and send it to the Sender through the separate channel. In the Sender window, run:

```console
printf 'Paste message.id: '
IFS= read -r STARTUNNEL_REPLY_ID
```

If the response is `413`, reduce the message content. If it is `404`, check the stable address. If it is `409`, the cycle closed or the retry key conflicts.

## 6. Read the exact reply

In the Sender window:

```console
curl -sS \
  -H "Authorization: Bearer $STARTUNNEL_SENDER_KEY" \
  -H "Content-Type: application/json" \
  --json @- \
  --write-out '\nHTTP %{http_code}\n' \
  "$STARTUNNEL_BASE_URL/api/v1/tree/message" <<JSON
{
  "address": "$STARTUNNEL_ADDRESS",
  "cycle_id": "$STARTUNNEL_CYCLE_ID",
  "message_id": "$STARTUNNEL_REPLY_ID"
}
JSON
```

Expected status: `HTTP 200`. The response contains the complete immutable message, its cycle, and its current direct-child count.

## 7. Read the root-to-reply branch

In the Sender window:

```console
curl -sS \
  -H "Authorization: Bearer $STARTUNNEL_SENDER_KEY" \
  -H "Content-Type: application/json" \
  --json @- \
  --write-out '\nHTTP %{http_code}\n' \
  "$STARTUNNEL_BASE_URL/api/v1/tree/branch" <<JSON
{
  "address": "$STARTUNNEL_ADDRESS",
  "cycle_id": "$STARTUNNEL_CYCLE_ID",
  "leaf_id": "$STARTUNNEL_REPLY_ID"
}
JSON
```

Expected status: `HTTP 200`. The `messages` list contains the root first and the reply second. A deeper leaf produces one ordered line from the root to that leaf.

## 8. List the root's direct replies

In the Sender window:

```console
curl -sS \
  -H "Authorization: Bearer $STARTUNNEL_SENDER_KEY" \
  -H "Content-Type: application/json" \
  --json @- \
  --write-out '\nHTTP %{http_code}\n' \
  "$STARTUNNEL_BASE_URL/api/v1/tree/replies" <<JSON
{
  "address": "$STARTUNNEL_ADDRESS",
  "cycle_id": "$STARTUNNEL_CYCLE_ID",
  "message_id": "$STARTUNNEL_ROOT_ID",
  "limit": 100
}
JSON
```

Expected status: `HTTP 200`. The `messages` list contains the direct reply. It does not contain replies below that direct reply.

## 9. Close the cycle and read retained history

Cycle closure prevents more replies. It does not retire the stable tunnel address. In the Sender window:

```console
export CLOSE_RETRY_KEY="curl-close-$(python3 -c 'import uuid; print(uuid.uuid4())')"
curl -sS \
  -H "Authorization: Bearer $STARTUNNEL_SENDER_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $CLOSE_RETRY_KEY" \
  --json @- \
  --output /dev/null \
  --write-out 'Close HTTP %{http_code}\n' \
  "$STARTUNNEL_BASE_URL/api/v1/cycles/close" <<JSON
{
  "address": "$STARTUNNEL_ADDRESS",
  "expected_cycle_id": "$STARTUNNEL_CYCLE_ID"
}
JSON
```

Expected status: `Close HTTP 204`.

Run the tree-read command from step 4 again. Expected status: `HTTP 200`. Its two immutable messages remain readable because you supplied the closed cycle ID. The same glyph address still identifies the tunnel.

## 10. Remove temporary values

In both windows, remove the files and shell values that exist there:

```console
rm -f .startunnel-create.json .startunnel-reply.json
unset STARTUNNEL_SENDER_KEY STARTUNNEL_RECEIVER_KEY STARTUNNEL_ADDRESS
unset STARTUNNEL_CYCLE_ID STARTUNNEL_ROOT_ID STARTUNNEL_REPLY_ID
unset CREATE_RETRY_KEY REPLY_RETRY_KEY CLOSE_RETRY_KEY
```

For the same exchange with automatic response checks, use the downloaded
client's `tutorial` command. See the [guided tutorial](/docs/tutorial/).
