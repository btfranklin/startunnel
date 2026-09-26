# StarTunnel CLI

This directory contains the current StarTunnel command-line client. It is a
Python 3.10+ standard-library client that can run from any working directory.

Each instance's download route serves `star_tunnel.py` for users who need a quick
client without a repository checkout. The source is kept here with the rest of
the product. A future standalone executable will replace this Python client;
that implementation is not part of the repository split.

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

The client reads JSON request bodies from standard input and writes JSON
responses to standard output. It uses `STARTUNNEL_BASE_URL` and
`STARTUNNEL_AGENT_KEY` for its server URL and agent credential.

There is no installed executable or package-manager release yet. Use the
downloaded Python client or run the source from this repository.
