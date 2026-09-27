# Run without a domain

[Setup index](README.md) → no domain

A team can use StarTunnel at `https://PUBLIC_IPV4` without buying a domain or
configuring DNS. Use a fixed public IPv4 address and a publicly trusted
certificate for that exact address. No VPN is required.

This page gives the changes needed for this configuration. It is not a tested,
complete installation recipe. The standard installation uses domain-based
Caddy certificate management; replacing the domain with an IP alone is not
sufficient proof of trusted HTTPS.

## Prepare the server

Choose [EC2](aws-ec2.md), [Lightsail](aws-lightsail.md),
[DigitalOcean](digitalocean.md), or [an existing Linux server](existing-linux.md).
Keep the storage, SSH, firewall, image, and secret instructions. Skip domain
and DNS checks. Use an Elastic IP, Lightsail static IP, DigitalOcean Reserved
IP, or another fixed public IPv4 address that reaches your server.

Keep TCP ports 80 and 443 reachable from the internet. Keep PostgreSQL and the
application port private. If the public IP changes, obtain a certificate for
the new address and update the instance settings and every client URL.

## Configure trusted HTTPS

One option is Certbot for certificate issuance and renewal, with Caddy loading
the resulting certificate files. Follow the official
[Certbot IP certificate instructions](https://letsencrypt.org/2026/03/11/shorter-certs-certbot/).
Use Certbot 5.4 or later for the webroot method. IP certificates use the
`shortlived` profile and expire after about six days.

The operator or setup agent must complete these changes:

1. Configure the HTTP challenge. With the webroot method, serve
   `/.well-known/acme-challenge/` from Certbot's challenge directory on port 80.
   Mount that directory into Caddy. The supplied Caddyfile does not do this.
   The standalone method instead needs port 80 free during each challenge;
   stopping Caddy for this causes a service interruption.
2. Test issuance with the staging service, then obtain a production certificate.
   A staging certificate is not publicly trusted.
3. Mount the certificate storage read-only into Caddy. If Certbot uses symbolic
   links from `live` to `archive`, make both paths available inside the container.
   Keep the private key accessible only to the processes that need it.
4. In a locally maintained copy of [the Caddyfile](../../docker/caddy/Caddyfile),
   add `tls /path/to/fullchain.pem /path/to/privkey.pem` inside the site block.
   Use paths inside the container. Preserve the existing proxy, limits, metrics
   restriction, and log filters. See [Caddy's TLS reference](https://caddyserver.com/docs/caddyfile/directives/tls).
   Use a local Compose override for the mounts and custom Caddyfile, and include
   that file in `COMPOSE_FILE` so later maintenance uses it too.
5. Enable automatic renewal and a deploy hook that makes Caddy load the renewed
   files. The supplied Caddyfile has `admin off`, so its default configuration
   cannot use the normal admin API reload. A Caddy container restart is one
   option, with a brief interruption. Monitor renewal failures and expiry.

In `production.env`, set `STARTUNNEL_DOMAIN` to the bare public IPv4 address,
without a scheme or path. Despite its name, the production Compose file uses
this value for the allowed host, HTTPS origin, base URL, and Caddy site address.
Use `https://PUBLIC_IPV4` as the instance URL throughout installation and client
setup. Do not disable certificate checks or use plain HTTP for team credentials.

## Verify and maintain

Run [the normal team verification](verify.md) against the IP URL from another
network. The browser and CLI must accept the certificate without a warning or
an insecure option. Complete the administrator login and two-agent exchange.
Test the renewal process and the deploy hook; confirm that Caddy serves the
new certificate after the hook runs.

Include the custom Compose and Caddy files, renewal configuration, and deploy
hook in your protected recovery materials. The standard backup script does not
collect these additions. During [recovery](backups.md), retain or reattach the
same public IP where possible, restore the TLS configuration, and obtain a new
certificate before reconnecting clients. Record the renewal owner and alert
method with the instance handoff.
