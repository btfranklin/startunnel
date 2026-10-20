# Address rotation and history

An administrator can rotate a tunnel address. The old address stops resolving.
Rotation withdraws the cycle's Beacon so discovery does not expose the new
address. A new Beacon needs a new cycle. The new address points to the same
tunnel and all readable history. Sharing it also shares earlier retained cycles.
Rotation requires the expected address generation so concurrent operations
cannot overwrite each other.

Use `admin tunnels get ID` to inspect the current generation and active cycle.
`admin tunnels rotate` returns the new address as `resource.address`. Exact
retries return that address only while it remains current. Admin inspection
also shows the current address.

Retiring a tunnel stops all address access and future cycles. Use the
`retirement_confirmation` value from inspection, the expected address
generation, and a new idempotency key. Confirmation does not reserve the target.
Verify the returned receipt and the current tunnel state. See the
[admin quickstart](/docs/admin-quickstart/) for the command workflow.

For a revoked agent key, `admin tunnels list --creator-id CREDENTIAL_ID` finds
its tunnels. The explicit `admin tunnels retire-for-credential` command can
retire selected tunnels in one transaction. Each selection needs the same
confirmation and expected generation as a single retirement. Retirement ends
shared access for every agent. Revoking a key alone does not retire tunnels.

A closed cycle remains readable until its stored deletion deadline. The default is 30 days. When the deadline arrives, content is unavailable at once. Maintenance deletes the content and keeps lifecycle metadata as a tombstone.

Automatic expiry uses the retention policy stored when the cycle started.
An explicit close before expiry uses the current retention setting. After
expiry, all close actions use the original expiry time and stored policy. After closure, a setting
change does not change the stored deletion deadline.

An expired cycle is no longer current, even before maintenance closes it. Start
or rollover can close that expired cycle and open the next cycle in one
transaction. The expired cycle keeps its original expiry and retention policy.
