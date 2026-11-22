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
  what would happen; a global kill switch stops every deletion at once. Every
  Library tab says which of the two is active. The actions that arm a deletion
  are styled apart from the safe ones, and turning dry run off or resuming
  deletions asks for a confirmation first, with what is now due. Each Leaving
  soon row says the date its files go and how many files that is.
- **Plex is the only required connection.** Sonarr, Radarr, Tautulli and TMDB
  are optional and the app degrades cleanly without them. Connect Plex by
  signing in with your Plex account or by typing an address and token.
- **A scan picks up where the last one stopped.** Plex history is read newest
  first down to the newest play already stored, Tautulli's own `after` filter
  carries the window, and a stored row is only rewritten when it changed. A
  second scan of an unchanged library reads and writes almost nothing.
- **TMDB is asked once a month, not once a scan.** Its answers are kept in the
  database for 30 days, and a series Sonarr already answered is not read at
  all unless TMDB still holds a fact the scan uses: a rating Plex does not
  have, or the year of an ended series. A dry-run scan keeps that cache too;
  it is what TMDB said, not what the scan decided.
- **Web UI** in English, Catalan and Spanish, usable from 320 px wide, with an
  owner login, CSRF protection and secrets encrypted at rest. Below 992 px the
  secondary chrome folds into disclosures so the candidate list comes first.
- **A first run that says where it stands.** Until Plex is connected and the
  first analysis has finished, the home page lists the five steps of the setup
  with their own state and one action each, and the header names the mode, the
  last analysis and the next one. Before that first analysis the summary cards
  show a dash instead of a fake zero, and the empty candidate list says the
  library has not been analysed yet.

## Status

**Pre-1.0 (v0.42.0).** Version 1 is under construction; published development
images are not a production-readiness promise. Stable version tags (including
`v0.x.y`) and prerelease tags can publish images after the release checks pass.
See the [roadmap](docs/ROADMAP.md) for what comes next and the
[changelog](CHANGELOG.md) for changes in each milestone.

