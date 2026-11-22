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
| `/config` | `/mnt/user/appdata/almali-library-curator` | rw | Settings, database, secrets and backups; logs go to `docker logs` |
| `/media` | The media share | ro | File sizes and hardlink counts; the app never writes media |
| `/unraid/mnt` | `/mnt` | ro, optional | Which array disk or pool holds each file |

The default web port is **17540** (one above Plex Smart Cache's 17539, so
both run side by side).

## Install with the script

### 1. Download and verify

Keep both scripts on persistent boot storage, not `/tmp` (which is lost on
reboot). Both scripts refuse to run when a checksum beside them does not
match. Download the installer from the immutable release tag, never from
`main`: the tag carries the image version the script installs and the
checksum that matches that exact script, while `main` moves.

```bash
VERSION=0.41.0   # the release tag, without the leading "v"
BASE="https://raw.githubusercontent.com/almali-dev/almali-library-curator/v${VERSION}"
mkdir -p /boot/config/plugins/almali-library-curator
cd /boot/config/plugins/almali-library-curator
curl -fsSLO "${BASE}/install-unraid.sh"
curl -fsSLO "${BASE}/install-unraid.sh.sha256"
curl -fsSLO https://raw.githubusercontent.com/almali-dev/almali-library-curator/main/uninstall-unraid.sh
curl -fsSLO https://raw.githubusercontent.com/almali-dev/almali-library-curator/main/uninstall-unraid.sh.sha256
sha256sum -c install-unraid.sh.sha256
sha256sum -c uninstall-unraid.sh.sha256
```

Each `sha256sum -c` prints the script name followed by `OK` when its download
is intact. Stop here if either check fails. Return to this persistent directory
when running the uninstaller or downloading updated scripts.

A checksum proves the download is intact, not who wrote it. The publish
workflow signs the installer with the same keyless identity as the image and
publishes the signature bundle next to the image in GHCR for that exact
version (pre-1.0 publishes no GitHub Release). Fetch the bundle with the
docker you already need, then verify the script before running it:

```bash
docker rm -f alc-installer-bundle 2>/dev/null || true
docker create --name alc-installer-bundle \
  "ghcr.io/almali-dev/almali-library-curator:installer-${VERSION}"
docker cp alc-installer-bundle:/install-unraid.sh.sigstore.json .
docker rm alc-installer-bundle
cosign verify-blob \
  --bundle install-unraid.sh.sigstore.json \
  --certificate-identity-regexp "^https://github.com/almali-dev/almali-library-curator/\.github/workflows/publish\.yml@refs/tags/v${VERSION}\$" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  install-unraid.sh
```

`cosign verify-blob` prints the verified claims and exits `0` only when the
bundle signs exactly this `install-unraid.sh` with the publish workflow
identity for tag `v${VERSION}` and GitHub's OIDC issuer. Stop if it fails.
Stock Unraid has no `cosign`: install it before this check, or download the
pinned `cosign` v2.6.5 release binary and check it against the published
`cosign_checksums.txt`. The installer fetches and checks that same pinned
`cosign` by itself before it verifies the image (next section), so the two
checks use one version.

### 2. Run it

```bash
bash install-unraid.sh
```

The script verifies its own checksum again when an `install-unraid.sh.sha256`
file sits next to it, then asks:

| Question | Default | Notes |
| --- | --- | --- |
| App data directory | `/mnt/user/appdata/almali-library-curator` | Settings, database, secrets and backups |
| Media share (read-only) | `/mnt/user/media` | Must exist, and it is mounted read-only |

