# Curator v0.32.0 — first installation on Tower
Install the published v0.32.0 image on the owner's Unraid in dry-run mode; branch `feature/almali-library-curator-v1`.
Delivery: one focused deployment-evidence slice, no application/source or workflow changes; verify installer checksum, image signature, health, login and dry-run. No meaningful test-first RED applies to deploying an unchanged release.

## Specs

### S1 — Owner authorizes installation, not just publication

Owner, verbatim:

> no ho recordo. Revisa-ho tu

The parent found no identifiable Curator container (including stopped containers) or official image on Tower. The parent proposed: "El següent pas seria **instal·lar Curator en mode dry-run**, sense activar eliminacions, per començar la prova real T17. Vols que ho fem?"

Owner, verbatim:

> fes-ho

Acceptance: deploy existing v0.32.0 without enabling real deletion or modifying existing Plex, Sonarr, Radarr or Maintainerr services. This installation is the beginning of T17, not completion of the owner's field-observation period.

### S2 — Exact authorized targets and initial login

The parent asked, verbatim:

> Autoritzes la instal·lació a Tower amb dades a /mnt/cache4tb/appdata/almali-library-curator, lectura dels mitjans a /mnt/user/Plex_media, fitxers temporals a /tmp/almali-curator-v032-install i accés web LAN al port 17540? Tria com crear l’usuari admin; tot quedarà en dry-run.

Owner selected:

> Generar una contrasenya segura (recomanat)

Option consequence, verbatim:

> La genero al servidor i queda al fitxer d’entorn protegit. Et donaré la instrucció per consultar-la des del teu terminal, sense mostrar-la al xat.

Use admin, a cryptographically random password generated on Tower, owner-only environment/signing-key/manifest files; never print credentials in tool logs, process arguments, repository artifacts or chat. Read media only; do not connect Plex automatically or borrow credentials from existing services. Use Europe/Madrid (observed Plex timezone), PUID 99 and PGID 100 (installer defaults), and LAN bind 192.168.1.165:17540. Optional /mnt mount is not requested and stays disabled.

### S3 — Verification and handoff

The parent stated, verbatim:

> No hi ha `cosign`; prepararé un verificador temporal, sense instal·lar-lo globalment.

Use the release-tag installer/checksum, verify the exact pulled digest with a temporary checksummed official cosign binary, and require signature verification. Stop on conflicts or failed validation rather than overwriting previous data. Observe container health, installed version 0.32.0, media mount read-only, dry-run enabled and a working owner login (credentials remain server-side). Independent verification is required for Risk: item 5 (deployment/environment) and item 2 (initial credentials/access); verification must be read-only and must not connect or mutate Plex. Return the UI URL and a credential-retrieval command for the owner to run privately. Do not enable automatic updates or make a public exposure/reverse-proxy change.

### S4 — Private registry authentication, owner-entered credential

Owner selected:

> Mantenir-la privada i autenticar Tower

Option consequence, verbatim:

> Preparem accés de només lectura al registre, amb la credencial introduïda de manera segura fora del xat; no canviem la visibilitat.

Keep the package private. The owner enters their GitHub username and a package-read token directly in their own SSH terminal; never request the token in chat, read its contents into agent output, or borrow credentials from another service. Confine Docker authentication to /tmp/almali-curator-v032-install/.docker with directory mode 0700 and config.json mode 0600. Resume installer/signature verification with DOCKER_CONFIG set to that path only after the owner's login succeeds. Do not modify Tower's global Docker credentials or GitHub package/repository visibility.

## Tasks

| ID | Outcome | Specs | Route | Status | Commit / evidence |
| --- | --- | --- | --- | --- | --- |
| T1 | Confirm authorized directories, port, release installer and temporary verifier prerequisites | S1–S3 | parent | done | job-15 PASS; cosign v3.1.4 checksum/version PASS; job-18 transferred exact release installer, explicit and published checksum checks PASS, bash syntax PASS; raw URL 404 corrected without changing installer |
| T2 | Install verified v0.32.0 in dry-run using generated server-side credentials | S1–S3 | parent | blocked | job-19 exit 1: anonymous GHCR pull returned unauthorized; owner chose private registry authentication; waiting for owner to enter a read:packages token in their own SSH terminal using isolated Docker config; no appdata/env/container created |
| T3 | Independently verify deployment, then parent spot-check | S1–S3 | gentle-ai-verify + parent | pending | Risk: item 5 (deployment), item 2 (credentials/access); no application-source change |
| T4 | Record deployment evidence and hand off UI/login instructions | S1–S3 | parent | pending | preserve T17 field observation as owner follow-up |

## Log

### L1 — Original installation request

> fes-ho

### L2 — Exploration and exact consent

Read-only SSH to root@tower returned hostname Tower. Docker has no identifiable Curator container or official Curator image. Tower is x86_64, Docker 29.8.2, Unraid OS 7.3. Plex mounts /mnt/user/Plex_media at /data; its TZ is Europe/Madrid. Port 17540 has no listener or Docker binding. curl, sha256sum, openssl and ss are present, cosign is absent. No existing service was changed.

Owner selected `generated_admin_password` in the exact S2 authorization question. This explicitly permits the named deployment targets and server-side password generation, not any unrelated directory/configuration edit. The released image is v0.32.0 from commit b6a90fd803b5db5af701c94c4eb61ef5ac0f6a86; Publish run 43107223065 succeeded (multi-arch build/push and signing). Existing release tag will not be moved.

