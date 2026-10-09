# Install StarTunnel

[Setup index](README.md) → provider → installation → [verification](verify.md)

Use this guide after [EC2](aws-ec2.md), [Lightsail](aws-lightsail.md),
[DigitalOcean](digitalocean.md), or [an existing Linux server](existing-linux.md).
It installs a new instance. It does not convert an older StarTunnel database.

You need a prepared Ubuntu 24.04 x86-64 server, working DNS, and the exact
source commit and image digest from [the image guide](image.md).
For an official image, take both values from the published release's attached
`release.json`. For a custom image, use the operator's matching image build record.
For a public IP address with no DNS, first read [Run without a domain](no-domain.md).
The default TLS steps below need the changes described there.

All commands on this page run in the **server's SSH terminal**.
Stop if a command fails; correct it before you continue.

## 1. Open an administrator shell

```shell
sudo -i
set -euo pipefail
umask 077
```

If you are already logged in as root, omit `sudo -i`. Keep this root shell for
the remaining server steps. Do not run them in your workstation's local shell.
The application itself runs as an unprivileged container user.

Check that `/opt/startunnel` and Compose project `startunnel-team` are not used
by another installation. Do not overwrite an existing instance.

## 2. Install the host tools

```shell
apt-get update
apt-get install --yes ca-certificates curl git python3 dnsutils age
```

Python here runs small standard-library setup scripts. The application's
Python 3.14 runtime and dependencies are in its image. No host PDM installation
is required.

On a fresh server, install Docker from its official repository:

```shell
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources <<'EOF'
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update
apt-get install --yes docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
docker version
docker compose version
```

