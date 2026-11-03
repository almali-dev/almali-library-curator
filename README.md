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
