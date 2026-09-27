# Promotional website

This directory owns the static Signal Chamber page, gate artwork, glyph list,
and animation. The installed application does not serve this page. Its `/`
route opens the application dashboard and uses the existing sign-in flow.

Build from the repository root:

```shell
pdm run python website/build.py --output website/dist --base-path /startunnel/ --repository-url https://github.com/btfranklin/startunnel
```

Use `--base-path /` for a custom domain or a local preview. The repository URL
controls documentation and source links; it does not select an application
server. The repository address is the public source location.
`--site-url` sets the public origin used in copied prompts and the agent index.
It defaults to `https://startunnel.net`; the builder appends `--base-path`.
For a project site, use `--site-url https://btfranklin.github.io` with
`--base-path /startunnel/`.

## Agent entry points

The homepage offers a general prompt and separate setup and connection prompts.
The general and setup prompts link to the [team setup index](../docs/setup/README.md).
The setup card, agent guide, and `llms.txt` also link to that index. It owns
configuration selection and the complete EC2, Lightsail, DigitalOcean, and
existing-server paths. These Markdown guides are read from the source repository;
they are not copied into the static website artifact.
Their authored text lives in `website/prompts/`. The static build renders each
prompt once into visible, selectable text. The shared copy control reads that
text. Without JavaScript, visitors can select the prompt or follow the guide link.

The build also publishes these resources:

| Path | Source and purpose |
|---|---|
| `agents/` | HTML rendering of the agent guide |
| `agents.md` | Exact copy of `skills/startunnel/SKILL.md` |
| `llms.txt` | Small resource index from `website/templates/llms.txt` |
| `openapi.json` | Exact copy of `generated/openapi.json` |

The skill can be discovered in the public repository with
`npx skills add btfranklin/startunnel --skill startunnel`. Reading the guide
directly does not require installing a skill. The guide uses the current
Python client from the user's instance and the existing deployment commands.
The website does not issue keys or host a shared application instance.

Publishing the site does not publish that repository, an application image,
or a standalone CLI package. The [image guide](../docs/setup/image.md) explains
how to build and publish the team's image. Production setup still requires
the team's provider access, DNS, registry, and backup choices.

The resource index follows the [llms.txt proposal](https://llmstxt.org/), checked
on September 25, 2026. It gives agents a useful entry point; it does not control
crawlers or guarantee that an agent will discover it.

## Build and preview

The builder uses Django's standalone template engine, not application settings.
It needs no database, credentials, running service, or network connection.
The output contains only HTML and public assets. Shared CSS, fonts, favicon,
and page behavior are copied from their single sources under `static/`.
These shared assets are tracked, so a fresh checkout can build without Node.
Run `npm ci` and `npm run vendor` only when updating the shared vendor assets.
The build records its output files and removes stale owned files on a rebuild.
It refuses a nonempty output directory without that record, and refuses to
overwrite a build directory that contains unrelated files.

To preview a root-path build:

```shell
pdm run python website/build.py --site-url http://localhost:8011
pdm run python -m http.server 8011 --bind 127.0.0.1 --directory website/dist
```

The Pages workflow builds this artifact. Deployment is disabled until the
repository uses GitHub Pages with Actions and its `PAGES_ENABLED` variable is
`true`. The workflow supplies the repository URL and Pages base path. It does
not publish an application image or a CLI release.
For the remote setup, see
[GitHub's Pages workflow guide](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages).

The page keeps the Signal Chamber artwork and animation. Public navigation
links to the static agent guide and repository documentation.
