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

## Part of Almali

This project is part of [Almali](https://github.com/almali-dev), a small collection of independent, open-source
tools for home media and self-hosting, built and maintained by [@mmarkimark](https://github.com/mmarkimark).
Each project works on its own: install only what you need.

## Licence

MIT.
