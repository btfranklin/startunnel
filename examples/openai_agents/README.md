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
STARTUNNEL_LIVE_AGENT_TESTS=1   pdm run python -m examples.openai_agents.run_exchange
```

The protected Docker fixture can create a mode-`0600` credential file under `/tmp` with `provision_live_agent_proof`. The proof reads it into the child environment and deletes it after use. Evidence contains identifiers and fixed event names only. It does not contain an address, payload, or key.

Authored prompts are in `prompts/`. The Python module does not contain fallback prompt text.
