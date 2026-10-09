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

Omit `sudo -i` if already root. Always load this file. It selects production
Compose files, the correct project, and the Caddy profile. Plain commands from
an unconfigured shell can select development settings or another stack.

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

## Recover administrator access

Use another available admin key or password account first. If remote access is
lost, run the recovery command from the configured server shell:

```shell
docker compose exec -T web /app/.venv/bin/python manage.py recover_instance_admin team-admin --key-file /tmp/startunnel-recovery.key
```

Select an existing account and a new private container file. Recovery activates
that account and issues a non-expiring key; it does not restore revoked keys.
Transfer the file through an approved private channel, keep it at mode `0600`,
and remove the temporary container copy after transfer. Verify `admin me` and
`admin doctor`, then inspect the `admin.recovered` audit event. The command does
not recover deployment secrets or a lost database. Use the
[admin quickstart](../user/admin-quickstart.md) for client access.

## Update the application

1. Select a published version from [GitHub Releases](https://github.com/btfranklin/startunnel/releases).
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
   readable by containers. Reload `production.env` as shown above.
4. Preview the upgrade from that matching checkout:

   ```shell
   python3 scripts/upgrade_production.py /absolute/path/release.json
   ```

   The helper validates the record, clean checkout, production configuration,
   secret files, and installed image's source/version labels. Versioned
   installations must appear in the release's `supported_from` list.
   Unversioned installations must have an ancestor source commit and matching
   applied migration files in an unchanged prefix. Forward migration and recovery
   evidence must cover the exact installed source. A missing revision label
   requires recovering the
   original build record and a separately reviewed manual upgrade; do not
   guess a commit or bypass compatibility checks.
5. Once the preview passes and the backup checks in step 2 are complete, apply:

   ```shell
   python3 scripts/upgrade_production.py /absolute/path/release.json --apply --backup-verified
   set -a
   . /opt/startunnel/production.env
   set +a
   ```

   `--backup-verified` is the operator's attestation, not an automated restore
   check. The helper pulls the official image and checks its revision before
   stopping writers. It preserves a private copy of the old configuration,
   changes only the application image reference, removes the completed old
   migration container, and waits for the production stack. It never deletes
   database volumes or regenerates secrets. There is no automatic rollback.
   If a prior attempt left recovery files, inspect them and the actual stack
   state before retrying; the helper refuses to overwrite them.
6. Repeat [external verification](verify.md), including administrator authentication and a two-agent
   exchange, and make a new backup. A container health check alone does not
   prove the complete deployment. Record the new version, commit, and digest.

### Existing ManageAI and other pre-release installations

The first release establishes a supported baseline; it does not convert the
older migration graph. Use the installed image's source revision to determine
eligibility with the preview above. A matching applied migration prefix and verified forward migration permit existing
accounts, agent keys, messages, and secrets to remain in place. If they differ,
stop and arrange an explicit data conversion tested against a restored copy.
The ManageAI installation's actual commit and database must be inspected before
claiming its upgrade is compatible.

For private/custom images, retain the [manual image build path](image.md#build-a-custom-image)
and follow the same backup, compatibility, matching-checkout, migration, and
verification requirements. The official helper deliberately accepts only the
official image repository.

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
