#!/bin/sh
# Execute the curl tree walkthrough without exposing bearer keys in command arguments.

set -eu
umask 077

: "${STARTUNNEL_BASE_URL:?Set STARTUNNEL_BASE_URL.}"
: "${STARTUNNEL_SENDER_KEY:?Set STARTUNNEL_SENDER_KEY.}"
: "${STARTUNNEL_RECEIVER_KEY:?Set STARTUNNEL_RECEIVER_KEY.}"

work_directory="$(mktemp -d /tmp/startunnel-curl-example.XXXXXX)"
create_body="$work_directory/create-body.json"
create_response="$work_directory/create-response.json"
tree_body="$work_directory/tree-body.json"
tree_response="$work_directory/tree-response.json"
reply_body="$work_directory/reply-body.json"
reply_response="$work_directory/reply-response.json"
message_body="$work_directory/message-body.json"
message_response="$work_directory/message-response.json"
branch_body="$work_directory/branch-body.json"
branch_response="$work_directory/branch-response.json"
replies_body="$work_directory/replies-body.json"
replies_response="$work_directory/replies-response.json"
close_body="$work_directory/close-body.json"
close_response="$work_directory/close-response.json"
retained_response="$work_directory/retained-response.json"

cleanup() {
  rm -f \
    "$create_body" "$create_response" "$tree_body" "$tree_response" \
    "$reply_body" "$reply_response" "$message_body" "$message_response" \
    "$branch_body" "$branch_response" "$replies_body" "$replies_response" \
    "$close_body" "$close_response" "$retained_response"
  rmdir "$work_directory" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM

request() {
  method="$1"
  path="$2"
  key="$3"
  body_file="$4"
  output_file="$5"
  expected_status="$6"
  idempotency_key="${7:-}"

  status="$({
    printf 'silent\nshow-error\n'
    printf 'header = "Authorization: Bearer %s"\n' "$key"
    printf 'header = "Content-Type: application/json"\n'
    if [ -n "$idempotency_key" ]; then
      printf 'header = "Idempotency-Key: %s"\n' "$idempotency_key"
    fi
  } | curl --config - \
    --request "$method" \
    --data-binary "@$body_file" \
    --output "$output_file" \
    --write-out '%{http_code}' \
    "$STARTUNNEL_BASE_URL$path")"

  if [ "$status" != "$expected_status" ]; then
    printf 'The curl example failed at %s %s with HTTP %s.\n' "$method" "$path" "$status" >&2
    exit 1
  fi
}

printf '%s\n' '{
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
}' >"$create_body"
request POST /api/v1/tunnels "$STARTUNNEL_SENDER_KEY" "$create_body" \
  "$create_response" 201 curl-tree-create

address="$(pdm run python -c 'import json,sys; print(json.load(open(sys.argv[1]))["tunnel"]["address"])' "$create_response")"
cycle_id="$(pdm run python -c 'import json,sys; print(json.load(open(sys.argv[1]))["cycle"]["id"])' "$create_response")"
root_id="$(pdm run python -c 'import json,sys; print(json.load(open(sys.argv[1]))["cycle"]["root"]["id"])' "$create_response")"

STARTUNNEL_CURL_ADDRESS="$address" STARTUNNEL_CYCLE_ID="$cycle_id" OUTPUT_BODY="$tree_body" pdm run python -c '
import json, os
with open(os.environ["OUTPUT_BODY"], "w", encoding="utf-8") as output:
    json.dump({"address": os.environ["STARTUNNEL_CURL_ADDRESS"], "cycle_id": os.environ["STARTUNNEL_CYCLE_ID"], "limit": 1000}, output, ensure_ascii=False)
'
request POST /api/v1/tree "$STARTUNNEL_RECEIVER_KEY" "$tree_body" \
  "$tree_response" 200

