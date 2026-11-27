# Back up and recover

[Setup index](README.md) · [Verification](verify.md) · [Maintenance](maintain.md)

Keep a database backup, all generated secret files, the production configuration,
and the matching source commit and image. The database alone is not sufficient:
the cryptographic secrets are required to authenticate existing keys and replay operations.
Keep the image in its registry. A server snapshot is an additional recovery
option, not a replacement for a verified database backup.

## 1. Create a recovery key on the workstation

Install [age](https://github.com/FiloSottile/age) on a trusted workstation:
`brew install age` on macOS, or `sudo apt-get install age` on Ubuntu.
Run these commands in a new private directory outside any repository:

```shell
umask 077
mkdir startunnel-recovery
cd startunnel-recovery
age-keygen -o recovery-key.txt
age-keygen -y recovery-key.txt > backup-recipients.txt
```

Keep `recovery-key.txt` in the team's password manager or encrypted recovery
storage. Do not put it on the production server. Only `backup-recipients.txt`
is public. It contains the `age1...` recipient used to encrypt backups.

On the **server**, use the [production shell](maintain.md#start-each-server-session),
then run:

```shell
printf 'Paste the public age1 recipient: '
IFS= read -r BACKUP_RECIPIENT
printf '%s\n' "$BACKUP_RECIPIENT" > /srv/startunnel/backup-recipients.txt
chmod 600 /srv/startunnel/backup-recipients.txt
install -d -m 700 /srv/startunnel/backups
```

Paste only the public line from the workstation's `backup-recipients.txt`.
Do not paste the recovery key. Keep this recipient file outside Git.

## 2. Install the backup command

The command below makes an online PostgreSQL dump while `web` and
`maintenance` stay available. It records a dump checksum and saves an encrypted
archive. Routine backup creation does not restore a database.

In the server's root shell, create the command:

```shell
cat > /usr/local/sbin/startunnel-backup <<'SCRIPT'
#!/bin/bash
set -euo pipefail
umask 077
cd /opt/startunnel
set -a
. /opt/startunnel/production.env
set +a
exec python3.15 scripts/backup_production.py \
  --output-directory /srv/startunnel/backups \
  --env-file /opt/startunnel/production.env \
  --recipients /srv/startunnel/backup-recipients.txt
SCRIPT
chmod 700 /usr/local/sbin/startunnel-backup
/usr/local/sbin/startunnel-backup
```

Expect a completed encrypted backup path. Record that path. The archive
contains the database dump, the active configuration and Compose files, the
secret files, and an installation record with the running image identities.
Keep the archive on encrypted storage. If another operation holds the instance
lock, the backup exits without publishing a new archive. Check the date of the
last verified off-host copy and retry after maintenance.

## 3. Copy it outside the server and test decryption

In the server's root shell, stage **only the encrypted file** for your SSH
user. Replace the example backup filename with the completed path. For EC2
and Lightsail the user is `ubuntu`; on DigitalOcean it is normally `root`.
Use the actual home directory for an existing-server account:

```shell
install -o ubuntu -g ubuntu -m 600 \
  /srv/startunnel/backups/20260927T120000Z.tar.age \
  /home/ubuntu/startunnel-backup.tar.age
```

For a root SSH account, use `-o root -g root` and
`/root/startunnel-backup.tar.age` instead. In the **workstation** recovery
directory, copy the file using the same SSH key and address as your provider
guide. Replace the username and IP as needed:

```shell
scp -i /secure/path/operator-key ubuntu@198.51.100.10:startunnel-backup.tar.age .
age --decrypt -i recovery-key.txt startunnel-backup.tar.age > recovered.tar
tar -tf recovered.tar
mkdir restore-check
tar -xf recovered.tar -C restore-check
cd restore-check/database.backup
shasum -a 256 -c manifest.sha256
```

On an Ubuntu workstation, use `sha256sum -c manifest.sha256` in place of the
last `shasum` command. Expect `postgres.dump: OK`. The archive must contain `database.backup`,
`source-commit`, `production.env`, and `secrets` with all generated secret files. Never print
the secret contents. Store the encrypted archive outside the production
server, separately from the recovery key. Remove only this test's decrypted
`recovered.tar` and `restore-check` directory after verification. Your
workstation's storage must be encrypted while it holds decrypted data.

## 4. Set the backup schedule

After the first backup and decryption check succeed, install a daily schedule
on the server.
This example uses 03:15 in the server's time zone; check `timedatectl` and select
a time that suits the team:

```shell
cat > /etc/cron.d/startunnel-backup <<'EOF'
15 3 * * * root /usr/local/sbin/startunnel-backup >> /var/log/startunnel-backup.log 2>&1
EOF
chmod 644 /etc/cron.d/startunnel-backup
systemctl is-active cron
```

If cron is absent, install it with `apt-get install --yes cron`, then run
`systemctl enable --now cron`. Check the first scheduled run's exit output,
backup timestamp, and service health. Log output contains operational status,
not database contents or keys.

This schedule creates local encrypted backups. **It does not copy them outside
the server.** Assign an owner to repeat the copy step daily, or connect the
encrypted output directory to the team's existing backup transfer service.
Verify that a new archive reaches that destination. Do not call off-server
backup automatic until that transfer is configured and checked.

Start with seven daily and four weekly copies, adjusted to the team's data
policy and available storage. Remove only dated archives that have a verified
copy outside the server. Check disk space daily.

## 5. Run restore drills separately

Test a restore after initial setup, at least once a month, and after database
changes. Use a separate recovery server when possible. Restore drills use
additional disk space and database capacity; they do not run in the nightly
backup command.

Decrypt a recent off-server archive as in step 3. On the recovery server, use
the matching checkout and its Compose settings, then run:

```shell
scripts/verify-startunnel-restore.sh /absolute/path/restore-check/database.backup
```

The command restores into a temporary database and removes that database after
the check. Record the backup timestamp and the result. Also test the full
replacement-server recovery below. Keep the restore check required before a
destructive application upgrade; see [maintenance](maintain.md).

After an off-host copy passes its restore check, record that verification on
the server. A deferred backup uses the marker time to report the age of the
last off-host verification.

```shell
touch /srv/startunnel/backups/.off-host-verified
```

## Recover on a replacement server

Use these steps on a replacement server with a fresh PostgreSQL volume.
Keep the old server stopped during final recovery so two instances cannot
accept writes under the same domain.

1. On the recovery workstation, decrypt the chosen archive as shown above.
   Read `source-commit` and the image reference in `production.env`. Keep secret
   files private. These are the source and image for recovery.
2. Prepare a replacement server with the same provider guide. Use the original
   hostname. During a recovery drill, use a separate test hostname and keep the
   original DNS unchanged. Do not copy data to an unencrypted disk.
3. Complete installation steps 1–5 with the recorded source commit and image.
   Pull that image using installation step 6, but **skip secret generation**.
   Do not start `web`, `maintenance`, or `migrate` yet.
   If the recorded checkout does not have the replacement restore command,
   get the checkout that contains it. Set `STARTUNNEL_RECOVERY_TOOLS` to the
   absolute path of that checkout. Keep the recorded checkout and image for
   the application.
4. Copy the decrypted `recovered.tar` over SSH to a private directory on the
   replacement server's encrypted disk. For example, create
   `/srv/startunnel/recovery-upload` with `install -d -o ubuntu -g ubuntu -m 700`
   on the server, then use `scp` from the workstation to that directory. Use
   the actual SSH account. Do not upload `recovery-key.txt`.

In the replacement server's **production root shell**, extract the archive:

```shell
set -euo pipefail
install -d -m 700 /srv/startunnel/recovery
tar -xf /srv/startunnel/recovery-upload/recovered.tar \
  -C /srv/startunnel/recovery --no-same-owner
```

Use the archived `production.env` as the replacement configuration. If the
archive contains `installation.json`, that file maps each selected Compose file's
original path to its archive copy under `config/`. Preserve the file order,
profiles, and image digests it records. Restore custom overrides in their
original relative locations so Compose can resolve their references. If a
host path changes, update the selected file list and the affected references.
Keep files already supplied by the matching checkout. On a recovery drill,
change the domain and any explicit base URL or host settings to the drill
hostname. Use the provider guide to check the replacement host settings.

Install the reviewed configuration and load it. Set
`STARTUNNEL_SECRETS_DIR` in that file to the absolute secret directory on
this server. In this example, the reviewed copy is
`/srv/startunnel/recovery/production.env`.

```shell
cd /opt/startunnel
install -m 600 /srv/startunnel/recovery/production.env /opt/startunnel/production.env
set -a
. /opt/startunnel/production.env
set +a
test ! -e "$STARTUNNEL_SECRETS_DIR"
test ! -L "$STARTUNNEL_SECRETS_DIR"
cp -a /srv/startunnel/recovery/secrets "$STARTUNNEL_SECRETS_DIR"
chown 10001:10001 "$STARTUNNEL_SECRETS_DIR"/*
chmod 700 "$STARTUNNEL_SECRETS_DIR"
chmod 600 "$STARTUNNEL_SECRETS_DIR"/*
recovery_tools="${STARTUNNEL_RECOVERY_TOOLS:-$PWD}"
python3.15 "$recovery_tools/scripts/production_lock.py" -- \
  "$recovery_tools/scripts/restore-startunnel-replacement.sh" \
  /srv/startunnel/recovery/database.backup
python3.15 scripts/start_production.py
docker compose up -d --no-build --wait --wait-timeout 180
```

Stop at any error. The command checks the dump and restores it into an empty
temporary database. It verifies the restricted database role over TCP before
it exchanges database names in one transaction. It keeps the bootstrap
database under the name it prints.

If the switch stops without a confirmation, keep the application stopped.
Use the names from the command output to check which transaction result
PostgreSQL has:

```shell
docker compose exec -T postgres psql -X --username startunnel_admin \
  --dbname postgres --command 'SELECT datname FROM pg_database ORDER BY datname;'
```

- If `startunnel` and the printed `startunnel_restore_...` database exist,
  the switch did not commit. Resolve the error and repeat the restore
  command. The prior staged database can remain until verification.
- If `startunnel` and the printed `startunnel_bootstrap_...` database exist,
  the switch committed. With the writers still stopped, run the TCP role
  probe below. Start the application only after the probe passes.
- For any other result, keep the writers stopped and inspect the selected
  Compose project and databases. Preserve the encrypted archive.

```shell
docker compose exec -T -e PGHOST=127.0.0.1 postgres \
  /usr/local/bin/startunnel-check-runtime-role startunnel
```

Point the selected hostname at the replacement IP before the HTTPS check. For a drill, use only the test
hostname. Complete [external verification](verify.md) and confirm an existing
agent key still works. Then establish the backup schedule on this host.
Remove the decrypted recovery files after verification. Keep the encrypted
archive and matching registry image until the retention policy permits removal.
