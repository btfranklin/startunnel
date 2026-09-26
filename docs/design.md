# Interface design

StarTunnel uses a direct server-rendered Django interface with minimal JavaScript.

## Brand

Use **StarTunnel** with this capitalization in prose and interface text.
Repository names, command names, and URLs retain their lowercase spelling.

- The logo mark is **🜎**, U+1F70E, philosophers' sulfur.
- Render the logo with the footless vector in `static/favicon.svg`.
  It is a custom rendering of 🜎, without serifs under its three legs.
  Use the bundled **Noto Sans Symbols** font for other glyphs and addresses.
- Render the **StarTunnel** wordmark with **Space Grotesk**, weight 700.
- Use the shared `.wordmark` and `.wordmark__mark` styles in `static/css/site.css`.
  Keep the mark and name vertically centered. The promotional site changes
  their color, not their font or glyph.
- The shared `static/favicon.svg` supplies the favicon, header mark, and
  matching gate mark. Both the service and promotional site use this asset.

Service pages, including public documentation and the API reference, inherit
the shared navigation. The promotional site and HTML style guide use the same
fonts and wordmark styles. Plain Markdown uses the reader's font; product names
in prose are not logos and do not require special formatting.

The gate uses eight distinct decorative marks, starting at 12 o'clock:
🜎 🜁 🜂 🜃 🜄 🜍 🜔 🜞. Their positions are defined in `website/glyphs.py`.
Tunnel addresses use a separate alphabet. Do not replace valid address glyphs
in examples, schemas, or tests when changing the brand.

## Human tasks

Every active human is an administrator. The interface supports these tasks:

- sign in with a local username and password;
- change the current password;
- create, activate, or deactivate human accounts;
- create and revoke instance agent credentials;
- inspect and operate instance tunnels;
- read product and API documentation.

There is no team switcher, membership page, invitation flow, OAuth callback, personal workspace, or plan page.

## Navigation

| Route | Purpose |
|---|---|
| `/accounts/login/` | Local sign-in |
| `/accounts/password/change/` | Password change |
| `/app/` | Instance summary |
| `/app/users/` | Human administration |
| `/app/agents/` | Agent credential administration |
| `/app/tunnels/` | Instance tunnel operations |
| `/docs/` | User documentation |
| `/api/docs` | Interactive API reference |

## Rules

- Use plain labels and explicit actions.
- Show a new agent key once, with a clear copy action and warning.
- Do not show stored digests.
- Confirm destructive account, credential, address, or tunnel actions.
- Explain the next administrator action after a human is deactivated.
- Do not use eyebrow text.
- Keep focus states, labels, error summaries, and keyboard order accessible.
- Do not put addresses or keys in URLs or browser logs.

## Language

Call the shared object a **tunnel**. Do not call it global, team, personal, or tenant scoped. Say that it is **unlisted, not confidential**.
