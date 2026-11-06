<p align="center">
  <a href="https://github.com/almali-dev">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/almali-dev/.github/main/brand/library-curator/lockup-dark.svg">
      <img alt="Almali Library Curator for Plex" src="https://raw.githubusercontent.com/almali-dev/.github/main/brand/library-curator/lockup.svg" height="64">
    </picture>
  </a>
</p>

# Almali Library Curator for Plex

*Formerly Library Curator for Plex (LCP).*

Shows what nobody watches in a Plex library, says why in one sentence per item,
and removes only what the owner marks, after a grace period and a final
re-check.

> Not affiliated with or endorsed by Plex Inc. "Plex" is a trademark of Plex, Inc.

## Status

Version 1 is under construction. The requirements live in
[`docs/Almali-Library-Curator-for-Plex.md`](docs/Almali-Library-Curator-for-Plex.md) and the
implementation plan in [`odd/tasks/almali-library-curator.md`](odd/tasks/almali-library-curator.md).

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ALC_CONFIG_DIR=./config pytest
ALC_AUTH_USER=admin ALC_AUTH_PASSWORD=admin \
  ALC_CONFIG_DIR=./config flask --app almali_library_curator.wsgi run
```

The server needs a login from the environment; see [Login](#login).

`tests/web/test_browser_layout.py` opens every screen at 320 px wide in headless
Chromium and measures the page width and the 44 × 44 px touch targets. It uses
Playwright when the Python environment already provides it with its Chromium,
and skips otherwise; it is not a project dependency.

Settings live in `$ALC_CONFIG_DIR` (default `/config` in the container) and are
overridden per run with `ALC_`-prefixed environment variables. A fresh install
starts in dry-run mode: nothing is ever deleted until the owner turns real
deletion on.

### Curated libraries

The app curates every Plex library by default. Choose a subset with the
"Curated" checkboxes in the first-run wizard (`/setup`) or **Settings**: a
library that is not curated is never scanned, shown or touched, its items
cannot be marked or deleted, and a marked item whose library loses its curation
is cancelled, never deleted. The library list always shows every section so the
choice can be changed later, and the deletion history stays global.

`ALC_CURATED_LIBRARIES` overrides the stored selection with a comma-separated
list of Plex library keys (for example `ALC_CURATED_LIBRARIES=1,4`). An unset
or empty value means no restriction — every library is curated.

### Plex connection

Plex is the only required connection. **Sign in with Plex** is the primary
way to connect: in the first-run wizard (`/setup`) or on **Connections**,
press "Sign in with Plex", open the Plex sign-in page, and enter the code the
app shows. The app polls plex.tv until your account authorizes it (the code
expires after fifteen minutes), tests the account token against your Plex
server, and stores it exactly like a manual entry: the address goes to
`settings.json`; the token is encrypted with Fernet and written to
`secrets.json`, with its key in `secrets.key`. Both files live in
`$ALC_CONFIG_DIR`, are readable by the owner only (mode 0600) and are never
served. The screens show only the token's last four characters. The PIN code
you enter is not a credential and may be shared or shown.

If no address is configured yet, the app can list your own servers from your
Plex account so you pick the address instead of typing it; it never writes an
address you did not pick. Typing the address and token by hand stays available
at all times: the app tests them against Plex and stores them only when Plex
answers as a Plex Media Server.

Outbound rule: the app calls only the services you configure, plus one
exception for the sign-in flow — `plex.tv` (creating and polling the PIN, and
listing your servers) and `clients.plex.tv`, which the Plex sign-in page
itself uses. Nothing else is ever contacted.

`ALC_PLEX_BASE_URL` and `ALC_PLEX_TOKEN` still override the stored values. While
either is set, the screens say that the environment manages Plex and refuse to
change it. If the secrets files are damaged or the key is lost, the app still
starts, reports Plex as not configured and asks for the token again. Keep
`secrets.key` with your backups of `$ALC_CONFIG_DIR`: without it the stored
token cannot be read.

## Login

Every screen and action needs the one owner login. It comes from the
environment only:

| Variable | Purpose |
| --- | --- |
| `ALC_AUTH_USER` | The login user name. |
| `ALC_AUTH_PASSWORD` | The login password. It is hashed in memory at start and is never written to `settings.json`, shown or logged. |
| `ALC_SECURE_COOKIES` | `true` adds the `Secure` flag to the session cookie. Off by default, because the web UI is plain HTTP on the LAN; turn it on when the app sits behind an HTTPS proxy. |

There is no default login. **Without both `ALC_AUTH_USER` and
`ALC_AUTH_PASSWORD` the app answers every screen and action with a "Login is
not configured" page (HTTP 503)** that names the two variables; `/healthz`
stays alive (200, `status: ok`) and lists `auth` under `missing` with
`ready: false`. Only `/healthz`, `/static/*` and `/login` are reachable
without signing in.

For local development, export an example login before starting the server
(an example to type, never a built-in default):

```bash
export ALC_AUTH_USER=admin
export ALC_AUTH_PASSWORD=admin
ALC_CONFIG_DIR=./config flask --app almali_library_curator.wsgi run
```

The session cookie is `HttpOnly` and `SameSite=Lax` and lasts 12 hours; the
**Sign out** button ends it. After 5 failed sign-ins from one address within
15 minutes, that address is locked out for 15 minutes (HTTP 429). The signing
key in `$ALC_CONFIG_DIR/secret_key` is owner-only (mode 0600); a key shorter
than 32 bytes is replaced at start, which signs every session out. Log lines
never carry the password: any structured field whose name contains
`password`, `token`, `secret`, `api_key`, `apikey`, `authorization` or
`cookie` is written as `***`.

## Protection engine (development API)

`ProtectionEngine(connection, settings=settings)` excludes protected libraries,
items rated at or above `minimum_rating` (8.0 by default), and rows kept forever.
Raising that minimum can remove rating protection; lowering it protects more
items. The rating rule can be disabled independently.

Inject the engine as `DetectionEngine(..., protector=protector)`. `scan()` returns
only surviving candidates; `scan_with_protection()` also returns protected rows,
their protection reasons and the categories they would otherwise match.
`protection_for(candidate)` provides the same check for the future final re-check.

`keep_forever(candidate)` and `undo_keep_forever(candidate)` persist and undo
protection for the exact item, unit and season set. Unreadable stored scopes keep
the item protected and emit a warning until explicit undo. Removing protection
never marks media for removal. The web actions and deletion lifecycle are still
under construction.

## Part of Almali

This project is part of [Almali](https://github.com/almali-dev), a small collection of independent, open-source
tools for home media and self-hosting, built and maintained by [@mmarkimark](https://github.com/mmarkimark).
Each project works on its own: install only what you need.

## Licence

MIT.
