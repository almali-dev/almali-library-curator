# Library Curator for Plex

Shows what nobody watches in a Plex library, says why in one sentence per item,
and removes only what the owner marks, after a grace period and a final
re-check.

> Not affiliated with or endorsed by Plex Inc. "Plex" is a trademark of Plex,
> Inc.

## Status

Version 1 is under construction. The requirements live in
[`docs/Library-Curator-for-Plex.md`](docs/Library-Curator-for-Plex.md) and the
implementation plan in [`odd/tasks/library-curator-for-plex.md`](odd/tasks/library-curator-for-plex.md).

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
LCP_CONFIG_DIR=./config pytest
LCP_CONFIG_DIR=./config flask --app library_curator.wsgi run
```

Settings live in `$LCP_CONFIG_DIR` (default `/config` in the container) and are
overridden per run with `LCP_`-prefixed environment variables. A fresh install
starts in dry-run mode: nothing is ever deleted until the owner turns real
deletion on.

## Licence

MIT.
