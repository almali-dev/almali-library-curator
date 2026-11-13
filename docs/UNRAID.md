# Almali Library Curator for Plex on Unraid

This guide deploys **Almali Library Curator for Plex** on Unraid: the
installer, the Community Applications (CA) template, updating, uninstalling
and the reverse-proxy advice.

> Not affiliated with or endorsed by Plex Inc. "Plex" is a trademark of
> Plex, Inc.

## What it is

One Docker image on GHCR (`ghcr.io/almali-dev/almali-library-curator`),
an Unraid installer script, and an Unraid CA template. The app shows what
nobody watches in a Plex library, says why in one sentence per item, and
removes **only** what the owner marks, after a grace period and a final
re-check. A fresh install starts in dry-run mode: nothing is ever removed
until real deletion is turned on in **Settings**.

| Container path | Host default | Mode | Why |
| --- | --- | --- | --- |
| `/config` | `/mnt/user/appdata/almali-library-curator` | rw | Settings, database, secrets, logs, backups |
| `/media` | The media share | ro | File sizes and hardlink counts; the app never writes media |
| `/unraid/mnt` | `/mnt` | ro, optional | Which array disk or pool holds each file |

The default web port is **17540** (one above Plex Smart Cache's 17539, so
both run side by side).

## Install with the script

### 1. Download and verify

Keep both scripts on persistent boot storage, not `/tmp` (which is lost on
reboot). Both scripts refuse to run when a checksum beside them does not
match. Download the scripts and checksums from the same release/ref.

```bash
mkdir -p /boot/config/plugins/almali-library-curator
cd /boot/config/plugins/almali-library-curator
curl -fsSLO https://raw.githubusercontent.com/almali-dev/almali-library-curator/main/install-unraid.sh
curl -fsSLO https://raw.githubusercontent.com/almali-dev/almali-library-curator/main/install-unraid.sh.sha256
curl -fsSLO https://raw.githubusercontent.com/almali-dev/almali-library-curator/main/uninstall-unraid.sh
curl -fsSLO https://raw.githubusercontent.com/almali-dev/almali-library-curator/main/uninstall-unraid.sh.sha256
sha256sum -c install-unraid.sh.sha256
sha256sum -c uninstall-unraid.sh.sha256
```

Each `sha256sum -c` prints the script name followed by `OK` when its download
is intact. Stop here if either check fails. Return to this persistent directory
when running the uninstaller or downloading updated scripts.

### 2. Run it

```bash
bash install-unraid.sh
```

The script verifies its own checksum again when an `install-unraid.sh.sha256`
file sits next to it, then asks:

| Question | Default | Notes |
| --- | --- | --- |
| App data directory | `/mnt/user/appdata/almali-library-curator` | Settings, database, logs, backups |
| Media share (read-only) | `/mnt/user/media` | Must exist; it is mounted read-only |
| Mount `/mnt` as `/unraid/mnt`? | no | Optional; it answers which array disk holds each file |
| Web UI port | `17540` | The container always listens on 17540 |
| Bind address | `0.0.0.0` | The address the web port is published on; `127.0.0.1` keeps it private |
| PUID / PGID | `99` / `100` | `nobody`:`users` on a stock Unraid; `0` is refused |
| Timezone | `Etc/UTC` | The time of the daily scan |
| Owner user name | `admin` | The one login of the app; line breaks are refused |
| Owner password | *(no default)* | Read silently, confirmed, never echoed |

The password is never passed on a command line, so it never appears in
`ps`, and it is never written to a log. It goes into the owner-only (0600)
environment file in the app data directory, together with the rest of the
answers.

### Non-interactive runs

Every answer can be given through an environment variable, and
`ALC_INSTALL_NONINTERACTIVE=1` turns the installer into a silent run (used
by automation):