If the server already has Docker Engine and Compose 2.24.4 or later, retain
that installation and skip the Docker installation block. Do not remove Docker
packages that another application uses. For package conflicts, use
[Docker's Ubuntu instructions](https://docs.docker.com/engine/install/ubuntu/)
before you continue. `docker version` must show a working server.

## 3. Prepare the data directory

On Lightsail and DigitalOcean, first complete the provider's
[attached storage steps](storage.md). Confirm that `/srv/startunnel` is mounted
on that disk with `findmnt /srv/startunnel`.
On EC2 with an encrypted root disk, or an existing server with an encrypted
root filesystem, create the directory on that filesystem:

```shell
install -d -m 700 /srv/startunnel
```

On a **new Docker installation with no containers or volumes**, put Docker
data on this encrypted storage. Do not use this block to move an existing
Docker installation or replace an existing `daemon.json`:

```shell
systemctl stop docker.service docker.socket
install -d -m 0755 /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{"data-root": "/srv/startunnel/docker"}
EOF
install -d -m 0755 /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/startunnel-storage.conf <<'EOF'
[Unit]
RequiresMountsFor=/srv/startunnel
EOF
systemctl daemon-reload
systemctl start docker
docker info --format '{{.DockerRootDir}}'
```

Expect `/srv/startunnel/docker`. The mount dependency prevents Docker from
starting without the required disk. On an existing Docker installation, keep
its configuration if its Docker data directory is already on encrypted
storage, as checked in the existing-server guide. In both cases,
`/srv/startunnel` must also be on encrypted storage for secrets and backups.

## 4. Get the matching deployment files

```shell
umask 022
git clone https://github.com/btfranklin/startunnel.git /opt/startunnel
cd /opt/startunnel
printf 'Source commit from release.json or the custom image build record: '
IFS= read -r SOURCE_COMMIT
git checkout --detach "$SOURCE_COMMIT"
git rev-parse HEAD
umask 077
```

The printed commit must match `source_commit` in `release.json` or the custom
image build record. Stop if it does not.
This checkout supplies Compose files, PostgreSQL scripts, and the Caddy file.
Keep it at the matching commit for the life of this deployment.
The checkout uses normal source permissions so PostgreSQL and Caddy can read
their mounted scripts and configuration. Only private runtime files use mode
`0600`. Do not apply mode `0600` to the whole source checkout.

## 5. Save the production configuration

Enter the real domain without `https://` or a path. Enter the complete digest
reference, not a tag. These values are not secrets:

```shell
printf 'Public hostname, for example tunnel.example.com: '
IFS= read -r STARTUNNEL_DOMAIN
printf 'Application image repository@sha256:digest: '
IFS= read -r STARTUNNEL_APP_IMAGE
cat > /opt/startunnel/production.env <<EOF
STARTUNNEL_DOMAIN=$STARTUNNEL_DOMAIN
STARTUNNEL_APP_IMAGE=$STARTUNNEL_APP_IMAGE
STARTUNNEL_SECRETS_DIR=/srv/startunnel/secrets
COMPOSE_PROJECT_NAME=startunnel-team
COMPOSE_FILE=/opt/startunnel/compose.yaml:/opt/startunnel/compose.prod.yaml
COMPOSE_PROFILES=edge
EOF
chmod 600 /opt/startunnel/production.env
set -a
. /opt/startunnel/production.env
set +a
```

Use only the hostname and image-reference formats shown above. This file is
shell configuration owned by root. Do not put passwords or arbitrary pasted
commands in it. It is ignored by Git. Every new server shell must load this
file before running production Compose or backup commands.

This selects the production overlay and Caddy profile for subsequent commands.
Do not add `compose.dev.yaml`, run `pdm run dev`, or generate a development
`.env` in this checkout.

## 6. Pull the image and create secrets

For a private GHCR image, create a separate short-lived GitHub token with
`read:packages` and read access to the package. Authorize SSO if required. Run
`docker login ghcr.io --username YOUR_GITHUB_USERNAME` and enter that token at
the hidden prompt. Do not use the workstation's write token on the server.
For another registry, use its login command. Public images need no login.

```shell
docker pull "$STARTUNNEL_APP_IMAGE"
docker image inspect "$STARTUNNEL_APP_IMAGE" \
  --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
python3 scripts/generate_production_secrets.py --directory "$STARTUNNEL_SECRETS_DIR"
chown 10001:10001 "$STARTUNNEL_SECRETS_DIR"/*
chmod 700 "$STARTUNNEL_SECRETS_DIR"
chmod 600 "$STARTUNNEL_SECRETS_DIR"/*
```

The revision label must match the recorded source commit. If a supplied image
has no label, obtain its build record before you proceed. The generator creates
eight independent secret files. It refuses to replace existing files. Do not
regenerate secrets for a running instance.

The default application image runs with UID `10001`. On Linux, Compose mounts
these files with their host ownership. The ownership step lets the application
read them at mode `0600`; root owns the containing directory. Do not make them
world-readable to solve a permission error. The PostgreSQL entrypoint reads its
files as root before it starts the database process.

## 7. Start the production stack

```shell
python3 scripts/start_production.py --edge
docker compose up -d --no-build --wait --wait-timeout 180
docker compose ps --all
```

Expect `postgres`, `maintenance`, and `web` to be running and healthy, `caddy`
to be running, and `migrate` to have exited with code `0`. The first command
validates image digests and secret files. The second waits for service health.
Neither command builds application code on the server.

Check the actual published ports:

```shell
docker compose port web 8000 || true
docker compose port postgres 5432 || true
docker compose port caddy 443
```

The first two commands must show no published port; some Compose versions
return a nonzero status for this expected result. Caddy must publish port 443.
Only Caddy publishes network ports in this configuration.

```shell
curl --fail --silent --show-error "https://$STARTUNNEL_DOMAIN/health/ready"
```

Expect HTTP success without a certificate warning. Caddy obtains and renews the
certificate. Public DNS and inbound TCP 80/443 must reach this server. A local
success is not yet the external connection check. See
[Caddy's HTTPS requirements](https://caddyserver.com/docs/automatic-https).

After a successful pull and startup, run `docker logout ghcr.io` if you logged
in to GHCR. A later update or recovery can log in again. Running containers and
local images do not need the registry token to restart.

## 8. Create the first administrator

Replace `team-admin` with the selected username:

```shell
docker compose exec web /app/.venv/bin/python manage.py create_instance_admin team-admin
```

Enter and confirm the password in the terminal. Store it in the team's password
manager. An agent that cannot provide private terminal input must let the
operator perform this step. Every active admin account has full administrator
access, including the ability to create admins and agent credentials.

## Next step

Follow [Verify and connect the team](verify.md) from another computer.
If startup fails, use [fault checks](maintain.md#fault-checks). Keep the server,
volumes, secrets, and configuration intact while you correct the cause.