Both path answers must be absolute and must not contain `:` or `,`. Docker
splits a bind (`-v SRC:DST[:MODE]`) and the options of a `--mount` on those
characters, so such a path would attach the wrong directory to `/config` or
`/media`; the installer refuses it instead of starting a container with the
wrong mounts.
| Mount `/mnt` as `/unraid/mnt`? | no | Optional; it answers which array disk holds each file |
| Web UI port | `17540` | The container always listens on 17540 |
| Bind address | `0.0.0.0` | The address the web port is published on; `127.0.0.1` keeps it private |
| PUID / PGID | `99` / `100` | `nobody`:`users` on a stock Unraid; `0` is refused |
| Timezone | `Etc/UTC` | The time of the daily scan |
| Owner user name | `admin` | The one login of the app; line breaks are refused |
| Owner password | *(no default)* | Read silently, confirmed, never echoed |

The password is never passed on a command line, so it never appears in
`ps`, and it is never written to a log. It goes into its own owner-only (0600)
file in the app data directory, `almali-library-curator.password`; the
environment file only names it with `ALC_AUTH_PASSWORD_FILE`, so the clear
text is never part of the container environment.

### Non-interactive runs

Every answer can be given through an environment variable, and
`ALC_INSTALL_NONINTERACTIVE=1` turns the installer into a silent run (used
by automation):

| Variable | Meaning |
| --- | --- |
| `ALC_INSTALL_NONINTERACTIVE` | `1` never asks; a missing answer stops the run |
| `ALC_INSTALL_APPDATA` | App data directory (absolute, no `:` or `,`) |
| `ALC_INSTALL_MEDIA` | Media share (absolute, no `:` or `,`, must exist) |
| `ALC_INSTALL_UNRAID_MNT` | `yes`/`no`: mount `/mnt` read-only as `/unraid/mnt` |
| `ALC_INSTALL_PORT` | Web UI port |
| `ALC_INSTALL_BIND` | Address the web port is published on (`0.0.0.0`, the LAN, by default; `127.0.0.1` keeps it private) |
| `ALC_INSTALL_PUID` / `ALC_INSTALL_PGID` | The ids the container runs as (`0` is refused) |
| `ALC_INSTALL_TZ` | Timezone |
| `ALC_INSTALL_USER` / `ALC_INSTALL_PASSWORD` | The owner login |
| `ALC_INSTALL_TAG` | Image tag to pull (`0.41.0`, the pinned release version, by default) |
| `ALC_INSTALL_IMAGE` | Full image reference, when it wins over the tag |
| `ALC_INSTALL_CONTAINER_NAME` | Container name |
| `ALC_INSTALL_REQUIRE_SIGNATURE` | Accepted for compatibility; verification is mandatory now, so it changes nothing |
| `ALC_INSTALL_SKIP_SIGNATURE` | `1` skips the signature check with a loud warning: the documented air-gapped escape hatch, never for a connected host |
| `ALC_INSTALL_START_TIMEOUT` | Seconds the installer waits for the container to become healthy (`120` by default) |
| `ALC_INSTALL_UPGRADE_ALLOW_EXTERNAL` | `1` lets an upgrade continue when paths that resolve outside `/config` inside the container would not be in the automatic backup (back them up yourself first) |

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
  address (`-p 0.0.0.0:17540:17540` by default). It runs with least
  privilege: `--cap-drop ALL` with only `CHOWN`, `DAC_OVERRIDE`, `SETUID` and
  `SETGID`
  added (the entrypoint needs them to give `/config` to `PUID`:`PGID` and
  drop to it), `--security-opt no-new-privileges:true`, a read-only root
  filesystem and a `/tmp` tmpfs for Gunicorn's worker heartbeat;
- the environment file
  `/mnt/user/appdata/almali-library-curator/almali-library-curator.env`,
  mode 0600. It names the password file (`ALC_AUTH_PASSWORD_FILE`); the
  password itself is not in it, so it is not part of the container
  environment (`docker inspect`, `/proc/1/environ`);
- the password file
  `/mnt/user/appdata/almali-library-curator/almali-library-curator.password`,
  mode 0600, which the uninstaller removes with the rest;
- the session signing key
  `/mnt/user/appdata/almali-library-curator/secret_key`, mode 0600 and at
  least 32 random bytes, **only when it was missing** (an existing key is
  kept, so a restart never invalidates the sessions);
