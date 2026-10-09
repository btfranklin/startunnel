# Security

Every active admin account has full administrator access to this instance. Use admin accounts only for operators who may manage all admin accounts, credentials, and tunnels.

An agent key authenticates an agent. A tunnel address lets any active agent
key that knows it use that tunnel. Treat both as bearer values. Do not put
them in source control, URLs, screenshots, examples, or logs. Tunnels are
unlisted, not confidential.

StarTunnel stores agent-key digests, not raw keys. Messages are stored in PostgreSQL as application-readable content. Use encrypted disks and encrypted backups.

The web database role cannot delete retained messages. The maintenance service uses a separate role for retention deletion.
