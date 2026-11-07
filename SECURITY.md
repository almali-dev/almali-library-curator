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

- **The owner login comes from the environment only** (`ALC_AUTH_USER`,
  `ALC_AUTH_PASSWORD`). There is no default account. Without both variables the
  app fails closed with HTTP 503 and serves nothing open; only `/healthz`,
  `/static/*` and `/login` are reachable without a session.
- **Every mutating request is CSRF-protected.** A request without a valid token
  is refused and changes nothing.
- **The session cookie** is `HttpOnly` and `SameSite=Lax`; `Secure` is added
  with `ALC_SECURE_COOKIES=true` for HTTPS deployments.
- **Secrets are encrypted at rest and never logged.** The Plex token is
  encrypted with Fernet under `$ALC_CONFIG_DIR` (owner-only files), is never
  returned in full by any screen or API, and any structured log field whose name
  looks like a password, token, secret, API key, authorization or cookie is
  written as `***`. Login events never carry the password.
- **Outbound calls go only to the services you configure**, plus one exception
  for the Plex sign-in flow: `plex.tv` and `clients.plex.tv`. Nothing else is
  ever contacted, and redirects are refused so a credential is never re-sent to
  another host.
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