- the manifest
  `/mnt/user/appdata/almali-library-curator/install-manifest.json`, which
  records exactly what was created, whether the start was verified
  (`start_verified`), the run that created it (`install_attempt`) and the
  state of an upgrade (`upgrade_state`: `none`, `upgrading`, `rolled-back`
  or `complete`).

Then open `http://<unraid-ip>:17540`, sign in with the owner login and
follow the numbered first-run steps on the home page: connect Plex, run the
first analysis, choose the managed libraries and add the optional connections.
The header says which mode the app is in, when it last analysed and when it
runs next; the list of steps disappears once Plex is connected and the first
analysis has finished.

## The image signature and the first start

The published image carries a keyless signature: the certificate is the
publish workflow of this repository on a release tag. A fresh install pulls
the image tag pinned in the installer (the release version, `0.41.0` here),
never the moving `latest` alias; `ALC_INSTALL_TAG` or `ALC_INSTALL_IMAGE`
selects another. The installer verifies the signature before it creates the
container:

```bash
cosign verify \
  --certificate-identity-regexp '^https://github.com/almali-dev/almali-library-curator/\.github/workflows/publish\.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/almali-dev/almali-library-curator:0.41.0
```

- Verification is **mandatory** and uses the pulled digest, not a tag: a
  failed verification stops the installation, names the image and creates
  nothing.
