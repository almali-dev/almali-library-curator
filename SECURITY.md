# Security policy

## Supported versions

The project is pre-release (pre-1.0), so **only the latest tag on the default
branch receives security fixes**. An older tag is not patched; upgrade to the
latest one.

From **v1.0.0** on, the latest minor version receives security fixes and older
minors do not.

## Reporting a vulnerability

Please report security issues **privately**, through GitHub's private
vulnerability reporting:

1. Open <https://github.com/almali-dev/almali-library-curator/security> (the
   repository's **Security** tab).
2. Press **Report a vulnerability**.
3. Describe the issue in the private advisory form.

Please do **not** open a public issue, a pull request or a discussion for a
security problem, and do not post details anywhere else until a fix is
available.

## What to include

- The version or tag you run, and how you installed it (from source, Docker or
  Unraid).
- What an attacker can do, and what they need to start (network access, a
  session, a crafted request, a hostile Plex server, and so on).
- The steps to reproduce it, as short as possible.
- Any log line, request or response that shows the problem, with **tokens,
  passwords, API keys and cookies removed**. The Plex token is a credential:
  never paste a whole one.

## Expected response

- I acknowledge a report **within 7 days**.
- This is a single-maintainer project ([@mmarkimark](https://github.com/mmarkimark)),
  so fixes are **best effort**: there is no paid support and no guaranteed fix
  date. I will tell you what I plan to do and when, and I will credit you in the
  advisory and the changelog unless you ask me not to.

## What is in scope

The app deletes media, so safety and verifiable outcomes outrank speed and
features. The properties below are the security contract of the project; a
report that shows one of them failing is in scope.

- **The owner login comes from the environment only** (`ALC_AUTH_USER` plus
  one password source: the plain `ALC_AUTH_PASSWORD`, the owner-only file named
  by `ALC_AUTH_PASSWORD_FILE`, or the Werkzeug hash in
  `ALC_AUTH_PASSWORD_HASH`). There is no default account. Without a user name
  and a usable source the app fails closed with HTTP 503 and serves nothing
  open; only `/healthz`, `/static/*` and `/login` are reachable without a
  session. An unreadable file, an empty file or a malformed hash fails closed
  too, and a conflict between sources is reported in the log. The Unraid
  installer stores the password in its own 0600 file and only names it in the
  environment, so the clear text is never part of the container environment.
- **Every mutating request is CSRF-protected.** A request without a valid token
  is refused and changes nothing. Request bodies are capped at 1 MiB: every
  form is a small URL-encoded post and no route reads an upload, so an
  oversized body is refused with HTTP 413 while it is parsed, before the CSRF
  check or the login check reads it, and nothing is spooled to a temporary
  directory.
- **Failed sign-ins are limited per client and per window.** Five failures
  from one client within 15 minutes lock that client out for 15 minutes, an
  IPv6 client counting as one client per `/64`, so rotating the last bits of an
  address buys no extra guesses. On top of that, once 100 failures are recorded
  in one 15-minute window from any client, every sign-in attempt answers HTTP
  429 until the window drains; the owner is never held out for longer than the
  window, and no request thread sleeps while an attempt is slowed.
- **The session cookie** is named `alc_session` and is `HttpOnly` and
  `SameSite=Lax`; `Secure` is added with `ALC_SECURE_COOKIES=true` for HTTPS
  deployments. Upgrading from the default cookie name requires signing in again.
- **Browser responses carry baseline security headers:**
  `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'`,
  `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and
  `Referrer-Policy: same-origin`. Non-static responses, including redirects,
  errors and API responses, also carry `Cache-Control: no-store`; static assets
  keep their normal caching. Templates use same-origin external scripts and
  stylesheets, without inline scripts, styles or event handlers.
- **Every request to Plex and plex.tv carries one client identifier per
  installation.** It is 128 random bits, created on first use and kept
  owner-only (mode 0600, written atomically) in
  `$ALC_CONFIG_DIR/plex_client_identifier`; the Plex sign-in and every Plex
  client of one installation send the same value, and no two installations
  share one. A missing, unreadable or corrupt file is replaced with a fresh
  identifier and a warning, and never stops the app. An upgrade keeps the
  stored Plex token valid.
- **Secrets entered in the app are encrypted at rest and never logged.** The
  Plex token and the Sonarr, Radarr, Tautulli and TMDB keys are encrypted with
  Fernet in `secrets.json` under `$ALC_CONFIG_DIR`, with the key in
  `secrets.key` (owner-only files). No screen or API returns them in full: the
  screens show only the last four characters of a stored credential. Any
  structured log field whose name looks like a password, token, secret, API key,
  authorization or cookie is written as `***`, and login events never carry the
  password.
- **Credentials given through environment variables are not encrypted.** An
  `ALC_` credential variable (for example `ALC_PLEX_TOKEN`) takes precedence
  over the stored value and is read from the environment on each run; it is
  never written to disk by the app, but it is as visible as the container's
  environment. Prefer entering credentials in the app.
- **`/healthz` is public and says what is not configured.** Its `missing` list
  names `plex` while Plex is not connected and `auth` while the owner login is
  not set, so anyone who can reach the port can tell an unfinished install.
- **Outbound calls go only to the services you configure**, plus one exception
  for the Plex sign-in flow: `plex.tv` and `clients.plex.tv`. Nothing else is
  ever contacted, and redirects are refused so a credential is never re-sent to
  another host. A blank connection credential keeps the stored Plex token or
  service key only when the submitted address has the same scheme, host and
  effective port as the stored address. A changed origin (or no stored address)
  requires entering the credential again before any connection test or save,
  including when picking a server after Plex sign-in. Host case, a trailing
  slash, a path change or an explicit default port do not change the origin.
  TMDB has no editable address and can still reuse its stored key.
- **The Unraid installer authenticates what it installs.** A fresh install
  pulls the release version pinned in `install-unraid.sh`, verifies the image's
  keyless signature before creating anything, and, when `cosign` is not on the
  PATH, fetches the pinned `cosign` v2.6.5 for the host architecture and checks
  it against a **sha256** digest embedded in the script. A failed download, a
  mismatched digest or a failed signature stops the run with nothing created.
  The publish workflow signs the installer with `cosign sign-blob` and publishes
  its bundle next to the image for that exact version. When a request carries
  `X-Forwarded-For` while `ALC_TRUSTED_PROXIES` is `0`, the app logs one warning:
  behind an undeclared proxy every client shares one address, so the login
  lockout applies to all of them. The installed container drops every Linux
  capability except the `CHOWN`, `DAC_OVERRIDE`, `SETUID` and `SETGID` the
  entrypoint needs to
  give `/config` to `PUID`:`PGID` and drop to it, sets
  `no-new-privileges:true`, runs with a read-only root filesystem (only
  `/config` and a `/tmp` tmpfs are writable), never uses a moving image tag in
  the installer, and the base image is pinned by digest and kept current by
  Dependabot. The one documented
  opt-out, `ALC_INSTALL_SKIP_SIGNATURE=1`, warns loudly and exists for
  air-gapped hosts only.
- **The app never writes to your media directly.** A deletion goes through
  Sonarr, Radarr or the Plex API, and only through the removal lifecycle: marked
  by the owner, past the grace period and confirmed by a final re-check. "Deleted"
  is reported only after the files are confirmed gone from disk.

Out of scope: a fully compromised `/config` directory or host (whoever holds the
encryption key can read the stored token), vulnerabilities in Plex, Sonarr,
Radarr, Tautulli or TMDB themselves, and reports that only say a scanner found a
missing hardening header without a way to exploit it.

## Not affiliated with Plex

Almali Library Curator for Plex is an independent project. It is **not
affiliated with or endorsed by Plex Inc.** "Plex" is a trademark of Plex, Inc.
and is used only to say what this app works with.
