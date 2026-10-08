# Set up StarTunnel for your team

Start here if your team needs a shared StarTunnel instance. These guides create
one server with a public HTTPS address. People and agents can connect from
different networks without a VPN. Human accounts and agent keys control access.
PostgreSQL has no public port.

## Choose a configuration

| Your team has | Use this guide | Result |
|---|---|---|
| An AWS account and wants standard AWS infrastructure | [AWS EC2](aws-ec2.md) | One EC2 instance with encrypted EBS storage and an Elastic IP |
| An AWS account and wants a simpler server plan | [AWS Lightsail](aws-lightsail.md) | One instance, a static IP, and an encrypted data disk |
| No preferred cloud provider, or a DigitalOcean account | [DigitalOcean](digitalocean.md) | One Droplet, a Reserved IP, and an encrypted Volume |
| A suitable Linux server and administrator access | [Existing Linux server](existing-linux.md) | The same application stack on that server |
| Only wants to try StarTunnel on one computer | [Local trial](../../README.md#run-it) | A localhost instance; other computers cannot connect |

The four shared configurations use Ubuntu 24.04 LTS, x86-64, Docker Compose,
PostgreSQL, and Caddy for HTTPS. Start with 2 vCPU, 4 GiB RAM, and at least
40 GB SSD storage. This is a starting allocation, not a measured capacity
guarantee. Measure your team's workload before you reduce it or promise a
specific number of agents. These are single-server installations; a server
failure causes an outage until recovery.

## Choose an address

- **With a domain:** use a subdomain that your team controls, such as
  `tunnel.example.com`. Follow the DNS and automatic HTTPS steps as written.
- **No domain:** use a fixed public IPv4 address. Read
  [Run without a domain](no-domain.md) before installation. This path needs
  an IP address certificate and additional certificate renewal configuration.
  The page gives guidance; it is not a complete automated installation recipe.

Both choices work with the hosting options above. The website setup prompts
send agents to this index so they can select the address path as well as the host.

## Follow the complete path

1. Use [Get the application image](image.md) to select an official release.
   Custom images can be built on a workstation or build host.
   If your operator already supplies an image digest and its source commit,
   use those values instead.
2. Follow one provider guide from the table to prepare the server and, if used, DNS.
3. Follow [Install StarTunnel](install.md) on the server.
4. Follow [Verify and connect the team](verify.md) from another computer.
5. Follow [Back up and recover](backups.md) before the team uses real data.

Each guide links to the next step. Later, use [Maintain the instance](maintain.md)
for restarts, updates, and fault checks. The [operations reference](../operations.md)
owns application settings and service behavior.

## Gather these inputs

- A team member who owns the instance and its costs.
- The selected account, region, server name, and allowed monthly spend.
  Check the provider's current estimate. Include the server, public IP,
  storage, snapshots, data transfer, registry, and backup storage.
- Either a domain that the team controls with permission to change its DNS,
  or a fixed public IPv4 address and the [no-domain TLS setup](no-domain.md).
  Replace the example address in every step.
- SSH access to the selected server and a private workstation for passwords
  and keys. Keep the SSH private key on that workstation.
- Access to pull the selected official public image, or permission to publish
  and pull a custom image. [The image guide](image.md) explains both paths.
- Encrypted storage outside the server for backups and recovery secrets.

On a macOS or Linux workstation, find the public IPv4 address for the SSH rule
with `curl -4 https://checkip.amazonaws.com`. Add `/32` to the result. If you
need a new SSH key, run `ssh-keygen -t rsa -b 4096 -f ~/.ssh/startunnel` and choose
a passphrase. Do not overwrite an existing key file. Upload only
`~/.ssh/startunnel.pub`; keep `~/.ssh/startunnel` private. The provider guides
use example key paths that you must replace with this actual path.
RSA works with all four paths, including
[Lightsail's RSA key import](https://docs.aws.amazon.com/cli/latest/reference/lightsail/import-key-pair.html).

No OpenAI account, OAuth application, or VPN is required. GitHub can hold your
source and application image. GitHub membership does not grant access to a
StarTunnel instance. Each human account has full instance administrator access;
each agent gets its own key.

## Instructions for an agent

Use the user's selected configuration. If they have not selected one, explain
the table and ask which host or provider they want. Select the domain or
no-domain address path too. Do not assume the team can change DNS. Ask only for missing
decisions and access. Use existing authorization to create the agreed resources;
do not select a paid account, region, or plan without that authorization.

Read the complete selected path before you run commands. Commands say whether
they run on the workstation or server. Stop at a failed check, correct its
cause, and repeat that check before the next step. Never invent an image digest,
source commit, domain, or successful result. Use secure local input for secrets;
do not request them in chat or include them in command arguments or reports.

Report the instance as ready only after external HTTPS, administrator login,
agent authentication, the two-agent exchange, and the backup restore check
pass. Give the owner the URL, selected configuration, source commit, image
digest, backup location, and any unresolved check. Do not include credentials
or tunnel addresses in that report.

## Scope of the evidence

The guides use the repository's production files and link to provider
documentation. Repository checks do not prove that a particular cloud account,
DNS record, image publication, or public certificate works. Run the checks in
your selected guide on the actual installation. These instructions do not
claim a completed deployment on all four providers.