- A failed pull also stops the installation before anything is created. When
  the registry answers `denied`, `unauthorized`, `not found` or `manifest
  unknown`, the installer prints the registry's own answer and the two ways
  out: the package is public (the owner changes it under **Package settings →
  Change visibility → Public**, because package visibility is separate from
  repository visibility), or this host signs in with `docker login ghcr.io`
  and a token that has `read:packages`. The Docker path in
  [README.md](../README.md#docker) carries the same hint and the anonymous
  `docker pull` that proves the package is public.
- When `cosign` is not on the PATH, the installer downloads the pinned
  `cosign` **v2.6.5** for the host architecture (linux-amd64 or linux-arm64)
  from its GitHub release, checks it against a sha256 digest embedded in the
  script, uses that copy and removes the temporary directory. A failed
  download or a mismatched digest stops the run before anything is created.
- `ALC_INSTALL_REQUIRE_SIGNATURE` is still accepted but no longer changes
  anything: the check it used to request is the default.
- `ALC_INSTALL_SKIP_SIGNATURE=1` skips the check with a loud warning. Use it
  only on an air-gapped host you trust: nothing then proves the image is the
  published one.
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
  container and the created files, so `uninstall-unraid.sh` removes that
  container plus the environment file, the password file and the
  installer-created signing key;
  the installer can then be run again. There are two exceptions. A fresh
  install that finds the container name already taken by another installation
  removes what it created and leaves no manifest at all (an upgrade keeps it;
  see the upgrade section). And a manifest that names a
  container whose ownership label does not match is not removed by the
  uninstaller: it refuses, names the mismatch and stops without removing
  anything. In both cases the uninstaller can never remove a container this
  installation did not create.

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
   required, and the password is masked. To keep the clear text out of the
   container environment, add `ALC_AUTH_PASSWORD_HASH` (a Werkzeug hash) or
   `ALC_AUTH_PASSWORD_FILE` (an owner-only file inside `/config`) under
   **Extra Parameters** instead;
4. check the paths: `/config` (rw), `/media` (ro) and, if you want the disk
   column to name the array disk, `/unraid/mnt` (ro) plus
   `ALC_UNRAID_ROOT=/unraid/mnt`;
5. **Apply**.

The template's variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `ALC_AUTH_USER`, `ALC_AUTH_PASSWORD` | `admin`, none | The owner login. The password is masked and never written to `settings.json`. `ALC_AUTH_PASSWORD_HASH` (a Werkzeug hash) or `ALC_AUTH_PASSWORD_FILE` (an owner-only file) can replace the plain password when you add the variable yourself. |
| `ALC_LOCALE` | empty | Interface and reason language: `en`, `ca` or `es`. Empty (the template default) leaves the language chosen in **Settings**; a value wins over it. |
| `ALC_UNRAID_ROOT` | none | Container path of the `/mnt` mount (`/unraid/mnt`), so the report names the array disk. |
| `ALC_PLEX_PATH_MAP` | none | Absolute `source=target` path pairs, comma-separated, when Plex reports other paths than the container sees. |
| `ALC_TRUSTED_PROXIES`, `ALC_SECURE_COOKIES` | `0`, `false` | Reverse-proxy hops and the `Secure` cookie flag, for an HTTPS proxy. |

A template install has no manifest, so the uninstaller below cannot remove
it: remove the container from the Unraid WebUI instead.

The template runs the container with the same least-privilege options as the
installer (`--cap-drop ALL` plus `CHOWN`, `DAC_OVERRIDE`, `SETUID` and
`SETGID`,
`--security-opt no-new-privileges:true`, `--read-only` and a `/tmp` tmpfs).
It also carries the label `com.centurylinklabs.watchtower.enable=true`, so
it is updated automatically when a new stable release is published, the same
way Plex Smart Cache is (see **Automatic updates** below).

## Update

### Automatic updates (template install)

The template image is `ghcr.io/almali-dev/almali-library-curator:latest`, and
the publisher moves `latest` only when a **stable** release is published and
it is the newest stable one; a prerelease never moves it. Publishing is
automatic: pushing a `vX.Y.Z` tag builds, signs and pushes the image and
moves `latest`. Something on the host has to pull it. Use one of these two:

- **Watchtower, label-scoped.** The template container opts in with the
  label `com.centurylinklabs.watchtower.enable=true`. Run one Watchtower on
  the host with `--label-enable`, so it only touches the containers that opt
  in. The original `containrrr/watchtower` is archived and stops on Docker
  29 ("client version 1.25 is too old"); use a maintained fork that keeps
  the same label, for example:

  ```bash
  docker run -d --name watchtower --restart unless-stopped \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -e TZ=Europe/Madrid \
    nickfedor/watchtower --label-enable --cleanup --schedule "0 0 5 * * *"
  ```

  `--schedule` takes six cron fields (seconds first): this one checks every
  day at 05:00. Watchtower recreates the container with the same
  configuration from the new image.
- **CA Auto Update Applications.** The Unraid plugin from Community
  Applications updates the Docker containers you select on its own
  schedule. Select `almali-library-curator` in its settings; it needs no
  label.

What this needs from the host:

- **The image must be pullable by the host.** A public package is pulled
  anonymously; a private one needs `docker login ghcr.io` with a token that
  has `read:packages`, in the host's own Docker configuration.
- **A template install, not a script install.** The script pins the image
  by digest, which never moves, so no updater changes it; use its
  `--upgrade` path instead.
- **A backup you can restore.** An automatic update does not verify the
  image signature and does not take the pre-upgrade backup the script
  install does. The app data is a bind mount, so the database, the settings
  and the secrets survive the update, and a new release migrates the
  database on its first start. Back up the app data regularly (see
  **Backup and restore**).

To stop automatic updates, pin a release in the template (`Repository` set to
`ghcr.io/almali-dev/almali-library-curator:X.Y.Z`) or remove the label from
**Extra Parameters**, then update it yourself with **Docker** → **Update**.

After an update, the running version is visible in three places: the About
page, the `application_started` line of the container log and the image label.

For a **script installation**, download and verify the current scripts as above,
then use the manifest-driven upgrade. `jq` must be on PATH; `cosign` is fetched
and checked by the script when it is missing, exactly like a fresh install,
and signature verification is mandatory for an upgrade too.

```bash
cd /boot/config/plugins/almali-library-curator
bash install-unraid.sh --upgrade --app-data /mnt/user/appdata/almali-library-curator
```

To select a release, prefix that command with `ALC_INSTALL_TAG=0.41.0` (or
`ALC_INSTALL_IMAGE=<full image reference>`). Without either, it re-pulls the
manifest's recorded image, so an existing install that was created before the
version pin keeps following `latest` and is never switched silently. A newer
release is installed either by running the installer downloaded from its tag
(it pins that version) or by setting `ALC_INSTALL_TAG` to it. The installer
verifies and runs the same immutable digest.
Other configuration answers are ignored: the manifest supplies the container name, custom app data
and media paths, port, bind address and optional `/unraid/mnt` mount. Installer
labels, the owner-only environment file (including PUID/PGID/timezone), the
password file, the signing key and its original ownership flag are kept.

The recorded app data and media paths pass the same check as the prompted
answers: each must be absolute and must not contain `:` or `,`. A manifest
whose paths were edited by hand is refused before anything is stopped,
archived or bound, and the refusal names the path.

Before replacing the container, the script stops it and creates an owner-only
`<app data>/backups/pre-upgrade-<timestamp>-<unique>.tar`. That archive is the
**app data directory** and nothing else: the database and its SQLite
sidecars, the settings, the credentials, the signing and encryption keys and
the original manifest, but never a previous backup.

A file that lives **outside** the app data directory is not in that archive:
the target of a symlink inside the app data directory, an external
`ALC_DATABASE_PATH` or an external `ALC_CONFIG_DIR`. Those two variables are
read **inside the container**, where the app data directory is `/config`, so
the upgrade resolves every path in the **container namespace**: a path that
resolves inside `/config` is covered by the archive, a path that resolves
outside it is not, even when the same name does not exist on the host. A
relative path is resolved the way the application resolves it, against the
working directory of the image (`/`). The upgrade **refuses** before it
stops or changes anything when it finds a path outside `/config`, and it
names the paths. Back them up yourself (or move them into the app data
directory) and run the upgrade again, or set
`ALC_INSTALL_UPGRADE_ALLOW_EXTERNAL=1` to continue after your own backup. The
upgrade cannot restore what it never archived.

The upgrade then records the replacement in the manifest
(`"upgrade_state": "upgrading"`, `"start_verified": false`) **before** the old
container is removed, so an interruption never leaves a stale verified
manifest behind. If the old container cannot be removed, the upgrade puts it
back to work when it was running (a container that was already stopped stays
stopped), records `"upgrade_state": "rolled-back"` and stops: a running
installation stays available. When `docker start` itself fails the upgrade
can only warn ("Could not restart ...; start it manually"), and the
installation stays stopped until it is started by hand. A failed backup leaves
no archive at all (the
half written one is removed) and restarts the old container when it had been
running (a container that was already stopped stays stopped). No container
replacement occurs without a completed backup. A failed container creation
still leaves an incomplete manifest (`"start_verified": false`); use the
uninstaller or retry `--upgrade`. Keep backups private and copy them off the
server.

Every container the installer creates carries the label
`ai.almali-dev.install-attempt`, and the manifest records the same value in
`install_attempt`. The upgrade only replaces the container that value names:
a container with another attempt's label is refused ("the manifest container
was not created by this installation") and nothing is changed. The
uninstaller applies the same rule before it removes anything: it compares the
ownership label of the container the manifest names with the attempt the
manifest records, and it refuses to remove a container whose label does not
match. A manifest written by an older installer records no attempt: it may
only replace or remove a container that carries no ownership label either,
and the upgrade records the new attempt in the manifest it writes. A run that
finds the container name already taken by **another installation** removes
what it created, so the recovery procedure below can never remove a container
this installation did not create. On a fresh install it also leaves no
manifest at all. An **upgrade** reaches that rollback only after the old
container was already removed, so it keeps the manifest together with the
environment file, the password file, the signing key and the app data; the manifest records the
replacement (`"upgrade_state": "upgrading"`, `"start_verified": false`) and
the attempt of the failed run. The uninstaller therefore still verifies the
ownership label, refuses to remove the container of the other installation
and names the mismatch; once that container is gone, the same manifest lets
it finish the cleanup (`bash uninstall-unraid.sh --app-data …`).

A failed or partial manifest write never replaces a valid manifest: the
manifest is streamed into a temporary file inside the app data directory, the
write is checked, and only a complete one is moved over the previous file. A
failed write removes the temporary file, leaves the previous manifest
unchanged and stops the run with an error that names the manifest (for
example, a full app data share).

Existing v1 manifests are supported: if the optional mount answer is absent,
the installer recovers it from `ALC_UNRAID_ROOT` in the existing environment
file without evaluating that file as shell code. A missing/invalid manifest
is refused before replacing anything. Template installs have no manifest:
back up first, then update through the Unraid Docker UI, which retains the
template configuration. Neither an automatic update nor one you start
yourself in the Docker UI provides this script's signature and backup gates.

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
   any symlink targets or externally configured database separately: they
   live outside the app data directory, so neither this manual archive nor
   the automatic archive of an upgrade contains them.

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

It removes the container, the environment file and the password file, and the
signing key only when the installer created it. The container it removes is the one the
manifest names, and only when that container carries the ownership label
`ai.almali-dev.install-attempt` the manifest records in `install_attempt`: a
container of another installation carries a different label, so the
uninstaller refuses to remove it, names the mismatch and stops without
removing anything. When `docker inspect` itself fails, the refusal reports
docker's own error as the cause and removes nothing; a transient docker
failure is never diagnosed as a missing ownership label. A manifest written
by an older installer records no
attempt, and it may only remove a container that carries no ownership label
either. A fresh install that found the container name taken by another
installation rolled itself back and left no manifest, so the uninstaller
cannot remove a container of another installation; an upgrade in the same
rare case keeps its manifest (see the upgrade section), so the uninstaller
verifies the ownership label, refuses the foreign container and can finish
the cleanup once the name is free. A failed installation
leaves the same manifest, marked `"start_verified": false`, so the uninstaller
removes it too, unless the run rolled itself back and left no manifest at
all. It then asks whether the app data
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
| Every screen answers 503 | Neither a usable owner user nor a password source is configured: the environment file names `ALC_AUTH_USER` and an `ALC_AUTH_PASSWORD*` variable, and the password file it points at exists and is readable |
| No disk in the file rows | The optional `/unraid/mnt` mount and `ALC_UNRAID_ROOT` are not set |
| Plex reports paths the app never sees | Set `ALC_PLEX_PATH_MAP=/data=/media,/tv=/media/tv`: absolute `source=target` pairs separated by commas. Empty means no mapping; malformed entries raise a visible `ValueError` on startup instead of being ignored |
| The installer refuses to run | The checksum does not match: download it again and check it with `sha256sum -c` |
| Nothing is ever removed | The app starts in dry-run mode; turn real deletion on in **Settings** |
| The pull answers denied, unauthorized or manifest unknown | The image package is private or this host is not signed in. The package is meant to be public (`docker pull ghcr.io/almali-dev/almali-library-curator:0.41.0` needs no login); the owner changes the visibility in **Package settings → Change visibility → Public**, or the host runs `docker login ghcr.io` with a token that has `read:packages`. Package visibility is separate from repository visibility |
| The installer refuses the image | The signature could not be verified: the pinned `cosign` could not be downloaded or did not match the sha256 embedded in the script, or the image is not signed. `ALC_INSTALL_SKIP_SIGNATURE=1` skips the check on an air-gapped host, with a loud warning |
| Every login is locked out at once behind a proxy | Set `ALC_TRUSTED_PROXIES` to the number of proxies in front of the app; the log reports `forwarded_headers_without_trusted_proxies` once while it is `0` and `X-Forwarded-For` arrives |
| The installer stops after the container is created | The container never became healthy: read its logs with `docker logs almali-library-curator`. The manifest records the incomplete installation (`start_verified: false`); `bash uninstall-unraid.sh --app-data /mnt/user/appdata/almali-library-curator` removes the container, the environment file, the password file and the signing key |
