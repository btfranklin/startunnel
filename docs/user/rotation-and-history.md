# Address rotation and history

An administrator can rotate a tunnel address. The old address stops resolving. The new address points to the same tunnel and history. Rotation requires the expected address generation so two operators cannot silently overwrite each other.

Retiring a tunnel stops all address access and future cycles.

A closed cycle remains readable until its stored deletion deadline. The default is 30 days. When the deadline arrives, content is unavailable at once. Maintenance deletes the content and keeps lifecycle metadata as a tombstone.

Changing the current retention setting does not change cycles that already captured a policy.
