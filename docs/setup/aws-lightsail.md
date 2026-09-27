# Set up an AWS Lightsail host

**No domain?** Read [Run without a domain](no-domain.md) for the IP address
and TLS changes to this path.

Use this guide to prepare one new Ubuntu server for StarTunnel. It uses the
Lightsail console. An agent with AWS access can follow the same choices and
checks. This guide creates paid cloud resources. The operator must select the
AWS account, Region, resource name, and host name, and authorize the cost. Do
not choose these values for the operator. Do not ask for authorization again
if the operator has already given it.

## Before you start

You need:

- An AWS account with permission to create and manage Lightsail instances,
  static IPs, disks, and instance firewalls. The account must have a working payment
  method and any required service limits must be clear.
- The operator's chosen AWS Region, resource name, and host name.
- Access to change DNS for the domain. The required record is
  `tunnel.example.com`; replace `example.com` with the operator's domain.
- The operator's current public IPv4 address in CIDR form, such as
  `198.51.100.24/32`, for SSH access.
- An SSH public key from the operator, or permission to create or download a
  Lightsail key pair and deliver its private key securely to the operator.
  Never put a private key in Git, a ticket, or a chat transcript.

For an uploaded key, use RSA as shown in the [setup index](README.md#gather-these-inputs).
Lightsail's import API requires an `ssh-rsa` public key. The server's SSH host
key can still use Ed25519; it is a different key used to identify the server.

Choose a Linux/Unix plan with **at least 2 vCPU, 4 GiB RAM, and 40 GiB SSD**.
This is a starting point, not a performance promise. Lightsail plans have
fixed resource sizes. Use a plan that meets or exceeds every minimum. Check
the current price and billing terms in the console before creation. Choose a
plan that includes public IPv4, not IPv6-only.

Choose Ubuntu 24.04 LTS **64-bit x86 (x86_64/amd64)**. Confirm that the
selected blueprint and plan are available in the operator's Region. Use a
fresh, dedicated instance. Do not reuse a host with unrelated services or
data.

Lightsail does not expose an encryption switch for the system disk included
with a plan. Create a new encrypted Lightsail block storage disk of at least
40 GB and attach it to the instance. This disk has an additional charge. Use
the shared storage guide to format and mount it before installing StarTunnel.
AWS documents that attached Lightsail disks are encrypted at rest by default.

## Create the instance

1. Open the [Lightsail console](https://lightsail.aws.amazon.com/) and select
   the operator's Region.
2. Choose **Create instance**. Under **Choose your instance location**, use
   the selected Region and an Availability Zone offered there.
3. Under **Choose your instance image**, select Linux/Unix, then Ubuntu 24.04
   LTS, 64-bit x86. Do not select an application blueprint or an ARM image.
4. Under **Choose your instance plan**, select a plan that meets or exceeds
   2 vCPU, 4 GiB RAM, and 40 GiB SSD. Confirm it includes public IPv4. Review
   the displayed price.
5. Under SSH key options, select the operator's uploaded key, create a
   custom key, or use the regional default key only if its private key can be
   securely delivered to the operator. Save the private key securely. AWS
   key pairs are regional.
6. Set the instance name to the name selected by the operator. Create only
   one instance.
7. Review all selections and the price, then create the instance.

## Configure the firewall

Open the instance, then its **Networking** tab. Lightsail has separate IPv4
and IPv6 firewalls. Edit both so that the public inbound rules are exactly:

| Protocol | Port | Source |
|---|---:|---|
| TCP | 22 | Operator's public IPv4 `/32` |
| TCP | 80 | All IPv4 addresses |
| TCP | 443 | All IPv4 addresses |

Lightsail's default Ubuntu firewall can allow SSH from every IPv4 address.
Remove that broad SSH rule and replace it with the operator's `/32`. For the
TCP 22 rule, select **Restrict to IP address**, enter the operator's `/32`,
and enable **Allow Lightsail browser SSH** if the operator will use the
browser SSH client. Do not add a separate public SSH rule. Do not add public
TCP 5432 or 8000. Keep IPv6 enabled while you create the static IPv4 address.
Remove every inbound IPv6 rule; do not add IPv6 access. Leave outbound access
enabled for package and software updates.

## Attach a static IPv4 address

Choose **Networking → Create static IP**, select the operator's Region and
this instance, enter the operator-selected address name, and create it. Check
the current price and billing terms before creation. Record the assigned
address. A static IP must be in the same Region as the instance.

## Create and attach an encrypted disk

In the Lightsail console, choose **Storage → Create disk**. Select the same
Availability Zone as the instance. Name the disk, set its size to at least
40 GB, and attach it to this instance. Review the additional disk price before
creation. Lightsail encrypts attached disks at rest by default. Record the
disk name and confirm that it is attached to this instance. Do not attach a
disk with data to preserve.

## Set the DNS record

At the operator's DNS provider, create or update:

| Type | Name | Value |
|---|---|---|
| A | `tunnel` | The Lightsail static IPv4 address |

Remove any stale `AAAA` record for `tunnel.example.com`. This guide does not
open the host to IPv6. Use a TTL of 300 seconds and DNS-only mode if the DNS
provider offers an HTTP proxy. Do not change nameserver or unrelated DNS records.

## Connect and check the host

Wait for the instance to reach a running state. Use the Lightsail browser SSH
client to read the server's SSH host key fingerprint:

```sh
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

Compare it with the fingerprint shown by your local SSH client on first
connection. Accept the host key only when the values match. If the server
does not have an Ed25519 host key, use the key type shown by `ssh-keygen -lf`.
Do not accept an unexpected changed host key.

Connect as `ubuntu` with the private key that matches the selected regional
Lightsail key pair. Replace this documentation IP address with the static IP
you recorded:

Restrict the private key file first, then connect from macOS or Linux:

```sh
chmod 600 /secure/path/operator-key.pem
ssh -i /secure/path/operator-key.pem ubuntu@198.51.100.10
```

Confirm that SSH works and that the image and architecture are correct:

```sh
whoami
sudo -v
uname -m
lsb_release -ds
df -h /
```

Expected results include user `ubuntu`, architecture `x86_64`, Ubuntu 24.04,
and a root disk of at least 40 GiB; formatting reduces its usable space.
The shared install guide needs an
Ubuntu SSH user with working `sudo`. It enters a root shell to install and run
Docker. Docker group membership is not needed.

Check the DNS answer from a system with DNS tools:

```sh
dig +short A tunnel.example.com
dig +short AAAA tunnel.example.com
```

The A answer must be the static IPv4 address. The AAAA answer must be empty.

## If you abandon this setup

Stop and delete only the instance, static IP, and key pair created for this
attempt, and only when they have no other users or dependencies. Detach and
delete only a data disk created for this attempt after confirming it has no
needed data. Release the static IP and delete any instance snapshots created
for this attempt if they are not needed; these resources can continue to incur
charges. Do not change shared firewalls, DNS zones, or other Lightsail
resources. If ownership is unclear, leave the resource in place and report
its name and state to the operator. Review the AWS Billing console for
remaining charges.

## Continue

After these checks pass, follow [Prepare encrypted storage](storage.md) to
format and mount the new disk. Then follow [Install StarTunnel](install.md).
For the setup choice index, see the [setup index](README.md).

## AWS documentation

- [Create a Lightsail instance](https://docs.aws.amazon.com/lightsail/latest/userguide/lightsail-how-to-create-instance.html)
- [Lightsail instance plans and capacity](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-bundles.html)
- [Lightsail instance firewall rules](https://docs.aws.amazon.com/lightsail/latest/userguide/understanding-firewall-and-port-mappings-in-amazon-lightsail.html)
- [Allow browser SSH with a restricted Lightsail firewall rule](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-editing-firewall-rules.html)
- [Set up SSH keys for Lightsail](https://docs.aws.amazon.com/lightsail/latest/userguide/lightsail-how-to-set-up-ssh.html)
- [Create and attach a static IP](https://docs.aws.amazon.com/lightsail/latest/userguide/lightsail-create-static-ip.html)
- [Create and attach a Lightsail disk](https://docs.aws.amazon.com/lightsail/latest/userguide/create-and-attach-additional-block-storage-disks-linux-unix.html)
- [Lightsail block storage encryption](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-faq-block-storage.html)
