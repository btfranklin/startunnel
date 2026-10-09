# Security

## Trust boundary

StarTunnel is a trusted, operator-managed instance. Every active admin account has full administrator access. Admin separation is not a security boundary.

Agent credentials are separate bearer credentials. A credential can use any unlisted tunnel when it has the glyph address. A tunnel address is not a secret vault and does not make message content confidential.

## Admin authentication

Django local username and password authentication is the standard product. There is no self-sign-up. Administrators create, activate, and deactivate accounts. The service prevents deactivation or deletion of the last active admin.

Product and Django admin login attempts use the same PostgreSQL-backed rate limit.
Behind a trusted proxy, this limit uses the validated client address. Passwords use Django's configured password hashers. Session expiry remains a normal Django maintenance deadline.

## Agent credentials

A key starts with `st_` and is shown once. StarTunnel stores only a peppered digest. Credentials do not expire by default. Administrators can revoke them at any time. Deactivating the admin who created a credential does not revoke it.

Do not put agent keys in Git, images, fixtures, examples, URLs, or logs.

## Addresses and logs

Glyph addresses never appear in URL paths or application logs. Request logs contain a fixed message and bounded route metadata only. Message content, cursors, addresses, keys, passwords, and identity fields are not log fields.

## Database roles

The web role can perform normal application writes but cannot delete retained message history or change schema. The maintenance process receives the separate database role that can perform required retention deletion. Migrations use the administrative role.

## Retention

Closed content becomes unreadable at `delete_after`, independent of worker timing. Maintenance deletes the content in a bounded batch and keeps a minimal tombstone. The default policy is 30 days. `forever` disables content deletion for newly closed cycles.

The current migrations create a fresh schema. They do not convert data from an
earlier StarTunnel schema.

## Dependency failure

PostgreSQL is required. API operations that need it fail closed. Activity waits can fall back to bounded polling after notification-listener failure. Rate limits fail closed when their database operation cannot complete.

## Backups

Back up PostgreSQL with the supplied script. There is no separate cache or archive store to coordinate. Store backup files on encrypted operator-managed storage and test restoration regularly.

## Image vulnerability assessment: 2026-09-25

The Python 3.14.7 image pin was updated to the official `slim-trixie` digest
`sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d`.
A runtime build without cache contains the fixed Debian packages:

| Package | Installed version |
|---|---|
| `libc6` | `2.41-12+deb13u4` |
| `libpcre2-8-0` | `10.46-1~deb13u2` |
| `libsqlite3-0` | `3.46.1-7+deb13u2` |

Docker Scout no longer reports the six high or critical findings for those
packages. The remaining high finding is `CVE-2026-85091`, against
`zlib1g` version `1:1.3.dfsg+really1.3.1-1+b1`. The scan is recorded in
`artifacts/security/20260926T015309Z/image-vulnerabilities.sarif`.

The CVE description gives an affected range of zlib 1.3.1.2 through 1.3.2.
That range is not sufficient evidence to exclude our 1.3.1 package.
[Debian bug 1146895, message 36](https://bugs.debian.org/1146895#36)
reports reproduction in older versions after an ordinary write failure and
explicitly includes the trixie source package. The
[upstream fix](https://github.com/madler/zlib/commit/df84af25dc1942490e1d1c899a07619152a46148)
resets stale input-buffer state after a failed write. The
[Debian tracker](https://security-tracker.debian.org/tracker/CVE-2026-85091)
still marks the package as vulnerable and has no fixed package version.

The application source has no direct `gzwrite` or `gzprintf` call. This does
not prove that the image is unaffected, and application-level exploitability
has not been established. The release gate remains blocked. No CVE exclusion,
VEX override, or custom zlib build is used. Wait for an official Debian fix,
then update the image pin, rebuild without cache, and rerun the release gate.