| Variable | Meaning |
| --- | --- |
| `ALC_INSTALL_NONINTERACTIVE` | `1` never asks; a missing answer stops the run |
| `ALC_INSTALL_APPDATA` | App data directory |
| `ALC_INSTALL_MEDIA` | Media share |
| `ALC_INSTALL_UNRAID_MNT` | `yes`/`no`: mount `/mnt` read-only as `/unraid/mnt` |
| `ALC_INSTALL_PORT` | Web UI port |
| `ALC_INSTALL_BIND` | Address the web port is published on (`0.0.0.0`, the LAN, by default; `127.0.0.1` keeps it private) |
| `ALC_INSTALL_PUID` / `ALC_INSTALL_PGID` | The ids the container runs as (`0` is refused) |
| `ALC_INSTALL_TZ` | Timezone |
| `ALC_INSTALL_USER` / `ALC_INSTALL_PASSWORD` | The owner login |
| `ALC_INSTALL_TAG` | Image tag to pull (`latest` by default) |
| `ALC_INSTALL_IMAGE` | Full image reference, when it wins over the tag |
| `ALC_INSTALL_CONTAINER_NAME` | Container name |
| `ALC_INSTALL_REQUIRE_SIGNATURE` | `1` stops the installation when the image signature cannot be verified |
| `ALC_INSTALL_START_TIMEOUT` | Seconds the installer waits for the container to become healthy (`120` by default) |

Every answer must be given; defaults are not applied in this mode, except
the bind address, which keeps the LAN default (`0.0.0.0`) when it is not
set. Read the password without echoing it, so it never lands in the shell
history:

```bash
read -rsp 'Owner password: ' ALC_INSTALL_PASSWORD && echo
ALC_INSTALL_NONINTERACTIVE=1 \
ALC_INSTALL_APPDATA=/mnt/user/appdata/almali-library-curator \
ALC_INSTALL_MEDIA=/mnt/user/media \
ALC_INSTALL_UNRAID_MNT=yes \
ALC_INSTALL_PORT=17540 \
ALC_INSTALL_BIND=0.0.0.0 \
ALC_INSTALL_PUID=99 \
ALC_INSTALL_PGID=100 \
ALC_INSTALL_TZ=Europe/Madrid \
ALC_INSTALL_USER=owner \
ALC_INSTALL_PASSWORD="$ALC_INSTALL_PASSWORD" \
bash install-unraid.sh
unset ALC_INSTALL_PASSWORD
```

### What the installer creates

- the container `almali-library-curator`
  (`docker run -d --restart unless-stopped --env-file …`), labelled
  `ai.almali-dev.installer=install-unraid.sh` and published on the bind
  address (`-p 0.0.0.0:17540:17540` by default);
- the environment file
  `/mnt/user/appdata/almali-library-curator/almali-library-curator.env`,
  mode 0600;
- the session signing key
  `/mnt/user/appdata/almali-library-curator/secret_key`, mode 0600 and at
  least 32 random bytes, **only when it was missing** (an existing key is
  kept, so a restart never invalidates the sessions);
- the manifest
  `/mnt/user/appdata/almali-library-curator/install-manifest.json`, which
  records exactly what was created and whether the start was verified
  (`start_verified`).

Then open `http://<unraid-ip>:17540`, sign in with the owner login and
follow the first-run wizard to connect Plex.

## The image signature and the first start

The published image carries a keyless signature: the certificate is the
publish workflow of this repository on a release tag. When `cosign` is on
the PATH, the installer verifies the image before it creates the container:

```bash
cosign verify \
  --certificate-identity-regexp '^https://github.com/almali-dev/almali-library-curator/\.github/workflows/publish\.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/almali-dev/almali-library-curator:latest
```

- With `cosign` on the PATH the verification is **mandatory**: a failed
  verification stops the installation, names the image and creates nothing.
- Without `cosign` the installer prints a warning and continues. Set
  `ALC_INSTALL_REQUIRE_SIGNATURE=1` to make a missing `cosign` stop the
  installation instead.
