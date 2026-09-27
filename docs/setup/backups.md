# Back up and recover

[Setup index](README.md) · [Verification](verify.md) · [Maintenance](maintain.md)

Keep a database backup, all eight secret files, the production configuration,
and the matching source commit and image. The database alone is not sufficient:
the cryptographic secrets are required to use existing keys and addresses.
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

The command below pauses `web` and `maintenance` while it makes the database
dump, then restarts them. Schedule a short daily interruption when the team
can tolerate it. It also checks a restore in a temporary database on the same
PostgreSQL server. This check uses additional disk space and database capacity.

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
exec 9>/run/lock/startunnel-backup.lock
flock -n 9 || exit 1
backup_root=/srv/startunnel/backups
work=$(mktemp -d "$backup_root/.work.XXXXXX")
output="$backup_root/$(date -u +%Y%m%dT%H%M%SZ).tar.age"
trap 'rm -rf -- "$work"' EXIT
test ! -e "$output"
scripts/backup-startunnel.sh "$work/database.backup"
scripts/verify-startunnel-restore.sh "$work/database.backup"
git rev-parse HEAD > "$work/source-commit"
cp /opt/startunnel/production.env "$work/production.env"
tar -C "$work" -cf - database.backup source-commit production.env \
  -C /srv/startunnel secrets |
  age -R /srv/startunnel/backup-recipients.txt > "$output.partial"
mv "$output.partial" "$output"
printf 'Verified encrypted backup: %s\n' "$output"
SCRIPT
chmod 700 /usr/local/sbin/startunnel-backup
/usr/local/sbin/startunnel-backup
```

Expect a successful restore check and an encrypted backup path. Record that
path. A `.partial` file is not a completed backup. If the command fails, check
that application services restarted and correct the failure before retrying.
Do not rotate secrets or update the application while a backup is running.

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
`source-commit`, `production.env`, and `secrets` with eight files. Never print
the secret contents. Store the encrypted archive outside the production
server, separately from the recovery key. Remove only this test's decrypted
`recovered.tar` and `restore-check` directory after verification. Your
workstation's storage must be encrypted while it holds decrypted data.

## 4. Set the backup schedule

After the first full check succeeds, install a daily schedule on the server.
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
copy outside the server. Check disk space daily. Repeat the restore check
after updates, and test full replacement-server recovery periodically.

## Recover on a replacement server

These steps restore into a **new, empty database**. Do not run them against the
active database. Keep the old server stopped during final recovery so two
instances cannot accept divergent writes under the same domain.

1. On the recovery workstation, decrypt the chosen archive as shown above.
   Read `source-commit` and the image reference in `production.env`. Keep secret
   files private. These are the source and image for recovery.
2. Prepare a replacement server with the same provider guide. Use the original
   hostname. During a recovery drill, use a separate test hostname and keep the
   original DNS unchanged. Do not copy data to an unencrypted disk.
3. Complete installation steps 1–5 with the recorded source commit and image.
   Pull that image using installation step 6, but **skip secret generation**.
   Do not start `web`, `maintenance`, or `migrate` yet.
4. Copy the decrypted `recovered.tar` over SSH to a mode-`0700` directory on the
   replacement server's encrypted disk. For example, create
   `/srv/startunnel/recovery-upload` with `install -d -o ubuntu -g ubuntu -m 700`
   on the server, then use `scp` from the workstation to that directory. Use
   the actual SSH account. Do not upload `recovery-key.txt`.

In the replacement server's **production root shell**, run:

```shell
set -euo pipefail
install -d -m 700 /srv/startunnel/recovery
tar -xf /srv/startunnel/recovery-upload/recovered.tar \
  -C /srv/startunnel/recovery --no-same-owner
test ! -e /srv/startunnel/secrets
cp -a /srv/startunnel/recovery/secrets /srv/startunnel/secrets
chown 10001:10001 /srv/startunnel/secrets/*
chmod 700 /srv/startunnel/secrets
chmod 600 /srv/startunnel/secrets/*
cd /srv/startunnel/recovery/database.backup
sha256sum -c manifest.sha256
cd /opt/startunnel
docker compose up -d --no-build --wait --wait-timeout 180 postgres
docker compose exec -T postgres pg_restore --exit-on-error \
  --username startunnel_admin --dbname startunnel --no-owner --no-acl \
  < /srv/startunnel/recovery/database.backup/postgres.dump
docker compose exec -T postgres psql --set=ON_ERROR_STOP=1 \
  --set=database_name=startunnel --username startunnel_admin --dbname startunnel \
  --file /usr/local/share/startunnel/runtime-grants.sql
python3 scripts/start_production.py --edge
docker compose up -d --no-build --wait --wait-timeout 180
```

Stop at any error. In particular, do not continue if the secret directory
already exists or the database is not empty. Point the selected hostname at
the replacement IP before the HTTPS check. For a drill, use only the test
hostname. Complete [external verification](verify.md) and confirm an existing
agent key still works. Then establish the backup schedule on this host.
Remove the decrypted recovery files after verification. Keep the encrypted
archive and matching registry image until the retention policy permits removal.
