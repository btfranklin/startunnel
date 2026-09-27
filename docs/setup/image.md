# Build the application image

[Setup index](README.md) → image → provider → installation → verification → backup

Run these steps on a trusted **workstation or build host**, not on the production
server. You need Git and Docker with Buildx. Docker Desktop includes Buildx;
on Linux, use [Docker's installation guide](https://docs.docker.com/engine/install/).
Run `docker version` and `docker buildx version` and correct any error first.
Use Bash for the commands below.

If your operator already provides a reviewed image pinned with `@sha256:`, its
matching Git commit, and registry access, skip to your [provider guide](README.md#choose-a-configuration).
The image must use the repository's `runtime` target and default application
UID/GID `10001:10001` for the shared installation steps.

## 1. Select and record the source

Create a new directory with no local credentials or private files:

```shell
git clone https://github.com/btfranklin/startunnel.git startunnel-build
cd startunnel-build
git log -1 --oneline
git status --short
```

Select a commit whose project checks passed. If the team uses another reviewed
commit, run `git checkout --detach COMMIT` with its full SHA before you continue.
Do not build from an unreviewed or dirty checkout. If the source repository is
not accessible, obtain access or an approved source checkout from the operator;
do not substitute another project.

```shell
SOURCE_COMMIT=$(git rev-parse HEAD)
printf '%s\n' "$SOURCE_COMMIT"
```

Record this full commit. The production checkout must use the same commit.
The image build installs the locked Python dependencies and collects static
files. It does not require Python or PDM on the workstation.

## 2. Prepare a registry

This guide uses GitHub Container Registry (`ghcr.io`). Use your own GitHub
account or a team organization in which you can publish packages. Choose the
package name `startunnel`. Use lowercase for the registry owner.

In GitHub, open **Settings → Developer settings → Personal access tokens →
Tokens (classic)**. Create a short-lived token with `write:packages`. Enable
organization SSO for it if required. Package permissions must allow the chosen
owner. Enter the token only at Docker's hidden password prompt:

```shell
docker login ghcr.io --username YOUR_GITHUB_USERNAME
```

Replace `YOUR_GITHUB_USERNAME` with the token owner's username. A token is the
password for this command. An agent without an interactive terminal must use
an approved local credential store or let the operator complete this step.

New packages are private by default. Keep that setting unless the team intends
to publish the image publicly. Give the server's token owner read access to the
package. See [GitHub's registry instructions](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

## 3. Build and publish

Replace `your-github-owner` before you run this block:

```shell
IMAGE_REPOSITORY=ghcr.io/your-github-owner/startunnel
docker buildx build --platform linux/amd64 --target runtime \
  --label "org.opencontainers.image.revision=$SOURCE_COMMIT" \
  --tag "$IMAGE_REPOSITORY:$SOURCE_COMMIT" --push .
docker buildx imagetools inspect "$IMAGE_REPOSITORY:$SOURCE_COMMIT"
```

Wait for the build and push to succeed. `linux/amd64` matches the x86-64 servers
in these guides, including when the build runs on an Apple Silicon Mac.
Do not pass secrets as build arguments or add them to the checkout.

Copy the top-level `Digest: sha256:...` value from the inspection output.
The complete production image reference is the repository plus `@` plus that
digest. It has this form:

```text
ghcr.io/your-github-owner/startunnel@sha256:YOUR_ACTUAL_64_HEX_DIGEST
```

This example is not a usable image. Use the real digest returned by the
registry. [Docker documents the inspection output](https://docs.docker.com/reference/cli/docker/buildx/imagetools/inspect/).
Record the image reference and source commit together. Keep this image in the
registry for deployment and recovery.

Log out if this workstation does not need continued registry access:

```shell
docker logout ghcr.io
```

## Next step

Choose [EC2](aws-ec2.md), [Lightsail](aws-lightsail.md),
[DigitalOcean](digitalocean.md), or [an existing Linux server](existing-linux.md).
If the server is already prepared, continue with [installation](install.md).