Run it with [Docker](#docker), on [Unraid](docs/UNRAID.md) (installer script or
Community Applications template), or [from source](#quick-start-from-source).

## Docker

Pull the published image and run it; nothing has to be built. The image is
`ghcr.io/almali-dev/almali-library-curator`, pinned to this release:

```bash
mkdir almali-library-curator && cd almali-library-curator
docker pull ghcr.io/almali-dev/almali-library-curator:0.42.0
curl -fsSLO https://raw.githubusercontent.com/almali-dev/almali-library-curator/main/docker-compose.example.yml
mv docker-compose.example.yml docker-compose.yml   # adjust the host paths
printf 'ALC_AUTH_USER=owner\nALC_AUTH_PASSWORD=choose-a-strong-password\n' > .env
chmod 600 .env
docker compose up -d
```

The example runs
`ghcr.io/almali-dev/almali-library-curator:0.42.0`; set `ALC_IMAGE_TAG` to
another published version tag to run that release instead. Once the owner
has made the package public (see
[Package visibility is an owner action](#package-visibility-is-an-owner-action)),
an anonymous `docker pull` works and no `docker login ghcr.io` is needed; until
then a pull answers `denied`.

If a pull answers `denied`, `unauthorized`, `not found` or `manifest unknown`,
the package is private or this host is not signed in. **Package visibility is
separate from repository visibility**: a public repository can still ship a
private image. The repository owner makes the package public under **Package
settings → Change visibility → Public** and proves it from a clean shell with
`docker pull ghcr.io/almali-dev/almali-library-curator:0.42.0` (no login). A
host that must authenticate runs `docker login ghcr.io` with a token that has
`read:packages`. The Unraid installer prints the same hint and stops before
creating anything.

The owner login is required: `ALC_AUTH_USER` has no default, and a container
started without a usable login answers every screen with the 503 page (see
[Login](#login)). The compose example accepts any one of the three password
sources below and leaves all of them optional, so a missing password is
reported by the app rather than by `docker compose`.

To keep the password out of the container environment (it is otherwise
readable with `docker inspect` and in `/proc/1/environ`), replace
`ALC_AUTH_PASSWORD` with one of:

- `ALC_AUTH_PASSWORD_FILE=/run/secrets/alc_auth_password`, pointing at an
owner-only (0600) file, for example a Docker secret or a file next to the
compose file;
- `ALC_AUTH_PASSWORD_HASH`, a Werkzeug hash generated once on the host:

```bash
python -c 'from werkzeug.security import generate_password_hash as g; print(g("choose-a-strong-password"))'
```

`ALC_AUTH_PASSWORD` keeps working and wins over nothing: when more than one
source is set the app logs the conflict and uses the hash, then the file, then
the plain variable. Leave exactly one of the three set; a blank one is
ignored, and the app fails closed while none is set.

In a `.env` file, `docker compose` interpolates values, so a hash needs `$$`
for every literal `$` (`ALC_AUTH_PASSWORD_HASH='scrypt:...$$...'`). The file
source has no such escaping; pass either one from the shell environment and
no escaping is needed.

### Build the image yourself

The image builds from this repository for any architecture the official Python
image supports, including amd64 and arm64. Build it with the tag the compose
example references, so `docker compose up` uses the local image:

```bash
git clone https://github.com/almali-dev/almali-library-curator.git
cd almali-library-curator
docker build -t ghcr.io/almali-dev/almali-library-curator:0.42.0 .
cp docker-compose.example.yml docker-compose.yml   # adjust the host paths
printf 'ALC_AUTH_USER=owner\nALC_AUTH_PASSWORD=choose-a-strong-password\n' > .env
chmod 600 .env
docker compose up -d
```

The app listens on port **17540** (one above Plex Smart Cache's 17539) and
serves Gunicorn with one worker, so the scan scheduler and the sign-in lockout
run in one process. Open `http://<host>:17540/setup`, sign in and connect Plex.

| Container path | Mode | Purpose |
| --- | --- | --- |
| `/config` | rw | Settings, database, secrets and pre-migration backups (`ALC_CONFIG_DIR`); logs go to the container output |
| `/media` | ro | File sizes and hardlink counts; the app never writes media |
| `/unraid/mnt` | ro, optional | Which array disk or pool holds each file; set `ALC_UNRAID_ROOT=/unraid/mnt` with it |

The container starts as root only to give `/config` to `PUID`:`PGID`
(default `99`:`100`, Unraid's `nobody`:`users`), then runs the app as that
user; root (`0`) is refused. Set `TZ` for the daily scan time. When Plex
reports other paths than the container sees, map them with
`ALC_PLEX_PATH_MAP=/plex/path=/media`. A fresh install starts in dry-run
mode; turn real deletion on in **Settings**.

The compose example and the Unraid installer run the container with least
privilege: `--cap-drop ALL`, only `CHOWN`, `DAC_OVERRIDE`, `SETUID` and `SETGID` added (the
entrypoint needs them to give `/config` to the app user and drop to it),
`--security-opt no-new-privileges:true`, a read-only root filesystem and a
`/tmp` tmpfs for Gunicorn's worker heartbeat. `/config` is the volume the app
writes; nothing else in the image is writable. The compose example and the
Unraid installer pin the image tag, so they update only when the owner
decides. The Unraid template follows `latest`, which moves only for the newest
stable release, and opts into Watchtower: a label-scoped Watchtower (or CA
Auto Update Applications) updates it on its own, as described in
[docs/UNRAID.md](docs/UNRAID.md#automatic-updates-template-install).

The web port is published on every interface by default (`0.0.0.0`). To keep
it private, set the bind address: `ALC_INSTALL_BIND=127.0.0.1` in the
compose example and in the Unraid installer (the same variable, which the
installer also asks for interactively). Behind a reverse proxy, set
`ALC_TRUSTED_PROXIES` to the number of proxies and keep port 17540 reachable
only from the proxy; add `ALC_SECURE_COOKIES=true` when the proxy serves
HTTPS. A client that reaches the port directly could otherwise spoof
`X-Forwarded-For`, which the app trusts only when a proxy is declared.

The health check calls `/healthz`: the container is healthy while the app
answers `200` with `"status": "ok"`, also before Plex is connected (the
response lists it under `missing`). The public response contains only
`status`, `ready` and `missing`, never the application or schema version.
The running application version is available on the signed-in About page.

The image installs its dependencies from committed, hash-pinned locks; see
[Docker dependency locks](#docker-dependency-locks) before changing them.

## Releases

Release code belongs on `main`: maintainers must merge the reviewed release
branch there and protect `main` before cutting a tag. CI also runs on the
current `feature/almali-library-curator-v1` release branch, version tags and pull
requests. Publishing calls that same reusable CI workflow and waits for all
Python 3.11/3.12/3.13 tests, the locked-runtime `pip-audit` check, and a Docker
build-and-boot test (`ALC_DOCKER_TESTS=1`). Runtime and build dependencies come
from the committed hash-pinned locks; test/audit tools are installed separately.

Use `vMAJOR.MINOR.PATCH` or `vMAJOR.MINOR.PATCH-alpha.N`, `-beta.N`, `-rc.N`.
The tag must match `pyproject.toml` (for example, `v1.0.0-rc.1` matches the
Python package version `1.0.0rc1`). Publishing tags share one non-cancelling lock;
branch checks use isolated locks. An existing GHCR version tag is never rebuilt
or re-pushed. If a push succeeded but signing failed, manually retry that version
tag: the workflow resolves its existing digest and verifies the exact Publish
workflow identity for that Git tag and GitHub's OIDC issuer. A valid signature
refuses the retry; missing or non-matching signatures trigger signing of that
exact digest only, then stop without moving aliases. Registry and unexpected
verification errors stop the run; a failed signing attempt can be retried again.
Never delete an image tag to retry it; code changes require a new version.

Images at `ghcr.io/almali-dev/almali-library-curator` carry their full version.
Only stable releases can update `latest`, major (`1`) and minor (`1.2`) aliases;
each alias moves only for the highest numeric stable Git tag in its scope.
Prereleases get only their full version tag. All release tags must remain in
Git for these comparisons to protect against a backfill downgrade. Images are
multi-architecture (amd64/arm64), include SBOM and maximum-mode provenance
attestations, and are signed by digest with Cosign. The same release signs the
Unraid installer with `cosign sign-blob` and publishes its bundle as
`ghcr.io/almali-dev/almali-library-curator:installer-<version>`; pre-1.0 has no
GitHub Release, so the bundle is fetched with `docker create` and `docker cp` and
checked with `cosign verify-blob` (see the
[Unraid guide](docs/UNRAID.md#install-with-the-script)).

### Package visibility is an owner action

Publishing pushes the image to a GitHub package, and a package has its own
visibility: making the repository public does **not** publish the image. The
repository owner must set the image package to public once (**Package
settings → Change visibility → Public**) and keep it public, because the
Docker install, the compose example and the Unraid installer all pull it
anonymously. Anonymous pulls need no token; a host that pulls with a token
needs `read:packages`, never `write:packages`. Verify from a shell without a
registry login:

```bash
DOCKER_CONFIG="$(mktemp -d)" docker pull ghcr.io/almali-dev/almali-library-curator:0.42.0
```

A private package fails that command with `denied` or `unauthorized`; the
installer and the README above name the same fix. An automated post-publish
version of this check is not part of the workflow yet: it can only go green
after the owner action, so it is documented here until the package is public.

The release commit also bumps the image version pinned in
`install-unraid.sh`; a packaging test fails when it differs from
`pyproject.toml`, and the published `install-unraid.sh.sha256` is regenerated
for that script.

Manual dispatch must select a version tag to publish; branch dispatch runs CI
but skips the publish job. GitHub concurrency is not a FIFO queue: it retains
one pending run and a newer run can replace it. Maintainers must check that
each intended version ran, and retry unpublished tags explicitly. Merging to
`main`, branch protection, cancelling old queued runs and creating GitHub
Releases remain maintainer operations; the workflow does not perform them.

## Docker dependency locks

The image installs its third-party dependencies from two committed, hash-pinned
locks: `docker/requirements.lock` for the runtime dependencies declared in
`pyproject.toml`, and `docker/build-requirements.lock` for the build
toolchain. Both are generated files, never edited by hand, and the Dockerfile
installs them with `pip --require-hashes`: the build fails when a requirement
carries no hash or when the downloaded file does not match the lock.
`pyproject.toml` declares the same floors and caps the image installs, so the
package and the image can no longer disagree.

Refresh the locks with [uv](https://docs.astral.sh/uv/) from the repository
root; each lock records the command that generated it in its header:

```bash
uv pip compile pyproject.toml \
  --python-version 3.12 --python-platform x86_64-manylinux2014 \
  --generate-hashes --output-file docker/requirements.lock
uv pip compile docker/build-requirements.in \
  --python-version 3.12 --python-platform x86_64-manylinux2014 \
  --generate-hashes --output-file docker/build-requirements.lock
```

Every distribution of a pinned version is hashed, so the same lock installs on
both published architectures. Check that before an image is built or
published (the command needs network access):

```bash
pip download --require-hashes --only-binary=:all: --no-deps \
  --implementation cp --abi cp312 \
  --platform manylinux2014_x86_64 --python-version 3.12 \
  -r docker/requirements.lock -d "$(mktemp -d)"
pip download --require-hashes --only-binary=:all: --no-deps \
  --implementation cp --abi cp312 \
  --platform manylinux2014_aarch64 --python-version 3.12 \
  -r docker/requirements.lock -d "$(mktemp -d)"
```

`tests/packaging/test_dependency_hashes.py` checks that the locks and
`pyproject.toml` still agree; run it after every refresh.

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

Open <http://127.0.0.1:17540>, sign in, and follow the numbered steps the home
page lists: connect Plex, run the first analysis, choose the libraries to
curate and the ones to protect, and add the optional connections. The app
starts in dry-run mode; turn real deletion on in **Settings** when the reports
look right. The list stays until Plex is connected and the first analysis has
finished, then the page is the candidate list it always was.

## Configuration

Settings live in `$ALC_CONFIG_DIR` (default `/config`, as in the container):
`settings.json` for the options the screens edit, `secrets.json` and
`secrets.key` for the encrypted Plex token and service keys, `secret_key` for the session
signing key, and the SQLite database. `settings.json`, the database and its
SQLite `-wal`/`-shm` sidecars are owner-only (mode 0600), even with a permissive
umask. Back the whole directory up.

Most options are edited in **Settings**. Any of them can be overridden per run
with an `ALC_`-prefixed environment variable, which wins over the file and is
never written back to it. The language of the interface and of the removal
reasons is chosen in **Settings** (**Language**) among the supported locales,
and `ALC_LOCALE` wins over that saved choice like every other variable below.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ALC_CONFIG_DIR` | `/config` | Where settings, secrets and the database live. |
| `ALC_AUTH_USER`, `ALC_AUTH_PASSWORD` | none | The owner login (required; see [Login](#login)). |
| `ALC_AUTH_PASSWORD_FILE`, `ALC_AUTH_PASSWORD_HASH` | none | Alternative password sources: an owner-only file, or a Werkzeug hash, so the clear text never enters the environment (see [Login](#login)). |
| `ALC_SECURE_COOKIES` | `false` | Add `Secure` to the session cookie behind HTTPS. |
| `ALC_TRUSTED_PROXIES` | `0` | Trusted reverse-proxy hops used for the client address in lockout. |
| `ALC_DRY_RUN` | `true` | Report only; never delete. |
| `ALC_LOCALE` | `en` | Interface and reason language: `en`, `ca` or `es` (also chosen in **Settings**; a set variable wins, an empty one leaves the Settings choice). |
| `ALC_GRACE_DAYS` | `14` | Days an item stays in "Leaving soon" (3–60). |
| `ALC_COLLECTION_NAME` | `Leaving soon` | Name of the Plex collection. |
| `ALC_MINIMUM_RATING` | `8.0` | Items rated at or above this are protected. |
| `ALC_SCAN_TIME` | `06:00` | Daily scan time, local `HH:MM`. |
| `ALC_SCHEDULER_ENABLED` | `true` | Turn the daily scan off (scans on demand still work). |
| `ALC_PROTECTED_LIBRARIES` | none | Comma-separated Plex library keys that are never touched. |
| `ALC_CURATED_LIBRARIES` | all | Comma-separated Plex library keys to curate. |
| `ALC_EXCLUDED_USERS` | none | Comma-separated Plex users whose plays do not count. |
| `ALC_PLEX_BASE_URL`, `ALC_PLEX_TOKEN` | none | Plex address and token (otherwise set in the app). |
| `ALC_PLEX_RATE_LIMIT_PER_SECOND` | `20.0` | Requests per second sent to the Plex server. The default suits a server on the local network; lower it if the server struggles. |
| `ALC_PLEX_PATH_MAP` | none | Comma-separated `plex-path=local-path` pairs when Plex sees media under other paths. |
| `ALC_SONARR_BASE_URL`, `ALC_SONARR_API_KEY` | none | Optional Sonarr connection (otherwise set in the app). |
| `ALC_RADARR_BASE_URL`, `ALC_RADARR_API_KEY` | none | Optional Radarr connection (otherwise set in the app). |
| `ALC_SONARR_DELETES`, `ALC_RADARR_DELETES` | `true` | Let a configured Sonarr or Radarr delete its own items. |
| `ALC_TAUTULLI_BASE_URL`, `ALC_TAUTULLI_API_KEY` | none | Optional Tautulli connection (otherwise set in the app). |
| `ALC_TMDB_API_TOKEN` | none | Optional TMDB token (otherwise set in the app). |
| `ALC_MEDIA_ROOT` | `/media` | Where the media is mounted (read-only). |
| `ALC_UNRAID_ROOT` | none | Unraid `/mnt` mount, to report which disk holds each item. |
| `ALC_LOG_LEVEL` | `INFO` | Log level. |

The Sonarr, Radarr, Tautulli and TMDB keys are entered in the app, on
**Connections** or in the first-run wizard (`/setup`), and tested against the
service before anything is stored. See [Service connections](#service-connections).

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
expires after fifteen minutes) and updates the waiting status in place, so an
address or token you are typing by hand is never reloaded; **Check again**
refreshes that same status on demand. It tests the account token against your
Plex server, and stores it exactly like a manual entry: the address goes to
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

### Service connections

Sonarr, Radarr, Tautulli and TMDB are optional, and each one has its own
form on **Connections** and in the first-run wizard (`/setup`): the address
of the service and its API key, or, for TMDB, only the API read access
token (TMDB keeps its own default address). Each form says where to find
that key.

**Connections** is one card per service: Plex first, marked as the required
connection, then the optional ones and the host paths. A card carries the
state of its service (connected, configured but not tested, not configured, or
unreachable with a line that says what to check), what the service adds, its
actions and its own form, folded behind **Configure** or **Edit**. One card is
open at a time; a link such as `/connections?service=sonarr` or
`/connections#sonarr` opens the card it names, and a refused save reopens the
card it came from with its message. A service the environment manages shows
its note where the form would be.

The app **tests the key before it saves it**: it calls the service with the
address and the key you typed, and stores nothing unless the service
answers with its own API (`system/status` for Sonarr and Radarr, the user
list for Tautulli, one movie for TMDB). A refused key, an unreachable
service or an answer that is not that service each show their own message
and change nothing.

What is stored follows the Plex rules: the address goes to `settings.json`
as `<service>_base_url`, and the key is encrypted with Fernet and written to
`secrets.json`, in its own entry (`sonarr_api_key`, `radarr_api_key`,
`tautulli_api_key`, `tmdb_api_token`), with its key in `secrets.key`. Saving
one key keeps every other entry, the Plex token included. The screens show
only that a key is stored and its last four characters, and the key never
appears in a page, a log line or a message. "Forget the key" deletes that
entry and nothing else.

The environment still wins, per service: while `ALC_SONARR_BASE_URL` or
`ALC_SONARR_API_KEY` is set (the same for Radarr and Tautulli, and
`ALC_TMDB_API_TOKEN` for TMDB), that service's form is replaced by a note
that the environment manages it, and its two routes refuse to change
anything. The other services keep their forms. A blank variable counts as
unset.

If the secrets file or the key is damaged, the app still starts, reports
that service as not configured and asks for the key again. Keep
`secrets.key` with your backups of `$ALC_CONFIG_DIR`: without it the stored
keys cannot be read.

## Login

Every screen and action needs the one owner login. It comes from the
environment only:

| Variable | Purpose |
| --- | --- |
| `ALC_AUTH_USER` | The login user name. |
| `ALC_AUTH_PASSWORD` | The login password. It is hashed in memory at start and is never written to `settings.json`, shown or logged. |
| `ALC_AUTH_PASSWORD_FILE` | A file holding the password (a trailing line break is not part of it). A file any other user can read is reported at start, never refused. The app reads it once at start; the clear text never enters the environment. |
| `ALC_AUTH_PASSWORD_HASH` | A Werkzeug hash, compared without ever knowing the clear text. Generate one with `python -c 'from werkzeug.security import generate_password_hash as g; print(g("..."))'`. |
| `ALC_SECURE_COOKIES` | `true` adds the `Secure` flag to the session cookie. Off by default, because the web UI is plain HTTP on the LAN; turn it on when the app sits behind an HTTPS proxy. |
| `ALC_TRUSTED_PROXIES` | The number of reverse proxies in front of the app (default `0`). Set it only to a trusted proxy count; at `0`, `X-Forwarded-For` is ignored, and the first request that carries the header logs a warning because every client then shares the proxy's address. |

When more than one password source is set, the app logs the conflict and uses
`ALC_AUTH_PASSWORD_HASH` first, then `ALC_AUTH_PASSWORD_FILE`, then
`ALC_AUTH_PASSWORD`; an unreadable file, an empty file or a malformed hash
fails closed (HTTP 503) instead of opening with a guess. A plain password
shorter than 8 characters (the minimum the Unraid installer asks for) is
reported at start but never refused, so an existing installation keeps
working. The Unraid installer writes the password to
`<app data>/almali-library-curator.password` (0600) and puts only
`ALC_AUTH_PASSWORD_FILE` in the environment file.

There is no default login. **Without a user name and a usable password
source the app answers every screen and action with a "Login is not
configured" page (HTTP 503)** that names the two variables; `/healthz`
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
At `0`, a request that carries `X-Forwarded-For` logs
`forwarded_headers_without_trusted_proxies` once, because behind an undeclared
proxy every client shares the proxy's address and five bad logins lock
everyone out.

The session cookie is `HttpOnly` and `SameSite=Lax`. A session ends **12 hours
after the sign-in**, however much it is used: using a session never extends
it. The **Sign out** button ends **every** session, in every browser: a
session epoch in `$ALC_CONFIG_DIR/session_epoch` (owner-only, mode 0600,
written atomically) is folded into every session, so a cookie copied before
the sign-out is signed out afterwards; a missing or unreadable epoch file
never stops the app. If the epoch cannot be written during sign-out, this
browser's session is still cleared, the previous epoch stays in force, and
`POST /logout` answers HTTP 503 with a translated error; other sessions stay
valid until a later sign-out or their expiry. The cookie is stateless;
changing the password - or the hash or the file it comes from - signs every
session out. A session survives a
restart and is valid on every worker. After 5 failed sign-ins from one address
within 15 minutes, that address is locked out for 15 minutes (HTTP 429); the
guard keeps at most 1,024 addresses in memory, cleans the expired ones up
first and never evicts an address with an active lock. If all 1,024 slots hold
active locks or pending attempts, new unseen addresses are refused with HTTP
429 until a slot frees; already-tracked addresses keep working normally. The
published image is signed: the Unraid installer installs the
pinned release version (never `latest` unless you ask for it), verifies the
image signature with `cosign` before creating anything, and waits for the
container to report a healthy state before marking the install manifest
verified. When `cosign` is
not installed, the installer downloads the pinned `cosign` v2.6.5 for its
architecture and checks it against a sha256 digest embedded in the script; a
failed download or checksum stops it before anything is created.
`ALC_INSTALL_SKIP_SIGNATURE=1` is the loud air-gapped escape hatch. The
signing key in `$ALC_CONFIG_DIR/secret_key` is owner-only (mode 0600); a key
shorter than 32 bytes is replaced at start, which signs every session out.
Log lines never
carry the password: any structured field whose name contains `password`,
`token`, `secret`, `api_key`, `apikey`, `authorization` or `cookie` is
written as `***`.

## Security

- One owner login from the environment; no default credentials; fail closed
  with HTTP 503 when the login is missing.
- CSRF protection on every action, `HttpOnly` / `SameSite=Lax` session cookie,
  lockout after repeated failed sign-ins. A saturated lock table refuses new
  addresses with HTTP 429 instead of bypassing the guard.
- The Plex token is encrypted at rest (Fernet) and never shown in full; secrets
  are never logged.
- Outbound calls go only to the services you configure, plus `plex.tv` and
  `clients.plex.tv` for the Plex sign-in. Redirects are refused, so credentials
  are never re-sent to another host.
- The app never writes to your media directly: deletions go through Sonarr,
  Radarr or the Plex API.
- The container image installs its dependencies from committed, hash-pinned
  locks, so a compromised index or a new release cannot change what the
  signed image contains.
- Manual dispatch of the publish workflow on a branch runs CI but skips the
  publish job: it never logs in, pushes or signs an image. Only version-tag
  runs passing the [release checks](#releases) can publish.

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
