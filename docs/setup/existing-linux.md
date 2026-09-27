# Use an existing Linux server

**No domain?** Read [Run without a domain](no-domain.md) for the IP address
and TLS changes to this path.

[Setup index](README.md) → existing server → [installation](install.md)

Use this path when the team already controls an always-on server. It must have
a stable public IPv4 address and administrator access. A laptop that sleeps or
a server behind carrier-grade NAT does not meet these requirements.

## 1. Check the server

Connect through your existing SSH method. Keep that session open while you
check the network rules. Run:

```shell
cat /etc/os-release
uname -m
free -h
df -h / /var/lib
sudo ss -lntup
```

The shared guide requires Ubuntu 24.04 LTS and `x86_64`. Start with 2 vCPU,
4 GiB RAM, and 40 GB of SSD storage, with enough free space for images,
database growth, and temporary backups. For another operating system, provision
a separate Ubuntu server or adapt and test the installation first.

Confirm that the disk that holds `/var/lib/docker` is encrypted. Use the
provider's volume settings or your existing disk-encryption records; filesystem
type alone does not prove encryption. Put Docker data on encrypted storage
before you install the database.

Ports TCP 80 and 443 must be free for Caddy. Do not stop an existing website or
replace another service's proxy configuration to free them. If those ports are
in use, allocate a separate server for this guide. An existing proxy can serve
StarTunnel, but that custom integration is outside this step-by-step path.

If Docker is already installed, record `sudo docker version` and
`sudo docker compose version`. The installation guide needs Compose 2.24.4 or
later for the production override syntax. Keep existing containers, networks,
volumes, and Docker packages intact. Do not run a global prune.

## 2. Set the network rules

At the provider firewall or the router in front of the server, permit:

| Incoming traffic | Source |
|---|---|
| TCP 22, or your existing SSH port | The operator's public IP only |
| TCP 80 | Any IPv4 address, `0.0.0.0/0` |
| TCP 443 | Any IPv4 address, `0.0.0.0/0` |

Keep TCP 5432 and 8000 closed. Keep outbound DNS and HTTPS available for image
downloads, updates, and certificate issuance. Permit matching return traffic.
If a router performs NAT, forward TCP 80 and 443 to this server's fixed LAN
address. Do not forward PostgreSQL or the application's internal port.

Preserve any unrelated firewall rules. Docker-published ports can bypass UFW;
use the provider or upstream firewall for these ingress restrictions. See
[Docker's firewall guidance](https://docs.docker.com/engine/network/packet-filtering-firewalls/).
If the server also has public IPv6, apply equivalent restrictions there or
disable public IPv6 for this installation. Test a second SSH connection before
you close the first session.

## 3. Point the domain at the server

At your DNS provider, add an **A** record for `tunnel.example.com` with the
server's public IPv4 address. Use a TTL of 300 seconds during setup. Keep it
DNS-only if your DNS provider offers an HTTP proxy. Remove an old AAAA record
for this name unless IPv6 is also configured and tested.

On your workstation, check:

```shell
dig +short A tunnel.example.com
dig +short AAAA tunnel.example.com
```

The A result must be the server's address. With this IPv4-only path, the AAAA
result must be empty. Wait for DNS propagation before requesting HTTPS.

## Next step

Have the [image reference and source commit](image.md) ready, then follow
[Install StarTunnel](install.md). Use its fixed `/opt/startunnel` directory
only if that path is unused. The guide uses Compose project `startunnel-team`;
check that this name is also unused before you start.
