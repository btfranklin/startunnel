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

For an in-place Caddy configuration edit, reload without stopping the service:

```shell
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --force
```

A checkout or editor can replace the mounted file. After a release checkout,
re-create Caddy as shown in the upgrade steps below.

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

These steps update the server and its team's clients to v0.3.0. Use the
published release's `release.json` and matching tagged guides. The supported
published predecessor is v0.2.0. For v0.1.0 or older installations, first use
the [tagged v0.2.0 update guide](https://github.com/btfranklin/startunnel/blob/v0.2.0/docs/setup/maintain.md#update-the-application).
Custom or unversioned deployments must check their installed source and
migration history as described below.

This release has no database migration. It preserves the v0.2.0 schema, data,
and credentials. The server requires official CLI v0.3.0 or later. Older copies
receive a clear update error before an API operation runs. Update each client
after the server update; the download remains available to older clients.

1. Select v0.3.0 from [GitHub Releases](https://github.com/btfranklin/startunnel/releases/tag/v0.3.0).
   Read its notes, configuration changes, database compatibility, and rollback
   instructions. Download its attached `release.json` outside the checkout.
   Keep the source commit and immutable image digest together.
2. Make a fresh [verified backup](backups.md), test its restore, and copy the
   backup and recovery secrets outside the server. Record the installed image
   digest and its source revision label. Keep the old image available and plan
   a short maintenance window.
   Keep all existing secret files with the recovery copy.
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
   web containers, pulls the new image, verifies its source label, and shows
   its read-only database migration plan in a temporary container. Web does
   not need to be running; PostgreSQL must be available. The preview does not
   change configuration, stored data, or running services.

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

   If you use the included Caddy service, re-create it so its bind mount reads
   the new configuration. This causes a short TLS interruption:

   ```shell
   docker compose up -d --no-deps --force-recreate caddy
   ```

   There is no automatic rollback. After an interrupted attempt, check the
   printed snapshot, configuration, migrations, and service state. Retry from
   the old configuration only if the database and writers remain unchanged.
6. Tell each CLI user to update the client from the upgraded instance. Run on
   each agent and administrator host:

   ```shell
   startunnel update
   startunnel version
   startunnel config
   startunnel me
   ```

   Set `STARTUNNEL_BASE_URL` to that instance's origin before the update. Admin
   users run `startunnel admin config` and `startunnel admin me`. Keep existing
   credentials. Update copies in agent images and job artifacts too. The server
   cannot update files on those machines. Updates remain explicit.

   If the installed script has no `update` command, download and install the
   client as shown in [the client guide](../../cli/README.md#update-the-client).
   Record the client version, source revision, and executable path with the
   server version and source commit. Source builds report `unreleased` and
   remain unverified. The file checksum identifies the exact client copy.

   `startunnel config` checks local settings without a request; it does not
   prove server access or key validity. `startunnel schema` and
   `startunnel admin schema` provide offline command and response definitions.
   Compare them with the upgraded instance's `/api/v1/openapi.json`.
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
