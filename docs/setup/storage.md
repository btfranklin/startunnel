# Prepare encrypted storage

Use this guide only when the server's root disk does not meet the storage
encryption requirement. The Lightsail and DigitalOcean provider guides create
and attach a new encrypted disk first. This guide formats that blank disk and
mounts it at `/srv/startunnel`. It does not install or configure Docker.

The disk must be new, empty, and at least 40 GB. Formatting erases the selected
device. Stop if you cannot prove which attached device is the new disk, if it
has a filesystem or partition table, or if it contains data. Do not format the
server's root disk or any existing disk.

EC2 hosts with encrypted root EBS and existing servers whose Docker data disk
is already encrypted do not need a new volume. For those hosts, create the
directory in the root shell:

```sh
sudo -i
mkdir -p /srv/startunnel
```

For those encrypted-root hosts, stop here and continue with
[Install StarTunnel](install.md). Do not run the disk-formatting steps below.

## Create and attach the provider disk

Follow the provider guide to create a **new blank disk of at least 40 GB** and
attach it to the selected server. Keep it in the same Availability Zone as a
Lightsail instance, or the same region as a DigitalOcean Droplet. Use the
provider's encrypted attached storage. Select manual formatting and mounting
if the provider offers automatic setup. Record the disk name and the instance
to which you attach it. Do not attach a disk that has data to preserve.

## Identify the new Linux device

Connect to the server and enter a root shell:

```sh
sudo -i
```

List every disk, filesystem, UUID, and mount point:

```sh
lsblk -e7 -o NAME,PATH,SIZE,TYPE,FSTYPE,UUID,MOUNTPOINTS,MODEL,SERIAL
```

Match the new device to the disk name and size shown by the provider. Record
the full `PATH` value for that disk. It must have type `disk`, no child
partitions, no filesystem, no UUID, and no mount point. Never guess the name
from an example. Common names differ by provider and instance.

Enter the actual path from your `lsblk` output. The checks below stop if the
path is not one whole, blank disk of at least 40 GB:

```sh
set -euo pipefail
read -r -p 'Full PATH for the new blank disk: ' DEVICE
test -b "$DEVICE"
test "$(lsblk -dnro TYPE -- "$DEVICE")" = disk
test "$(lsblk -nrpo PATH -- "$DEVICE" | wc -l | tr -d ' ')" = 1
test "$(lsblk -bndo SIZE -- "$DEVICE")" -ge 40000000000
test -z "$(lsblk -nrpo FSTYPE -- "$DEVICE" | tr -d '[:space:]')"
test -z "$(lsblk -nrpo UUID -- "$DEVICE" | tr -d '[:space:]')"
test -z "$(lsblk -nrpo MOUNTPOINTS -- "$DEVICE" | tr -d '[:space:]')"
SIGNATURES=$(wipefs --no-act --noheadings --output TYPE -- "$DEVICE")
test -z "$SIGNATURES"
lsblk -f -- "$DEVICE"
```

If any command fails, stop. Do not work around a failed check. Re-check the
provider attachment and the `lsblk` output. If the path, disk size, partitions,
filesystem, or signatures are not clear, ask the operator before proceeding.

## Format and mount the new disk

Confirm that `/srv/startunnel` does not already exist. If it exists, stop and
check for existing data or mounts. Then format only the device you confirmed
above. The confirmation requires the full device path a second time:

```sh
test ! -e /srv/startunnel
read -r -p "Type $DEVICE to confirm this new disk is empty and may be erased: " CONFIRM
test "$CONFIRM" = "$DEVICE"
mkfs.ext4 -L startunnel-data "$DEVICE"
```

Get the new filesystem UUID, add a persistent mount to `/etc/fstab`, and mount
it now. This entry intentionally does not use `nofail`; a missing data disk
must not allow Docker to write application data to the unencrypted root disk.

```sh
UUID=$(blkid -s UUID -o value "$DEVICE")
test -n "$UUID"
mkdir -p /srv
mkdir /srv/startunnel
if grep -Fq "UUID=$UUID" /etc/fstab; then
  echo 'This UUID is already in /etc/fstab. Stop and inspect the file.' >&2
  exit 1
fi
if awk '$2 == "/srv/startunnel" { found=1 } END { exit !found }' /etc/fstab; then
  echo 'The /srv/startunnel mount already exists in /etc/fstab. Stop and inspect the file.' >&2
  exit 1
fi
printf 'UUID=%s /srv/startunnel ext4 defaults 0 2\n' "$UUID" >> /etc/fstab
systemctl daemon-reload
mount /srv/startunnel
chmod 700 /srv/startunnel
```

Confirm the mount uses the new filesystem and UUID:

```sh
findmnt --mountpoint /srv/startunnel --output SOURCE,UUID,TARGET,FSTYPE
df -h /srv/startunnel
lsblk -f
```

The source in `findmnt` must be the filesystem with the UUID printed by
`blkid`, mounted at `/srv/startunnel`. Stop if the mount check fails. Do not
install Docker or create application data until the mount is correct.

## Continue

Return to the selected provider guide, then follow [Install StarTunnel](install.md).
The install guide configures Docker to store its data under
`/srv/startunnel/docker` and requires the mount at boot before Docker starts.
For the setup choice index, see the [setup index](README.md).

## Provider documentation

- [Lightsail disks](https://docs.aws.amazon.com/lightsail/latest/userguide/create-and-attach-additional-block-storage-disks-linux-unix.html)
- [Lightsail disk encryption](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-faq-block-storage.html)
- [DigitalOcean Volumes](https://docs.digitalocean.com/products/volumes/how-to/create/)
- [DigitalOcean Volume encryption](https://docs.digitalocean.com/products/volumes/details/features/)