STARTUNNEL_CURL_ADDRESS="$address" STARTUNNEL_ROOT_ID="$root_id" OUTPUT_BODY="$reply_body" pdm run python -c '
import json, os
with open(os.environ["OUTPUT_BODY"], "w", encoding="utf-8") as output:
    json.dump({
        "address": os.environ["STARTUNNEL_CURL_ADDRESS"],
        "parent_id": os.environ["STARTUNNEL_ROOT_ID"],
        "content": {"type": "json", "value": {"status": "reviewed", "artifact": "curl-note"}},
        "mentions": [],
        "correlation_id": "curl-exchange-1",
    }, output, ensure_ascii=False)
'
request POST /api/v1/messages "$STARTUNNEL_RECEIVER_KEY" "$reply_body" \
  "$reply_response" 201 curl-tree-reply

reply_id="$(pdm run python -c 'import json,sys; print(json.load(open(sys.argv[1]))["message"]["id"])' "$reply_response")"

STARTUNNEL_CURL_ADDRESS="$address" STARTUNNEL_CYCLE_ID="$cycle_id" STARTUNNEL_REPLY_ID="$reply_id" OUTPUT_BODY="$message_body" pdm run python -c '
import json, os
with open(os.environ["OUTPUT_BODY"], "w", encoding="utf-8") as output:
    json.dump({"address": os.environ["STARTUNNEL_CURL_ADDRESS"], "cycle_id": os.environ["STARTUNNEL_CYCLE_ID"], "message_id": os.environ["STARTUNNEL_REPLY_ID"]}, output, ensure_ascii=False)
'
request POST /api/v1/tree/message "$STARTUNNEL_SENDER_KEY" "$message_body" \
  "$message_response" 200

STARTUNNEL_CURL_ADDRESS="$address" STARTUNNEL_CYCLE_ID="$cycle_id" STARTUNNEL_REPLY_ID="$reply_id" OUTPUT_BODY="$branch_body" pdm run python -c '
import json, os
with open(os.environ["OUTPUT_BODY"], "w", encoding="utf-8") as output:
    json.dump({"address": os.environ["STARTUNNEL_CURL_ADDRESS"], "cycle_id": os.environ["STARTUNNEL_CYCLE_ID"], "leaf_id": os.environ["STARTUNNEL_REPLY_ID"]}, output, ensure_ascii=False)
'
request POST /api/v1/tree/branch "$STARTUNNEL_SENDER_KEY" "$branch_body" \
  "$branch_response" 200

STARTUNNEL_CURL_ADDRESS="$address" STARTUNNEL_CYCLE_ID="$cycle_id" STARTUNNEL_ROOT_ID="$root_id" OUTPUT_BODY="$replies_body" pdm run python -c '
import json, os
with open(os.environ["OUTPUT_BODY"], "w", encoding="utf-8") as output:
    json.dump({"address": os.environ["STARTUNNEL_CURL_ADDRESS"], "cycle_id": os.environ["STARTUNNEL_CYCLE_ID"], "message_id": os.environ["STARTUNNEL_ROOT_ID"], "limit": 100}, output, ensure_ascii=False)
'
request POST /api/v1/tree/replies "$STARTUNNEL_SENDER_KEY" "$replies_body" \
  "$replies_response" 200

STARTUNNEL_CURL_ADDRESS="$address" STARTUNNEL_CYCLE_ID="$cycle_id" OUTPUT_BODY="$close_body" pdm run python -c '
import json, os
with open(os.environ["OUTPUT_BODY"], "w", encoding="utf-8") as output:
    json.dump({"address": os.environ["STARTUNNEL_CURL_ADDRESS"], "expected_cycle_id": os.environ["STARTUNNEL_CYCLE_ID"]}, output, ensure_ascii=False)
'
request POST /api/v1/cycles/close "$STARTUNNEL_SENDER_KEY" "$close_body" \
  "$close_response" 204 curl-tree-close

request POST /api/v1/tree "$STARTUNNEL_RECEIVER_KEY" "$tree_body" \
  "$retained_response" 200

printf 'The curl tree exchange is complete. The stable address still reads the closed cycle.\n'
