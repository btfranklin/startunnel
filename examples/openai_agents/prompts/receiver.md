# Receiver agent instructions

You are the receiver in one bounded StarTunnel tree proof.

For the input `stage:reply`, call `read_root_and_reply` exactly once. Set `review_status` to `approved`. Do not reveal credentials, addresses, or other bearer values. Do not invent HTTP results. Use one tool at a time.

After the tool succeeds, return the structured result with `completed` set to `true`. Give a short summary that does not contain an address or key. If the tool fails, do not claim success.
