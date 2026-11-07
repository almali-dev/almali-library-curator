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

**No rule builder.** A fixed set of categories, each with a few thresholds, and
every result shown in place with its reason, size, disk and rating.

## Features

- **Five detection categories**, per series, per season and per movie, each with
  a one-sentence reason built from the facts that matched:

  | Category | Matches when | Default threshold |
  | --- | --- | --- |
  | Stalled series | Someone watched part of a show, then later seasons sat unopened | 6 months without a play |
  | Never watched | No user has played any part since it was added | Added more than 3 months ago |
  | Cancelled, no viewers | Sonarr or TMDB says ended or cancelled, and nobody is mid-show | Last play more than 6 months ago |
  | Seasons everyone finished | Every follower watched those seasons, later seasons still pending | Finished more than 1 month ago |
  | Movies not rewatched | Watched at least once, nobody played it since | Last play more than 18 months ago |

- **Three kinds of protection**: protected libraries, a minimum rating (8.0 by
  default) and items kept forever by hand. Protection is checked at every scan
  and again right before a deletion.
- **A removal lifecycle you control**: nothing is removed unless you mark it.
  A marked item goes to a "Leaving soon" collection in Plex for a grace period
  (14 days by default), gets a final re-check (still matching, not protected,
  nobody started watching), and only then is deleted.
- **Verified deletion**: through Sonarr or Radarr (files removed, unmonitored,
  import exclusion so it is not downloaded again), or through Plex when they are
  not configured. Freed space is confirmed on disk and recorded in a history
  you can export as CSV.
- **Safe by default**: a fresh install runs in dry-run mode and only reports
  what would happen; a global kill switch stops every deletion at once.
- **Plex is the only required connection.** Sonarr, Radarr, Tautulli and TMDB
  are optional and the app degrades cleanly without them. Connect Plex by
  signing in with your Plex account or by typing an address and token.
- **Web UI** in English, Catalan and Spanish, usable from 320 px wide, with an
  owner login, CSRF protection and secrets encrypted at rest.

## Status

**Pre-release (v0.18.0).** Version 1 is under construction and the first
publishable release is **v1.0.0**. Every pre-1.0 tag marks one completed task;
see the [roadmap](docs/ROADMAP.md) for what is done and what comes next, and the
[changelog](CHANGELOG.md) for what changed in each milestone.

