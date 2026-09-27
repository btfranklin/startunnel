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

## Update the application

1. Read the selected release's notes and database compatibility requirements.
   The current initial schema does not convert old StarTunnel databases.
2. Make and copy a [verified backup](backups.md) outside the server. Record the
   current commit and image digest. Plan a short maintenance window.
3. Use [the image guide](image.md) to build the selected new commit. Keep both
   the previous and new images available.
4. On the server, run `git status --short`. Resolve tracked changes before
   changing commits. Run `umask 022`, fetch the source, then check out the new
   image's exact commit with `git checkout --detach COMMIT`. Run `umask 077`
   afterward. Mounted source files must remain readable by the containers.
5. Edit only `STARTUNNEL_APP_IMAGE` in `/opt/startunnel/production.env` to the
   new digest. Load the file again as shown above. Log in to the registry with
   a read token if needed, then run `docker pull "$STARTUNNEL_APP_IMAGE"`.
6. Stop the application writers with `docker compose stop web maintenance`.
   Run `python3 scripts/start_production.py --edge`, then
   `docker compose up -d --no-build --wait --wait-timeout 180`.
7. Repeat [external verification](verify.md) and make a new backup. Log out of
   the registry if its token is no longer needed.

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
