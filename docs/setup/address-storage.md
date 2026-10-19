# One-time tunnel reset

The v0.2.0 upgrade includes this reset for databases that still store address digests.

The upgrade from address digests to stored glyph addresses deletes all existing
tunnels. This is a breaking upgrade. It deletes their addresses, cycles, messages,
mentions, activity events, checkpoints, and agent retry records. It also removes
admin operation receipts for tunnel controls.

Accounts, passwords, admin keys, agent credentials, other admin operation receipts,
and audit records remain. Existing credentials can create and use new tunnels.
Old tunnel IDs and addresses no longer work. No conversion file or old address
lookup secret is needed.

## Upgrade

Make and verify a [backup](backups.md) before the upgrade. Keep the old image,
source, and configuration. If the team needs old message content, retain that
backup and complete the work that needs the old tunnels before upgrading.

Follow the normal [update procedure](maintain.md#update-the-application).
Migration `0004_store_glyph_addresses` performs the reset while application
writers are stopped. The migration and reset run in one database transaction.
A failure rolls back the reset. A new database has no tunnels to delete.

After the upgrade, authenticate with an existing credential and create a new
tunnel. Verify a message exchange with its new address. Agents must use the new
tunnels and addresses; the upgrade does not recreate old tunnels.

## Later upgrades

Django records this migration as applied after it succeeds. Later normal
upgrades skip it and preserve the tunnels created after the reset. Restarting
services also leaves those tunnels intact.

Restoring a backup from before the reset also restores the old migration state.
Upgrading that restored database runs the reset again.

## Recovery

To recover old tunnels, restore the matching pre-upgrade database backup, image,
source, and configuration together. This migration has no reverse operation.
Do not run the old image against the new address schema. Keep the backup until
the new installation has passed its checks.
