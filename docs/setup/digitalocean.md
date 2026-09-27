# Set up a DigitalOcean host

**No domain?** Read [Run without a domain](no-domain.md) for the IP address
and TLS changes to this path.

Use this guide to prepare one new Ubuntu server for StarTunnel. It uses the
DigitalOcean Control Panel. An agent with DigitalOcean access can follow the
same choices and checks. This guide creates paid cloud resources. The operator
must select the account, region, project, resource name, and host name, and
authorize the cost. Do not choose these values for the operator. Do not ask
for authorization again if the operator has already given it.

## Before you start

You need:

- A DigitalOcean account with permission to create Droplets, Volumes, firewalls, and
  Reserved IPs. The account must have a working payment method and enough
  account limits for the chosen resources.
- The operator's chosen region, project, Droplet name, and host name.
- Access to change DNS for the domain. The required record is
  `tunnel.example.com`; replace `example.com` with the operator's domain.
- The operator's current public IPv4 address in CIDR form, such as
  `198.51.100.24/32`, for SSH access.
- The operator's SSH public key, or permission to create a key pair and add
  its public key to the DigitalOcean team. Keep the private key only with the
  operator. Never put a private key in Git, a ticket, or a chat transcript.

The starting capacity is **2 vCPU, 4 GiB RAM, and 40 GiB SSD**. Treat this as a
starting point, not a performance promise. Choose a Droplet plan with at least
these resources and an x86_64 CPU. Plan names and available sizes can vary by
region and account. Check the current price in the Control Panel before
creation.

Choose the Ubuntu 24.04 LTS **x86_64/amd64** image. Do not use an ARM image.
Use a fresh, dedicated Droplet. Do not reuse a host with unrelated services or
data.

DigitalOcean documents at-rest encryption for DigitalOcean Volumes and their
snapshots. Its Droplet creation page does not offer a boot-disk encryption
switch. Create a new encrypted Volume of at least 40 GB and attach it to the
Droplet. This Volume has an additional charge. Use the shared storage guide to
format and mount it before installing StarTunnel. Do not claim that the
Droplet boot disk has a customer-selectable encryption setting.

## Create the Droplet

