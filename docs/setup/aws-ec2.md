# Set up an AWS EC2 host

**No domain?** Read [Run without a domain](no-domain.md) for the IP address
and TLS changes to this path.

Use this guide to prepare one new Ubuntu server for StarTunnel. It uses the
AWS console. An agent with AWS access can follow the same choices and checks.
This guide creates paid cloud resources. The operator must select the AWS
account, Region, resource name, and host name, and authorize the cost. Do not
choose these values for the operator. Do not ask for authorization again if
the operator has already given it.

## Before you start

You need:

- An AWS account with permission to create EC2 instances, security groups,
  Elastic IP addresses, and EC2 key pairs. The account must have a working
  payment method and any required account or service limits must be clear.
- The operator's chosen AWS Region, resource name, and host name.
- Access to change DNS for the domain. The required record is
  `tunnel.example.com`; replace `example.com` with the operator's domain.
- The operator's current public IPv4 address in CIDR form, such as
  `198.51.100.24/32`, for SSH access.
- An SSH public key from the operator, or permission to create an EC2 key pair
  and deliver its private key securely to the operator. Never put a private
  key in Git, a ticket, or a chat transcript.

The starting capacity is **2 vCPU, 4 GiB RAM, and 40 GiB SSD**. Treat this as a
starting point, not a performance promise. Select an x86_64 instance type with
at least 2 vCPU and 4 GiB RAM. Select a 40 GiB or larger general-purpose SSD
EBS volume. The live AWS price and available instance types depend on the
selected Region and account. Review the estimate in the console before launch.

Use an Ubuntu Server 24.04 LTS **64-bit x86 (x86_64/amd64)** AMI. Do not use an
ARM image. Use a fresh, dedicated instance. Do not reuse a host with unrelated
services or data.

## Create the host

