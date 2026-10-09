# Address rotation and history

An administrator can rotate a tunnel address. The old address stops resolving. The new address points to the same tunnel and history. Rotation requires the expected address generation so two operators cannot silently overwrite each other.

Use `admin tunnels get ID` to inspect the current generation and active cycle.
`admin tunnels rotate` requires a new private output file. Share that file only
through the approved private channel. Admin inspection does not disclose the
current address.

Retiring a tunnel stops all address access and future cycles. Use the
`retirement_confirmation` value from inspection, the expected address
generation, and a new idempotency key. Confirmation does not reserve the target.
Verify the returned receipt and the current tunnel state. See the
[admin quickstart](/docs/admin-quickstart/) for the command workflow.

A closed cycle remains readable until its stored deletion deadline. The default is 30 days. When the deadline arrives, content is unavailable at once. Maintenance deletes the content and keeps lifecycle metadata as a tombstone.

Changing the current retention setting does not change cycles that already captured a policy.
