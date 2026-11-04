<p align="center">
  <a href="https://github.com/almali-dev">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/almali-dev/.github/main/brand/almali-horizontal-dark.svg">
      <img alt="Almali" src="https://raw.githubusercontent.com/almali-dev/.github/main/brand/almali-horizontal.svg" height="48">
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

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ALC_CONFIG_DIR=./config pytest
ALC_CONFIG_DIR=./config flask --app almali_library_curator.wsgi run
```

Settings live in `$ALC_CONFIG_DIR` (default `/config` in the container) and are
overridden per run with `ALC_`-prefixed environment variables. A fresh install
starts in dry-run mode: nothing is ever deleted until the owner turns real
deletion on.

### Plex connection

Plex is the only required connection. Enter its address and token in the
first-run wizard (`/setup`) or on **Connections**: the app tests them against
Plex and stores them only when Plex answers as a Plex Media Server. The address
goes to `settings.json`; the token is encrypted with Fernet and written to
`secrets.json`, with its key in `secrets.key`. Both files live in
`$ALC_CONFIG_DIR`, are readable by the owner only (mode 0600) and are never
served. The screens show only the token's last four characters.

`ALC_PLEX_BASE_URL` and `ALC_PLEX_TOKEN` still override the stored values. While
either is set, the screens say that the environment manages Plex and refuse to
change it. If the secrets files are damaged or the key is lost, the app still
starts, reports Plex as not configured and asks for the token again. Keep
`secrets.key` with your backups of `$ALC_CONFIG_DIR`: without it the stored
token cannot be read.

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