If the account has no suitable public subnet, first open **VPC → Create VPC**.
Choose **VPC and more**, one Availability Zone, one public subnet, zero private
subnets, no NAT gateway, and no VPC endpoints. Enable DNS resolution and DNS
hostnames. Use a non-overlapping IPv4 CIDR approved for this account. Review
the preview, which must include an internet gateway and a public route table,
then create the VPC. Select that VPC and public subnet below. See
[Create a VPC](https://docs.aws.amazon.com/vpc/latest/userguide/create-vpc.html)
for the console fields. Do not alter a shared private subnet to make it public.

1. Open the [EC2 console](https://console.aws.amazon.com/ec2/) and select the
   operator's Region.
2. Choose **Instances**, then **Launch instances**. Set the instance name to
   the name selected by the operator.
3. Under **Application and OS Images**, choose Ubuntu Server 24.04 LTS and
   confirm the architecture is 64-bit x86.
4. Under **Instance type**, choose an x86_64 type with at least 2 vCPU and
   4 GiB RAM. Check the displayed price.
5. Under **Key pair**, select the operator's imported key pair, or create a key
   pair and securely give its private key to the operator. Do not lose the
   private key. AWS does not let you download it again after creation.
6. Under **Network settings**, choose a VPC and subnet that can route to the
   public internet. A default VPC and public subnet are a simple path when
   they exist and meet the operator's requirements. Do not assume they exist.
   For an existing VPC, verify all of these items before launch:
   - The subnet route table has `0.0.0.0/0` routed to an internet gateway
     attached to that VPC.
   - The instance will receive a public IPv4 address, or an Elastic IP will be
     associated after launch.
   - The subnet network ACL allows the required inbound traffic and return
     traffic. Network ACLs are stateless; restrictive ACLs must allow return
     traffic on ephemeral ports. Do not change a shared ACL without the
     operator's direction.
7. Create a new security group for this host, or use a dedicated group that
   has only these inbound rules:

   | Protocol | Port | Source |
   |---|---:|---|
   | TCP | 22 | Operator's public IPv4 `/32` |
   | TCP | 80 | `0.0.0.0/0` |
   | TCP | 443 | `0.0.0.0/0` |

   Do not add IPv6 access. Do not allow public TCP 5432 or 8000. Leave normal
   outbound access enabled so the host can install updates and software.
8. Under **Configure storage**, set the root EBS volume to 40 GiB or more,
   use SSD-backed `gp3` where available, and set **Encrypted** to **Yes**.
   Use the account's default AWS-managed EBS key unless the operator selects a
   different key and confirms access to it.
9. Review the full configuration and price. Launch only the one instance
   selected by the operator.

## Assign a stable IPv4 address

An instance's automatically assigned public IPv4 address can change after
stop/start. Allocate an Elastic IP in the same Region and associate it with
this instance. Check the current Elastic IP charge before allocation; AWS may
charge for public IPv4 addresses. Record the Elastic IP for DNS.

In **Network & Security → Elastic IPs**, choose **Allocate Elastic IP address**
with Amazon's address pool. Select the new address, then **Actions → Associate
Elastic IP address**. Choose **Instance**, the new instance ID, and its primary
private IP. Confirm the association and check that the instance details show
this public IP.

## Set the DNS record

At the operator's DNS provider, create or update:

| Type | Name | Value |
|---|---|---|
| A | `tunnel` | The instance's Elastic IP |

Remove any stale `AAAA` record for `tunnel.example.com`. This guide does not
open the host to IPv6. Use a TTL of 300 seconds and DNS-only mode if the DNS
provider offers an HTTP proxy. Do not change nameserver or unrelated DNS records.

## Connect and check the host

Wait for the instance status checks to pass. In the EC2 console, select the
instance and open **Actions → Monitor and troubleshoot → Get system log** (or
the instance's console output) to find its SSH host key fingerprint when AWS
provides one. Compare the fingerprint before accepting the SSH host key. The
EC2 browser SSH client may also provide a way to read the host fingerprint,
but its network access must work with the security group above. Do not widen
port 22 to all sources for this check. If no trusted console path can show the
fingerprint, stop and obtain it from the operator before accepting the SSH
host key.

Connect as `ubuntu` with the private key that matches the selected key pair.
Replace this documentation IP address with the Elastic IP you recorded:

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
and a root disk of at least 40 GiB; `df` shows less usable space after formatting.
The shared install guide needs an
Ubuntu SSH user with working `sudo`. It enters a root shell to install and run
Docker. Docker group membership is not needed.

Check the DNS answer from a system with DNS tools:

```sh
dig +short A tunnel.example.com
dig +short AAAA tunnel.example.com
```

The A answer must be the Elastic IP. The AAAA answer must be empty.

## If you abandon this setup

Stop and remove only the instance, Elastic IP, key pair, and security group
created for this attempt, and only when they have no other users or
dependencies. Release the Elastic IP so it does not continue to incur charges.
Do not delete or edit a shared VPC, subnet, route table, internet gateway,
network ACL, key pair, or security group. If ownership is unclear, leave the
resource in place and report its name and state to the operator. Check for
remaining EBS volumes and snapshots that belong only to this failed host;
delete them only if they contain no data the operator needs. Review the AWS
Billing console for remaining charges.

## Continue

After these checks pass, follow [Prepare encrypted storage](storage.md) to
create `/srv/startunnel` on the encrypted EBS root disk. You do not need an
extra volume. Then follow [Install StarTunnel](install.md). For the setup
choice index, see the [setup index](README.md).

## AWS documentation

- [Launch an EC2 instance](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/EC2_GetStarted.html)
- [EC2 instance configuration and storage](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-launch-parameters.html)
- [Encrypt EBS volumes](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/AMIEncryption.html)
- [EC2 security group rules](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/security-group-rules-reference.html)
- [Add internet access to a subnet](https://docs.aws.amazon.com/vpc/latest/userguide/working-with-igw.html)
- [Connect to a Linux EC2 instance](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/connect-to-linux-instance.html)
- [Allocate and associate an Elastic IP](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/elastic-ip-addresses-eip.html)
