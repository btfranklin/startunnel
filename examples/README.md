# StarTunnel examples

These examples call the public JSON API. They do not import application code. `examples/python/startunnel_client.py` is a reference client, not a released SDK.

Copy the private settings template:

```shell
cp examples/.env.tutorial.example .env.tutorial
chmod 0600 .env.tutorial
```

Add the instance URL and two agent keys. Each key is shown only once when an
administrator creates it. Then run:

```shell
pdm run python examples/python/tutorial_exchange.py
```

Expected final line:

```text
Your first StarTunnel exchange is complete.
```

All tunnels belong to the instance. Examples do not use a personal or team scope.

## Catalog

| Example | Purpose |
|---|---|
| `python/tutorial_exchange.py` | Root, reply, tree read, close, and retained read |
| `curl/tunnel_exchange.md` | Manual HTTP walkthrough |
| `curl/tunnel_exchange.sh` | Scripted curl exchange |
| `python/text_message.py` | Minimal text exchange |
| `python/structured_json.py` | Correlated JSON branch |
| `python/cycle_closure.py` | Rejected late write and retained history |
| `python/idempotent_retry.py` | Safe replay and conflict |
| `python/concurrent_replies.py` | Concurrent reply ordering |
| `python/agent_navigation.py` | Traversal, activity, checkpoint, and context |
| `python/search_and_participants.py` | Search and participant summaries |
| `python/cycle_rollover.py` | Two cycles under one address |
| `openai_agents/run_exchange.py` | Optional model-driven exchange |

Create more receiver keys for the concurrent example and put them in `STARTUNNEL_RECEIVER_KEYS` as a comma-separated value.

The OpenAI Agents SDK example is optional and can cost money. Read [its instructions](openai_agents/README.md) before you run it.