Forecast: fewer than 200 authored documentation lines; existing installer is run unchanged, no application code or CI changes planned. Deployment evidence is a focused slice after release-evidence commit 596d397, never the entire accumulated feature branch. No push, new tag or new image publication is required for this installation.

### L3 — Preflight and verifier plan

Job-15 exited 0: /mnt/user/Plex_media and /mnt/cache4tb/appdata exist (99:100), appdata/temporary deployment targets do not exist, 1.4 TiB is available on cache4tb, and port 17540/container name are available. No conflicts were removed. Job-16 resolved official cosign v3.1.4 Linux amd64 asset SHA-256 `424e2c97d0b45fd7a6b608a87f090f35633f4c23ece5f092d48bba11aaf4bbfa` through GitHub's release asset metadata. Job-17 downloads that binary only into the authorized temporary directory and the unchanged installer/checksum from the exact release commit; it checks both against explicit known SHA-256 values before use.

Native assess was called with an item-5 deployment escalation. Its native risk was unassessable because the new tracking file is untracked; the returned plan still explicitly requires self-verification and one independent verifier (agentEscalation item 5, high). No native source review is represented as complete; no application source or workflow change is planned.

### L4 — Installer download incident

Job-17 exited 22: cosign's explicit SHA-256 check passed and its executable reports v3.1.4, linux/amd64; the subsequent raw.githubusercontent.com installer download returned HTTP 404. Only the authorized temporary directory and verified cosign binary were created. No installer was executed, no appdata was created, and no container was created. T1 remains in progress; T2–T4 remain pending.

Correction: transport only install-unraid.sh and its published checksum from local commit b6a90fd803b5db5af701c94c4eb61ef5ac0f6a86 via git archive over SSH into the same authorized temporary directory, refuse to overwrite existing installer paths, then verify the explicit known checksum, published checksum, and bash syntax. This changes transport, not installer contents or verification requirements.

### L5 — Installer prerequisites verified

Job-18 exited 0. The exact release installer passed the explicit SHA-256 check and the published checksum check; bash -n passed. T1 is complete. T2 uses ALC_INSTALL_REQUIRE_SIGNATURE=1, version tag 0.32.0, approved named paths, non-root app IDs 99:100, Europe/Madrid, LAN bind 192.168.1.165:17540, no optional /mnt mount, and a cryptographically random password generated only on Tower. Temporary verifier home/cache is confined to the authorized temporary directory. The unchanged installer owns the bounded health wait; the overall invocation is bounded to 600 seconds. Do not retry automatically on a failure or timeout.

### L6 — Registry access blocks installation

Job-19 exited 1 at docker pull: `Head "https://ghcr.io/v2/almali-dev/almali-library-curator/manifests/0.32.0": unauthorized`. Installer checksum, configuration/path validation and conflict checks passed before that. The installer did not reach signature verification, prepare_app_data, write_env_file or create_container, so no deployment appdata, saved password or container was created. The generated password existed only in the failed remote process environment and was unset on exit; it was not printed.

The invocation uses an isolated HOME under the authorized temporary directory, so this was an anonymous registry request, not proof that any existing Tower login is invalid. It does not by itself prove the package's visibility setting. Do not inspect or borrow another application's credentials. Do not retry the pull unchanged or relax signature verification. Owner decision is required: explicitly make the GHCR package publicly readable, or keep it private and provide a read-only registry authentication path without sharing a token in chat. T2 was blocked on that decision; independent verification and UI handoff cannot run before deployment exists.

### L7 — Owner keeps the registry private

Owner selected `private_registry_auth`, quoted in S4. No visibility change is authorized. Authentication requires a human-held token entered privately on Tower, not through chat or an agent-visible tool invocation. The parent provides a command using read -s and docker login --password-stdin, with an isolated DOCKER_CONFIG under the already approved temporary path. T2 remains blocked until the owner completes this login; do not repeatedly retry the unauthenticated pull. After login succeeds, resume the same verified installer with DOCKER_CONFIG pointing to this isolated directory, keeping signature verification mandatory and generating a fresh owner password on Tower.

### L8 — Explicit owner stop and saved checkpoint

Owner, verbatim:

> guarda i tanco

Stop here. T1 is complete; T2 is blocked on the owner completing secure private-registry authentication; T3 and T4 remain pending. No deployment is represented as complete. No appdata directory, persisted app admin password, signing key or Curator container was created by the failed installer. The authorized temporary directory contains the verified cosign binary and exact-release installer/checksum; do not delete or overwrite it on resume. The owner has received the private-terminal docker login command, but has not reported success; do not assume credentials exist or print their contents.

Resume by reconciling this document with its Engram mirror and checking the actual Tower state. The temporary path is under /tmp and may disappear on Unraid reboot; if absent, recreate only the approved tooling after verification. Once the owner confirms Login Succeeded, use DOCKER_CONFIG=/tmp/almali-curator-v032-install/.docker for the same installer and cosign, keep ALC_INSTALL_REQUIRE_SIGNATURE=1, and generate a fresh app-admin password server-side. Do not repeat the anonymous pull, change package visibility, update other services or mark T17 field testing complete. The next authorized work is still installation, independent verification, and UI/login handoff. No push, tag or new release is needed for this checkpoint.
