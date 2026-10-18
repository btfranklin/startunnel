# Address storage conversion

StarTunnel stores the full canonical glyph address in each address row. This
includes current and retired addresses. New databases need no conversion file.

An older database stores HMAC digests. Migration `0004_store_glyph_addresses`
needs the original addresses to replace those digests. A digest cannot be
converted back to an address. The migration stops if an original is missing
or does not match. It does not rotate addresses or delete address history.

## Prepare the old database

1. Stop web writers and keep them stopped through the conversion. Keep the
   maintenance process available until the normal upgrade stops it.
2. Let existing reply, close, start, and rollover operation receipts expire.
   Their maximum lifetime is 24 hours. The migration refuses to change their
   request format while an unexpired receipt remains. Tunnel creation receipts
   keep the same format and do not need to expire.
3. Make and verify a database backup. Keep the old image and configuration.
4. Collect every original address, including retired address generations,
   from the operator's private records. Match each address to its address-row
   UUID. Do not put this data in Git, a command argument, or a log.

If an original address is missing, keep the old database and report the missing
row to the owner. Do not invent an address or delete the row to pass migration.

## Supply the conversion file

Create a private JSON file with mode `0600`. It has this structure. Replace
the placeholders with the old lookup secret and the exact canonical glyph
addresses. Use address-row UUIDs, not tunnel UUIDs.

```json
{
  "address_secret": "<old STARTUNNEL_ADDRESS_SECRET value>",
  "addresses": {
    "<address-row UUID>": "<24-glyph address>"
  }
}
```

Set `STARTUNNEL_ADDRESS_MIGRATION_FILE` to its path for the migration process
only. For a container, mount the private file read-only and use its container
path. The migration validates all rows against the old digests before it
writes any address. The database transaction removes the digest column and
replaces its immutable-address trigger with the direct-address rule.

Test the conversion on a restored database first. During the normal
[update procedure](maintain.md#update-the-application), keep writers stopped
and select the reviewed new image in `.env.prod`. With PostgreSQL running,
run the migration service with the private file mounted:

```shell
sudo chown 10001:10001 /srv/startunnel/address-conversion.json
sudo chmod 0600 /srv/startunnel/address-conversion.json
docker compose --env-file .env.prod -f compose.yaml -f compose.prod.yaml run --rm --no-deps --volume /srv/startunnel/address-conversion.json:/run/startunnel-addresses.json:ro --env STARTUNNEL_ADDRESS_MIGRATION_FILE=/run/startunnel-addresses.json migrate
```

Use the actual private host path for the left side of the volume mount.
The file must be readable by the image's user, UID `10001`.
Resume the update procedure after the migration succeeds.
Confirm that existing addresses still open their
tunnels, retired addresses remain unavailable, credentials still work, and
new writes succeed. Remove the conversion file and its environment setting
after successful verification. The old address lookup secret is no longer a
runtime setting or a mounted service secret.

## Recovery

This migration has no reverse conversion. To return to the old image, restore
the matching pre-conversion backup and its configuration. Do not run the old
image against the new address schema. Keep a verified backup until the new
installation has passed its checks.