- After the container is created, the installer waits for it to report a
  healthy state. The wait is bounded: 120 seconds by default
  (`ALC_INSTALL_START_TIMEOUT`). An unhealthy container, an exited
  container or the timeout stops the installation: the container is named,
  the way to read its logs is printed (`docker logs <container>`) and no
  completion message is printed.
- The manifest is written **before `docker run`**, with
  `"start_verified": false`; it is set to `true` only once the container is
  healthy. Even a rejected `docker run` (for example, an occupied port) leaves
  a manifest the uninstaller can recover. The manifest names the intended
  container and created files, so `uninstall-unraid.sh` removes any container
  that exists plus the environment file and installer-created signing key;
  the installer can then be run again.

## Install from the Community Applications template

The template is `unraid-template.xml` in the repository. It carries the
repository, the registry, the support and project links, the category, the
web UI (`http://[IP]:[PORT:17540]/`) and an icon that really exists in the
repository.

To use it:

1. Unraid WebUI → **Docker** → **Add Container** → **Template**;
2. paste the template URL
   `https://raw.githubusercontent.com/almali-dev/almali-library-curator/main/unraid-template.xml`
   (or upload the file with **Template** → **Upload**);
3. fill in the owner login: `ALC_AUTH_USER` and `ALC_AUTH_PASSWORD` are
   required, and the password is masked;
4. check the paths: `/config` (rw), `/media` (ro) and, if you want the disk
   column to name the array disk, `/unraid/mnt` (ro) plus
   `ALC_UNRAID_ROOT=/unraid/mnt`;
5. **Apply**.

A template install has no manifest, so the uninstaller below cannot remove
it: remove the container from the Unraid WebUI instead.

## Update

For a **script installation**, download and verify the current scripts as above,
then use the manifest-driven upgrade. `cosign` and `jq` must be on PATH;
unlike a fresh install, upgrades always require signature verification.

```bash
cd /boot/config/plugins/almali-library-curator
bash install-unraid.sh --upgrade --app-data /mnt/user/appdata/almali-library-curator
```

To select a release, prefix that command with `ALC_INSTALL_TAG=vX.Y.Z` (or
`ALC_INSTALL_IMAGE=<full image reference>`). Without either, it re-pulls the
manifest's recorded image (`latest` for a default install; an explicitly pinned
tag stays pinned). The installer verifies and runs the same immutable digest.
Other configuration answers are ignored: the manifest supplies the container name, custom app data
and media paths, port, bind address and optional `/unraid/mnt` mount. Installer
labels, the owner-only environment file (including PUID/PGID/timezone), the
signing key and its original ownership flag are kept.

Before replacing the container, the script stops it and creates an owner-only
`<app data>/backups/pre-upgrade-<timestamp>-<unique>.tar`. It includes the
database and SQLite sidecars, settings, credentials, signing/encryption keys
and original manifest, but excludes previous backups. A backup failure keeps
the old container; an archive failure restarts it if it was running. No
container replacement occurs without a completed backup. A failed replacement
still leaves an incomplete manifest; use the uninstaller or retry `--upgrade`.
Keep backups private and copy them off the server.

Existing v1 manifests are supported: if the optional mount answer is absent,
the installer recovers it from `ALC_UNRAID_ROOT` in the existing environment
file without evaluating that file as shell code. A missing/invalid manifest
is refused before replacing anything. Template installs have no manifest:
back up first, then update through the Unraid Docker UI, which retains the
template configuration. The template opts into Watchtower, but automated
updates do **not** provide this script's signature and backup gates.

## Backup and restore

**Back up before every update.** The upgrade command above makes a cold app
data archive automatically. For a manual backup (also for a template install):

1. Stop the container in the Unraid Docker UI; wait until it is stopped.
2. Archive the **entire app data directory**, not just a live `.db` file.
   Include settings, `secret_key`, `secrets.key`, `secrets.json`, the environment
   file and manifest when present, and SQLite `-wal`/`-shm` sidecars. For example,
   with the default paths and no symlinked/external configuration files:

   ```bash
   (umask 077; tar -C /mnt/user/appdata/almali-library-curator \
     --exclude='./backups' -cf /mnt/user/appdata/curator-backup.tar .)
   ```

