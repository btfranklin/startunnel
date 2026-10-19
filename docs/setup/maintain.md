# Maintain the instance

[Setup index](README.md) · [Installation](install.md) · [Backups](backups.md)

## Start each server session

Run in the **server SSH terminal**:

```shell
sudo -i
cd /opt/startunnel
set -a
. /opt/startunnel/production.env
set +a
```

Omit `sudo -i` if already root. This example uses `production.env`. If your
deployment uses another config file, load that file instead and pass its path
to the upgrade helper with `--env-file`. The loaded `COMPOSE_FILE`,
`COMPOSE_PROJECT_NAME`, and `COMPOSE_PROFILES` values select the stack. The
included Caddy service needs the `edge` profile. An external TLS proxy does
not need that profile. Plain commands from an unconfigured shell can select
development settings or another stack.

## Check and restart

```shell
docker compose ps --all
docker compose exec web /app/.venv/bin/python manage.py inspect_runtime
df -h /srv/startunnel
docker stats --no-stream
curl --fail --silent --show-error "https://$STARTUNNEL_DOMAIN/health/ready"
```

Inspect runtime counters without printing credentials, messages, or addresses.
Check free disk space and backup age daily. Keep enough space for at least one
additional database dump and a replacement application image.

For a service restart:

```shell
docker compose restart web maintenance
docker compose up -d --no-build --wait --wait-timeout 180
```

This causes a short interruption. Do not use `down --volumes` or a Docker volume
prune for a restart. They can remove durable data. Container restart policies
start services after a server reboot; verify their health after every reboot.

To apply Caddy configuration changes or renewed certificate files without a
service interruption, run:

```shell
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --force
```

The admin API uses container loopback. The supplied Compose file does not publish
its port, 2019. Keep that port private.

## Recover administrator access

Use another available admin key or password account first. If remote access is
lost, run the recovery command from the configured server shell:

```shell
docker compose exec -T web /app/.venv/bin/python manage.py recover_instance_admin team-admin --key-file /tmp/startunnel-recovery.key
```

Select an existing account and a new private container file. Recovery activates
that account and issues a non-expiring key; it does not restore revoked keys.
Transfer the file through an approved private channel, keep it private,
and remove the temporary container copy after transfer. Verify `admin me` and
`admin doctor`, then inspect the `admin.recovered` audit event. The command does
not recover deployment secrets or a lost database. Use the
[admin quickstart](../user/admin-quickstart.md) for client access.

## Update the application