Run it with [Docker](#docker), on [Unraid](docs/UNRAID.md) (installer script or
Community Applications template), or [from source](#quick-start-from-source).

## Docker

Build the image from this repository (any architecture the official Python
image supports, including amd64 and arm64), then start it with the compose
example:

```bash
git clone https://github.com/almali-dev/almali-library-curator.git
cd almali-library-curator
docker build -t almali-library-curator .
cp docker-compose.example.yml docker-compose.yml   # adjust the host paths
printf 'ALC_AUTH_USER=owner\nALC_AUTH_PASSWORD=choose-a-strong-password\n' > .env
chmod 600 .env
docker compose up -d
```

The owner login is required: `docker compose` refuses to start while
`ALC_AUTH_USER` or `ALC_AUTH_PASSWORD` is missing from `.env`, and a container
started without them answers every screen with the 503 page (see
[Login](#login)).

The app listens on port **17540** (one above Plex Smart Cache's 17539) and
serves Gunicorn with one worker, so the scan scheduler and the sign-in lockout
run in one process. Open `http://<host>:17540/setup`, sign in and connect Plex.

| Container path | Mode | Purpose |
| --- | --- | --- |
| `/config` | rw | Settings, database, secrets, logs, backups (`ALC_CONFIG_DIR`) |
| `/media` | ro | File sizes and hardlink counts; the app never writes media |
| `/unraid/mnt` | ro, optional | Which array disk or pool holds each file; set `ALC_UNRAID_ROOT=/unraid/mnt` with it |

The container starts as root only to give `/config` to `PUID`:`PGID`
(default `99`:`100`, Unraid's `nobody`:`users`), then runs the app as that
user; root (`0`) is refused. Set `TZ` for the daily scan time. When Plex
reports other paths than the container sees, map them with
`ALC_PLEX_PATH_MAP=/plex/path=/media`. A fresh install starts in dry-run
mode; turn real deletion on in **Settings**.

Behind a reverse proxy, set `ALC_TRUSTED_PROXIES` to the number of proxies and
keep port 17540 reachable only from the proxy; add `ALC_SECURE_COOKIES=true`
when the proxy serves HTTPS.

The health check calls `/healthz`: the container is healthy while the app
answers `200` with `"status": "ok"`, also before Plex is connected (the
response lists it under `missing`).

## Quick start (from source)

Requires Python 3.11 or newer.

```bash
git clone https://github.com/almali-dev/almali-library-curator.git
cd almali-library-curator
python -m venv .venv && . .venv/bin/activate
pip install -e .
export ALC_AUTH_USER=owner ALC_AUTH_PASSWORD='choose-a-strong-password'
ALC_CONFIG_DIR=./config python -m gunicorn --bind 127.0.0.1:17540 \
  almali_library_curator.wsgi:app
```

Open <http://127.0.0.1:17540>, sign in, and follow the first-run wizard: connect
Plex, choose the libraries to curate and the ones to protect. The app starts in
dry-run mode; turn real deletion on in **Settings** when the reports look right.

## Configuration

Settings live in `$ALC_CONFIG_DIR` (default `/config`, as in the container):
`settings.json` for the options the screens edit, `secrets.json` and
`secrets.key` for the encrypted Plex token, `secret_key` for the session
signing key, and the SQLite database. Back the whole directory up.

Most options are edited in **Settings**. Any of them can be overridden per run
with an `ALC_`-prefixed environment variable, which wins over the file and is
never written back to it:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ALC_CONFIG_DIR` | `/config` | Where settings, secrets and the database live. |
| `ALC_AUTH_USER`, `ALC_AUTH_PASSWORD` | none | The owner login (required; see [Login](#login)). |
| `ALC_SECURE_COOKIES` | `false` | Add `Secure` to the session cookie behind HTTPS. |
| `ALC_TRUSTED_PROXIES` | `0` | Trusted reverse-proxy hops used for the client address in lockout. |
| `ALC_DRY_RUN` | `true` | Report only; never delete. |
| `ALC_LOCALE` | `en` | Interface language: `en`, `ca` or `es`. |
| `ALC_GRACE_DAYS` | `14` | Days an item stays in "Leaving soon" (3–60). |
| `ALC_COLLECTION_NAME` | `Leaving soon` | Name of the Plex collection. |
| `ALC_MINIMUM_RATING` | `8.0` | Items rated at or above this are protected. |
| `ALC_SCAN_TIME` | `06:00` | Daily scan time, local `HH:MM`. |
| `ALC_SCHEDULER_ENABLED` | `true` | Turn the daily scan off (scans on demand still work). |
| `ALC_PROTECTED_LIBRARIES` | none | Comma-separated Plex library keys that are never touched. |
| `ALC_CURATED_LIBRARIES` | all | Comma-separated Plex library keys to curate. |
| `ALC_EXCLUDED_USERS` | none | Comma-separated Plex users whose plays do not count. |
| `ALC_PLEX_BASE_URL`, `ALC_PLEX_TOKEN` | none | Plex address and token (otherwise set in the app). |
| `ALC_PLEX_PATH_MAP` | none | Comma-separated `plex-path=local-path` pairs when Plex sees media under other paths. |
| `ALC_SONARR_BASE_URL`, `ALC_SONARR_API_KEY` | none | Optional Sonarr connection. |
| `ALC_RADARR_BASE_URL`, `ALC_RADARR_API_KEY` | none | Optional Radarr connection. |
| `ALC_SONARR_DELETES`, `ALC_RADARR_DELETES` | `true` | Let a configured Sonarr or Radarr delete its own items. |
| `ALC_TAUTULLI_BASE_URL`, `ALC_TAUTULLI_API_KEY` | none | Optional Tautulli connection (play history). |
| `ALC_TMDB_API_TOKEN` | none | Optional TMDB token (ratings and series status). |
| `ALC_MEDIA_ROOT` | `/media` | Where the media is mounted (read-only). |
| `ALC_UNRAID_ROOT` | none | Unraid `/mnt` mount, to report which disk holds each item. |
| `ALC_LOG_LEVEL` | `INFO` | Log level. |

The Sonarr, Radarr, Tautulli and TMDB keys are read from the environment for
now; entering them in the app, encrypted at rest, is planned (roadmap task T11b).

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
| `ALC_TRUSTED_PROXIES` | The number of reverse proxies in front of the app (default `0`). Set it only to a trusted proxy count; at `0`, `X-Forwarded-For` is ignored. |

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

`ALC_TRUSTED_PROXIES` lets Werkzeug use `X-Forwarded-For` through trusted proxy
hops for lockout; invalid or negative values are treated as `0` with a warning.

The session cookie is `HttpOnly` and `SameSite=Lax` and lasts 12 hours. The
**Sign out** button signs this browser out. The cookie is stateless; changing
`ALC_AUTH_PASSWORD` signs every session out. A session survives a restart and
is valid on every worker. After 5 failed sign-ins from one address within
15 minutes, that address is locked out for 15 minutes (HTTP 429). The signing
key in `$ALC_CONFIG_DIR/secret_key` is owner-only (mode 0600); a key shorter
than 32 bytes is replaced at start, which signs every session out. Log lines
never carry the password: any structured field whose name contains
`password`, `token`, `secret`, `api_key`, `apikey`, `authorization` or
`cookie` is written as `***`.

## Security

- One owner login from the environment; no default credentials; fail closed
  with HTTP 503 when the login is missing.
- CSRF protection on every action, `HttpOnly` / `SameSite=Lax` session cookie,
  lockout after repeated failed sign-ins.
- The Plex token is encrypted at rest (Fernet) and never shown in full; secrets
  are never logged.
- Outbound calls go only to the services you configure, plus `plex.tv` and
  `clients.plex.tv` for the Plex sign-in. Redirects are refused, so credentials
  are never re-sent to another host.
- The app never writes to your media directly: deletions go through Sonarr,
  Radarr or the Plex API.

Please report vulnerabilities privately; see [SECURITY.md](SECURITY.md).

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ALC_CONFIG_DIR=./config pytest
ALC_AUTH_USER=admin ALC_AUTH_PASSWORD=admin \
  ALC_CONFIG_DIR=./config flask --app almali_library_curator.wsgi run
```

`tests/web/test_browser_layout.py` opens every screen at 320 px wide in headless
Chromium and measures the page width and the 44 × 44 px touch targets. It uses
Playwright when the Python environment already provides it with its Chromium,
and skips otherwise; it is not a project dependency.

The requirements live in
[`docs/Almali-Library-Curator-for-Plex.md`](docs/Almali-Library-Curator-for-Plex.md)
See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

## Part of Almali

This project is part of [Almali](https://github.com/almali-dev), a small collection of independent, open-source
tools for home media and self-hosting, built and maintained by [@mmarkimark](https://github.com/mmarkimark).
Each project works on its own: install only what you need.

## Licence

[MIT](LICENSE).
