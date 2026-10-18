# Optional OpenAI Agents SDK proof

This proof creates separate sender and receiver agents. They do not share conversation history. Each agent receives one instance credential and only its role-specific tools.

The agents create a root, add two child messages, close the cycle, and verify the retained three-message tree. Program-owned API evidence is the proof; agent prose is not proof.

This example can spend OpenAI API credits. Set `STARTUNNEL_LIVE_AGENT_TESTS=1` to confirm the cost-bearing operation.

Required settings:

```text
STARTUNNEL_BASE_URL
STARTUNNEL_SENDER_KEY
STARTUNNEL_RECEIVER_KEY
OPENAI_API_KEY
OPENAI_MODEL
```

Run:

```shell
STARTUNNEL_LIVE_AGENT_TESTS=1 pdm run python -m examples.openai_agents.run_exchange
```

For an isolated Docker run, set `OPENAI_API_KEY` and `OPENAI_MODEL` in the
calling process, then run:

```shell
pdm run python scripts/run_isolated_test_lane.py live-agents --allow-openai-cost
```

This lane creates its own agent credentials in a mode-`0600` file under `/tmp`.
The child process loads that file; the test removes it and its evidence file
after use. Evidence contains identifiers and fixed event names, without
addresses, payloads, or keys. A direct example run uses the supplied instance
and keys and leaves its closed cycle subject to normal retention.

Authored prompts are in `prompts/`. The Python module does not contain fallback prompt text.
