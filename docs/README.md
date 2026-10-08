# StarTunnel Documentation

Use this index to find the document that owns each project topic.

## Document ownership

This document owns the documentation index and the source-of-truth map.

It does not own product rules, wire schemas, operational procedures, or test
requirements.

Go next to the document that owns your task. Use [AGENTS.md](../AGENTS.md) for
a short coding-agent task map.

## Sources of truth

| Document | What it owns |
|---|---|
| [VISION.md](../VISION.md) | Product purpose, principles, intended use, and non-goals |
| [Architecture](architecture.md) | Domains, dependency direction, data flow, tree order, and cycle lifecycle |
| [API](api.md) | API behavior, compatibility rules, and links to the wire schema |
| [Generated OpenAPI](../generated/openapi.json) | Authoritative request and response schema |
| [Security](security.md) | Trust model, threat boundaries, secrets, retention, and instance rules |
| [Operations](operations.md) | Docker, configuration, health, deployment, backup, and recovery |
| [Release policy](releases.md) | Versioned images, publication, compatibility, and upgrades |
| [Team setup index](setup/README.md) | Configuration selection and complete EC2, Lightsail, DigitalOcean, and existing-server setup paths |
| [Testing](testing.md) | Test matrix, commands, fixtures, and acceptance gates |
| [Design](design.md) | Brand, typography, navigation, and interface rules |
| [Decisions](decisions.md) | Durable reasons for non-obvious technical choices |
| [Implementation plan](implementation-plan.md) | Current phase status, open work, and release gates |
| [Public user documentation](user/index.md) | Public tutorials and user concepts |
| [Example index](../examples/README.md) | Runnable example selection, setup, and use |
| [Public agent guide](../skills/startunnel/SKILL.md) | Agent setup and connection workflow, also published by the promotional website |

## Public documentation

The installed application renders repository-owned Markdown from `docs/user/`. The fixed
documentation route manifest controls which files are public. Raw HTML is
disabled. Code samples use allowlisted regions from `examples/` so that a
runnable file stays the source of truth.

The fixed public routes are:

- `/docs/`
- `/docs/agent-quickstart/`
- `/docs/tutorial/`
- `/docs/concepts/`
- `/docs/authentication/`
- `/docs/instance-tunnels/`
- `/docs/rotation-and-history/`
- `/docs/delivery/`
- `/docs/errors/`
- `/docs/limits/`
- `/docs/security/`
- `/docs/examples/`
- `/docs/local-development/`
- `/downloads/star_tunnel.py`
- `/api/docs/`
- `/api/v1/openapi.json`

The public documentation is part of the release gate. See
[Testing](testing.md) for link, include, example, and tutorial checks.

The download route serves only `cli/star_tunnel.py`. The response is
an attachment and sets `X-Content-Type-Options: nosniff`.

The separate [promotional website](../website/README.md) publishes the agent
guide as HTML and Markdown, a small `llms.txt` index, and the generated API
schema. It does not serve application routes or issue credentials.

## Ownership rule

Put a durable fact in one document. Link to that document from other entry
points. Do not copy large rule sets between documents. If a deployment fact is
not available in the repository, mark it as an external input in
[Operations](operations.md).
