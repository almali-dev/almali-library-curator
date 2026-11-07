## Summary

<!-- What changes, and why. One behaviour per pull request; keep it near 400 changed lines. -->

## Linked issue

<!-- Fixes #123, or the issue this implements. Open one first for a new feature or a behaviour change. -->

## Checklist

- [ ] A failing test was observed before the fix, through the public interface (the Flask test client, a fake Plex / Sonarr / Radarr / plex.tv server), and `pytest` is green now.
- [ ] New or changed user-facing strings exist in `en`, `ca` and `es` (`almali_library_curator/i18n/*.json`), with no literal copy in templates, scripts or Python.
- [ ] Docs are updated where the behaviour changed (`README.md`, `docs/Almali-Library-Curator-for-Plex.md`, ).
- [ ] `CHANGELOG.md` has an entry under `## [Unreleased]`.
- [ ] The pull request title is a Conventional Commit (`feat:`, `fix:`, `docs:`, `test:`, `build:`, `chore:`, `refactor:`).