3. Check the archive with `tar -tf /mnt/user/appdata/curator-backup.tar`, copy
   it to private off-server storage, then start the container again. Back up
   any symlink targets or externally configured database separately.

**Restore or downgrade:** never open a migrated database with an older image.
Stop the container, preserve the failed app data directory separately, and
restore a matching **pre-upgrade** archive into an empty directory at the
original app data path. For example, after preparing that empty directory:

```bash
tar -C /mnt/user/appdata/almali-library-curator \
  -xf /path/to/private/pre-upgrade-backup.tar
```

Keep the restored file ownership and permissions. For a script installation,
run `--upgrade` with `ALC_INSTALL_TAG` set to the release that made the backup;
it reuses the restored manifest, verifies that image and makes another backup
before replacement. For a template installation, select the matching image tag
in the Docker UI before starting. Verify health and the settings/history before
allowing real deletion. Restoring the encryption keys with the database is
required to recover stored credentials.

## Uninstall

Run from `/boot/config/plugins/almali-library-curator`, where both verified
scripts were downloaded (download and verify them again if missing).

The uninstaller removes exactly what the installer created, as recorded in
the manifest:

```bash
bash uninstall-unraid.sh --app-data /mnt/user/appdata/almali-library-curator
```

It removes the container and the environment file, and the signing key
only when the installer created it. A failed installation leaves the same
manifest, marked `"start_verified": false`, so the uninstaller removes it
too. It then asks whether the app data
directory (settings, database, logs) may be deleted; the default is to
**keep** it. An unattended run (`ALC_UNINSTALL_NONINTERACTIVE=1` or `-y`)
never asks and always keeps it.

The uninstaller refuses to touch anything when there is no manifest, and it
never writes inside the media share: the media is never touched.

## Reverse proxy

- Keep the app port **private** when `ALC_TRUSTED_PROXIES` is above `0`:
  with a trusted proxy the client address comes from `X-Forwarded-For`,
  so a client that reaches the port directly could spoof it. Bind the port
  to the loopback address, or let only the proxy reach it: answer `127.0.0.1`
  to the *Bind address* question of the installer (or set
  `ALC_INSTALL_BIND=127.0.0.1` in a non-interactive run and in the compose
  example), or let only the proxy reach the port.
- Set `ALC_SECURE_COOKIES=true` when the proxy serves the app over HTTPS,
  so the session cookie is only sent over HTTPS.
- `ALC_TRUSTED_PROXIES` is the number of proxies in front of the app (`0`
  by default, which ignores the header entirely).

The proxy must forward the client address in `X-Forwarded-For` and the
scheme in `X-Forwarded-Proto`.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Every screen answers 503 | `ALC_AUTH_USER` and `ALC_AUTH_PASSWORD` are missing from the environment file |
| No disk in the file rows | The optional `/unraid/mnt` mount and `ALC_UNRAID_ROOT` are not set |
| Plex reports paths the app never sees | Set `ALC_PLEX_PATH_MAP=/data=/media,/tv=/media/tv`: absolute `source=target` pairs separated by commas. Empty means no mapping; malformed entries raise a visible `ValueError` on startup instead of being ignored |
| The installer refuses to run | The checksum does not match: download it again and check it with `sha256sum -c` |
| Nothing is ever removed | The app starts in dry-run mode; turn real deletion on in **Settings** |
| The installer refuses the image | The signature could not be verified: `cosign` is missing (`ALC_INSTALL_REQUIRE_SIGNATURE=1`) or the image is not signed |
| The installer stops after the container is created | The container never became healthy: read its logs with `docker logs almali-library-curator`. The manifest records the incomplete installation (`start_verified: false`); `bash uninstall-unraid.sh --app-data /mnt/user/appdata/almali-library-curator` removes the container, the environment file and the signing key |
