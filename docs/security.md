# Security

This guide defines trust boundaries and security controls for an instance.

## Trust boundary

StarTunnel is a trusted, operator-managed instance. Every active admin account has full administrator access. Admin separation is not a security boundary.

Agent credentials are separate bearer credentials. A credential can use any unlisted tunnel when it has the glyph address. A tunnel address is not a secret vault and does not make message content confidential.

## Admin authentication

Admin accounts use optional Django passwords and named `sta_` API keys. Each
active account has full instance authority. There is no self-sign-up. The CLI,
admin API, and browser call the same domain services.

Admin credentials store only peppered digests, display prefixes, and lifecycle
metadata. Keys authenticate only to admin endpoints. Ordinary `st_` keys cannot
use those endpoints. Deactivating an account revokes its admin keys and blocks
browser access. Reactivation requires new admin keys.

Database locks protect the last usable access path. One active password account
or non-expiring admin key must remain. Server bootstrap and recovery commands
write new keys to private files and record safe audit events. Remote endpoints
cannot bootstrap an unauthenticated administrator.

Audit events identify the admin, credential or browser channel, target, request,
and result. They never contain raw keys, passwords, addresses, or payloads.
Admin operation receipts are owner-scoped and support exact retries for 24 hours.
A revoked credential or retired address cannot be restored by replay.

Product and Django admin login attempts use the same PostgreSQL-backed rate limit.
Behind a trusted proxy, this limit uses the validated client address. Passwords use Django's configured password hashers. Session expiry remains a normal Django maintenance deadline.

## Agent credentials

A key starts with `st_`. Creation returns it through a private file or browser download. StarTunnel stores only a peppered digest. Credentials do not expire by default. Administrators can revoke them at any time. Deactivating the admin who created a credential does not revoke it.

Do not put agent keys in Git, images, fixtures, examples, URLs, or logs.

## Addresses and logs

Glyph addresses never appear in URL paths or application logs. Request logs contain a fixed message and bounded route metadata only. Message content, cursors, addresses, keys, passwords, and identity fields are not log fields.

## Database roles

The web role can perform normal application writes but cannot delete retained message history or change schema. The maintenance process receives the separate database role that can perform required retention deletion. Migrations use the administrative role.

## Retention

Closed content becomes unreadable at `delete_after`, independent of worker timing. Maintenance deletes the content in a bounded batch and keeps a minimal tombstone. The default policy is 30 days. `forever` disables content deletion for newly closed cycles.

Forward migrations add admin credentials, operation receipts, and audit
attribution to the matching initial baseline. They preserve existing accounts,
passwords, agent credentials, messages, and historical audit records. Other
migration graphs need a separately reviewed conversion. See [release policy](releases.md).

## Dependency failure

PostgreSQL is required. API operations that need it fail closed. Activity waits can fall back to bounded polling after notification-listener failure. Rate limits fail closed when their database operation cannot complete.

## Backups

Back up PostgreSQL with the supplied script. There is no separate cache or archive store to coordinate. Store backup files on encrypted operator-managed storage and test restoration regularly.

## Dependency audit: 2026-10-08

The release candidate audit on 2026-10-08 also found `CVE-2026-101918` and
`CVE-2026-102275` in PyJWT 2.14.0, an optional live-agent dependency. The lockfile
now selects the patched PyJWT 2.15.1 release. This dependency update does not
resolve the separate image vulnerability below. Debian's tracker still listed
that issue as unfixed when checked on 2026-10-08.

## Alpine runtime migration: 2026-10-08

The application now uses the official Python 3.14.8 Alpine 3.23 image, pinned
by registry digest. The build updates Alpine packages and requires
`zlib>=1.3.2-r1`, the version Alpine identifies as fixing `CVE-2026-85091` in its
[security database](https://secdb.alpinelinux.org/v3.23/main.json). This replaces
the Debian runtime described in the historical assessment below; no vulnerability
exclusion or custom zlib build is used.

Production dependencies have Python 3.14 amd64 musl wheels. Browser and other
test-helper images retain a separate Debian base because Playwright requires
glibc. Application services in those stacks run Alpine. Release readiness still
requires the complete security, system, and load proof on the candidate digest;
availability of patched packages alone does not establish that evidence.

The current image gate uses checksummed Trivy 0.75.0 for CycloneDX inventory and
SARIF vulnerability reports. It scans the exact candidate digest, blocks high and
critical findings including unfixed vulnerabilities, and needs no Docker Hub
account. The Docker Scout findings below remain historical evidence.

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
VEX override, or custom zlib build was used for this assessment. The Alpine
runtime migration above supersedes waiting for an official Debian fix.