These steps describe planned v0.2.0. Apply them only after v0.2.0 is published
and its release record lists the installed version as supported. The current
v0.1.0 release record does not contain these pending changes. For published
v0.1.0, use its [tagged maintenance guide](https://github.com/btfranklin/startunnel/blob/v0.1.0/docs/setup/maintain.md).
Test current source on a new instance with a
[custom image](image.md#build-a-custom-image).

For a database that still stores address digests, this upgrade performs a
[one-time tunnel reset](address-storage.md). It deletes all existing tunnels
and their message data. Accounts and credentials remain. Read that notice and
make a verified backup before applying the update. No conversion file is needed.

For databases that already store glyph addresses, removing the unused
per-message content digest preserves existing messages and payloads. The
application no longer stores a per-message content digest.

1. After v0.2.0 is published, select it from [GitHub Releases](https://github.com/btfranklin/startunnel/releases).
   Read its notes, configuration changes, database compatibility, and rollback
   instructions. Download its attached `release.json` outside the checkout.
   Keep the source commit and immutable image digest together.
2. Make a fresh [verified backup](backups.md), test its restore, and copy the
   backup and recovery secrets outside the server. Record the installed image
   digest and its source revision label. Keep the old image available and plan
   a short maintenance window.
3. Run `git status --short` and resolve tracked changes. Run `umask 022`,
   `git fetch --tags origin`, and `git checkout --detach COMMIT`, replacing
   `COMMIT` with the full source commit from the release record. Run `umask 077`
   afterward. The checkout must match the new image; mounted files must remain
   readable by containers. Reload the selected config file as shown above.
4. Preview the upgrade from that matching checkout:

   ```shell
   python3 scripts/upgrade_production.py /absolute/path/release.json
   ```

   For another config name, add `--env-file /absolute/path/team.env` to both
   commands. The preview checks the release record, clean checkout, selected
   Compose model, secret files, and installed image labels. It checks existing
   web containers and runs a migration check in a temporary container from the
   old image. Web does not need to be running; PostgreSQL must be available.

   Versioned installations must appear in `supported_from`. Unversioned
   installations need an ancestor source commit and unchanged migration files.
   Test forward migration and recovery from the exact installed source before
   claiming compatibility. If the revision label is missing, recover the build
   record and arrange a reviewed manual upgrade.
5. Once the preview passes and the backup checks in step 2 are complete, apply:

   ```shell
   python3 scripts/upgrade_production.py /absolute/path/release.json --apply --backup-verified
   set -a
   . /opt/startunnel/production.env
   set +a
   ```

   `--backup-verified` confirms that you restored a fresh backup and copied it
   outside the server. The helper does not perform that check.

   The helper pulls and checks the new image before it stops writers. It saves
   the old configuration in a unique mode `0600` file and prints the path, then
   replaces the image setting and starts the stack. It retains earlier
   snapshots. Database volumes and secret files remain in place. A pending
   database migration can change stored data, including the one-time reset.

   There is no automatic rollback. After an interrupted attempt, check the
   printed snapshot, configuration, migrations, and service state. Retry from
   the old configuration only if the database and writers remain unchanged.
6. Tell each CLI user to install the client from the upgraded instance. The
   command installation is planned for v0.2.0. Existing v0.1.0 script users
   must perform this initial installation on each CLI machine:

   ```shell
   curl -fSLo star_tunnel.py "$STARTUNNEL_BASE_URL/downloads/star_tunnel.py"
   python3 star_tunnel.py install
   export PATH="$HOME/.local/bin:$PATH"
   startunnel --help
   startunnel version
   ```

   Use that instance's URL in `STARTUNNEL_BASE_URL`. Update copies in agent
   images and job artifacts too. The server cannot update files on those
   machines. Keys remain valid; users do not need new keys for a client update.
   See [the client guide](../../cli/README.md#update-the-client).
   After this first installation, later server upgrades need only
   `startunnel update` on each agent host, followed by `startunnel me` or
   `startunnel admin me`. The instance URL selects the update source.
   Record `startunnel version` after the update with the server version and
   source commit. Default custom builds can report `unreleased`; their file
   checksum still identifies the exact client copy.
   The updated client also provides `startunnel schema` for offline agent
   command definitions. Compare request types and limits with the upgraded
   instance's `/api/v1/openapi.json`.
   Argument errors now return JSON on stderr with code `usage_error` and exit
   status `2`. Update callers that parse the old argument-error text.
7. Repeat [external verification](verify.md), including administrator authentication and a two-agent
   exchange, and make a new backup. A container health check alone does not
   prove the complete deployment. Record the new version, commit, and digest.

### Existing pre-release installations

The first release establishes a supported baseline; it does not convert the
older migration graph. Use the installed image's source revision to determine
eligibility with the preview above. A matching applied migration prefix and
verified forward migration preserve accounts, credentials, and deployment
secrets. If the one-time tunnel reset is pending, it deletes existing tunnels
and messages. Later upgrades preserve new tunnels after that reset has been
applied. If the migration graphs differ, stop and arrange an explicit data
conversion tested against a restored copy.
Inspect the actual installed commit and database before claiming upgrade
compatibility.

For private/custom images, use the [manual image build path](image.md#build-a-custom-image)
and follow the same backup, compatibility, matching-checkout, migration, and
verification requirements. Write a release record from the matching source
checkout with `python3 scripts/release_record.py --write VERSION IMAGE --platform
linux/arm64` if you use an ARM64 image. The writer defaults to `linux/amd64`.
Check the record and image labels before an upgrade.

If a migration fails, leave the application writers stopped and inspect the
failure. An old image is not necessarily compatible with a new database.
Recover the previous image, matching checkout, secrets, and database backup
together on a replacement host when rollback requires database recovery.
Use [the recovery steps](backups.md#recover-on-a-replacement-server).

## Host updates

Install Ubuntu security updates regularly. Use `apt-get update` and
`apt-get upgrade` in a maintenance window after a verified backup. Read package
prompts before accepting changes to Docker or SSH. Reboot when Ubuntu requires
it, then verify the encrypted data mount, services, and external HTTPS.
Keep DNS and certificate ports available for Caddy renewal.

## Fault checks

| Symptom | Check and action |
|---|---|
| SSH timeout | Check the static IP, server state, and the operator's current public IP in the SSH rule. Use the provider console to correct the rule. |
| Image pull denied | Check registry login, token expiry, organization SSO, package read permission, and the full digest reference. |
| Secret file error | Check `stat -c '%a %u:%g %n' /srv/startunnel/secrets/*`. Each file must be `600 10001:10001` for the documented image. Check the parent mount. Do not print file contents. |
| PostgreSQL authentication failure after restart | Preserve the existing secret files and database. A newly generated password does not change a password already stored in PostgreSQL. Recover the matching secrets before you restart. |
| Migration exited nonzero | Read bounded migration logs, confirm source/image alignment, and check that this is a new or compatible database. Do not delete the volume to retry. |
| HTTPS timeout | Check A/AAAA records, DNS-only mode, TCP 80/443 ingress, and Caddy state. No AAAA record should remain for the IPv4-only path. |
| Certificate error | Wait for DNS propagation, check certificate-authority errors in Caddy logs, and check for a restrictive DNS CAA record. Do not disable certificate verification. |
| `admin me` rejected | Check the instance origin, private file ownership and mode, and exclusive admin-key configuration. Verify that the key and owner are active. Use server recovery if all admin access is lost. |
| `admin doctor` fails | Inspect the safe database and maintenance checks. Check containers and database access before a mutation. |
| Login rejected or CSRF error | Use the exact configured HTTPS hostname. Check `STARTUNNEL_DOMAIN`, then recreate services after a configuration correction. |
| `me` returns 401 with a key | Check the target instance and whether the credential is active. Revoke and replace a lost key. Never print the key to debug it. |
| Storage or health failure | Check `findmnt /srv/startunnel`, free space, and container state. Reattach the correct volume before starting Docker. |

For bounded production logs, after loading `production.env`, run:

```shell
docker compose logs --no-log-prefix --tail 100 migrate web maintenance postgres caddy
```

Inspect logs privately. Do not paste keys, identity data, payloads, or addresses
into reports. Do not use the development-only `scripts/container-logs.sh` for
this production stack.

For additional settings and service behavior, see [Operations](../operations.md).
