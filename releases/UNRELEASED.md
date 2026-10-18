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
