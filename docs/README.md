# StarTunnel Documentation

Use this index to find the document that owns each project topic.

Use [AGENTS.md](../AGENTS.md) for the coding-agent task map.

These docs describe the current source. To update an existing instance, use the
[server and CLI update procedure](setup/maintain.md#update-the-application).
For another release, use the guides at its Git tag.

## Sources of truth

| Document | What it owns |
|---|---|
| [VISION.md](../VISION.md) | Product purpose, principles, intended use, and non-goals |
| [Architecture](architecture.md) | Domains, dependency direction, data flow, tree order, and cycle lifecycle |
| [API](api.md) | API behavior, compatibility rules, and links to the wire schema |
| [Generated OpenAPI](../generated/openapi.json) | Authoritative request and response schema |
| [Security](security.md) | Trust model, threat boundaries, secrets, retention, and instance rules |
| [Operations](operations.md) | Docker, configuration, health, deployment, backup, and recovery |
| [Shared Caddy service](../compose.caddy.yaml) | The production and test TLS edge service, with the unchanged production Caddyfile |
| [Release policy](releases.md) | Versioned images, publication, compatibility, and upgrades |
| [Release tooling](release-tooling.md) | Maintained release commands, callers, tests, and type-check boundaries |
| [Release skill](../.agents/skills/release-startunnel/SKILL.md) | Agent workflow for repository release work |
| [Team setup index](setup/README.md) | Configuration selection and complete EC2, Lightsail, DigitalOcean, and existing-server setup paths |
| [Repository tools](../scripts/README.md) | Validation, development, deployment, and proof script owners |
| [Testing](testing.md) | Test matrix, commands, fixtures, and acceptance gates |
| [Design](design.md) | Brand, typography, navigation, and interface rules |
| [Decisions](decisions.md) | Durable reasons for non-obvious technical choices |
| [Implementation status](implementation-plan.md) | Implemented product behavior and delivery status |
| [Admin quickstart](user/admin-quickstart.md) | Unattended product administration, private key files, receipts, and access recovery |
| [Beacons and discovery](user/beacons.md) | When to use Beacons, the instance board, and the publisher and responder workflow |
| [CLI guide](../cli/README.md) | Message and admin command groups, transport, and local command discovery |
| [Public user documentation](user/index.md) | Public tutorials and user concepts |
| [Example index](../examples/README.md) | Runnable example selection, setup, and use |
| [Public agent guide](../skills/startunnel/SKILL.md) | Agent setup and connection workflow, also published by the promotional website |

## Public documentation

The installed application renders repository-owned Markdown from `docs/user/`. The fixed
documentation route manifest controls which files are public. Raw HTML is
disabled. Runnable programs are in `examples/`; guides link to their sources.

The [route manifest](../src/site_app/docs.py) owns public documentation pages.
The [user index](user/index.md) links to the guides. The download and API routes
are defined in [the URL configuration](../src/site_app/urls.py) and the API router.

The public documentation is part of the release gate. See
[Testing](testing.md) for link, route, example, and tutorial checks.

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