1. Open the [DigitalOcean Control Panel](https://cloud.digitalocean.com/) and
   select the operator's project.
2. Choose **Create → Droplets**.
3. Under **Choose Region**, select the operator's region.
4. Under **Choose an image**, select **OS → Ubuntu → 24.04 (LTS) x64**.
   Confirm the image says x64 or amd64.
5. Under **Choose a Droplet Size**, select an x86_64 plan with at least 2 vCPU,
   4 GiB RAM, and 40 GiB SSD. Review the price. If no plan in the selected
   region meets every minimum, stop and ask the operator to choose another
   region or capacity.
6. Under **Choose Authentication Method**, select **SSH Key** and add the
   operator's public key. If the key is not listed, use **Add SSH Key** to
   upload the public key. Do not select password authentication.
7. In **Networking**, keep **Public IPv4** selected. Clear **Public IPv6**.
   Do not enable other paid options unless the operator selected them.
8. Set quantity to one. Set the hostname to the operator-selected name and
   confirm the correct project. Review all selections and the total price.
9. Create the Droplet.

Before attaching the cloud firewall, open the Droplet's **Access → Launch
Droplet Console**. New Ubuntu Droplets include the console agent. Read and
record the SSH host fingerprint through this authenticated provider console:

```sh
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

Keep that record for the local SSH check below, then close the console and
configure the cloud firewall. The Droplet Console uses network SSH and can be
blocked after you restrict port 22. Do not install the application before
applying the firewall.

## Create and attach a Reserved IPv4

After the Droplet is ready, choose **Create → Reserved IP**. Select **IPv4**,
then assign it to this Droplet in the same datacenter region. Review any
displayed price before creation. Record the Reserved IP. An assigned Reserved
IPv4 can be reassigned to a replacement Droplet in the same datacenter.

## Create and attach an encrypted Volume

Choose **Create → Volume Block Storage**. Select the Droplet's region and
project. Create a new blank Volume of at least 40 GB and attach it to this
Droplet. Review the additional cost before creation. If the create form offers
automatic formatting or mounting, select manual setup. Record the Volume name
and confirm that it is attached to this Droplet. Do not attach a Volume with
data to preserve.

## Configure the cloud firewall

Choose **Networking → Firewalls** and create a firewall for this Droplet.
Set inbound rules to exactly:

| Protocol | Port | Source |
|---|---:|---|
| TCP | 22 | Operator's public IPv4 `/32` |
| TCP | 80 | All IPv4 addresses |
| TCP | 443 | All IPv4 addresses |

Remove any default SSH rule that permits access from all addresses. Do not
add public TCP 5432 or 8000. Do not add IPv6 sources. Keep the default
outbound allow rules so the host can install updates and software. Apply the
firewall to this Droplet and confirm the Droplet is listed as protected.

## Set the DNS record

At the operator's DNS provider, create or update:

| Type | Name | Value |
|---|---|---|
| A | `tunnel` | The Reserved IPv4 address |

Remove any stale `AAAA` record for `tunnel.example.com`. This guide does not
open the host to IPv6. Use a TTL of 300 seconds and DNS-only mode if the DNS
provider offers an HTTP proxy. Do not change nameserver or unrelated DNS records.

## Connect and check the host

Compare the host fingerprint from the Droplet Console with the fingerprint
shown by your local SSH client. Accept it only when the values match. Keep
SSH limited to the operator's IP. Do not accept an unexpected changed key.

Connect as `root` with the private key that matches the uploaded public key.
Replace this documentation IP address with the Reserved IP you recorded:

Restrict the private key file first, then connect from macOS or Linux:

```sh
chmod 600 /secure/path/operator-key
ssh -i /secure/path/operator-key root@198.51.100.10
```

Confirm that SSH works and that the image and architecture are correct:

```sh
whoami
sudo -v
uname -m
lsb_release -ds
df -h /
```

Expected results include user `root`, architecture `x86_64`, Ubuntu 24.04,
and a root disk of at least 40 GiB; formatting reduces its usable space.
The shared install guide needs an
Ubuntu SSH user with working `sudo`. It enters a root shell to install and run
Docker. Docker group membership is not needed.

Check the DNS answer from a system with DNS tools:

```sh
dig +short A tunnel.example.com
dig +short AAAA tunnel.example.com
```

The A answer must be the Reserved IP. The AAAA answer must be empty.

## If you abandon this setup

Delete only the Droplet, Reserved IP, firewall, and SSH key created for this
attempt, and only when they have no other users or dependencies. Delete only
an encrypted Volume or snapshot created for this attempt after confirming it
has no needed data. Unassign and delete the Reserved IP so it does not
continue to incur charges. Do not delete or edit shared firewalls, VPCs, DNS
zones, SSH keys, or other account resources. If ownership is unclear, leave
the resource in place and report its name and state to the operator. Review
the Billing page for remaining charges.

## Continue

After these checks pass, follow [Prepare encrypted storage](storage.md) to
format and mount the new Volume. Then follow [Install StarTunnel](install.md).
For the setup choice index, see the [setup index](README.md).

## DigitalOcean documentation

- [Create a Droplet](https://docs.digitalocean.com/products/droplets/how-to/create/)
- [DigitalOcean production-ready Droplet setup](https://docs.digitalocean.com/products/droplets/getting-started/recommended-droplet-setup/)
- [Create a cloud firewall](https://docs.digitalocean.com/products/networking/firewalls/how-to/create/)
- [Configure firewall rules](https://docs.digitalocean.com/products/networking/firewalls/how-to/configure-rules/)
- [Create and assign a Reserved IP](https://docs.digitalocean.com/products/networking/reserved-ips/getting-started/quickstart/)
- [Upload and use SSH keys](https://docs.digitalocean.com/products/droplets/how-to/add-ssh-keys/)
- [DigitalOcean Volume encryption](https://docs.digitalocean.com/products/volumes/details/features/)
- [Create and attach a DigitalOcean Volume](https://docs.digitalocean.com/products/volumes/how-to/create/)
- [Connect to Droplets with the Droplet Console](https://docs.digitalocean.com/products/droplets/how-to/connect-with-console/)
