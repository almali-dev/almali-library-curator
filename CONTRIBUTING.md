# Contributing

Thanks for taking a look. This is a small, single-maintainer project, so the
short version is: open an issue first for anything big, keep a pull request
small and focused, and let the tests show the change works.

Almali Library Curator for Plex deletes media. That makes every change a safety
change: a rule that removes the wrong item, or reports a deletion that did not
happen, is worse than a missing feature.

## Development setup

Python **3.11 or newer** is required (`requires-python = ">=3.11"` in
`pyproject.toml`). CI runs the suite on **Python 3.11, 3.12 and 3.13**.

```bash
git clone https://github.com/almali-dev/almali-library-curator.git
cd almali-library-curator
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ALC_CONFIG_DIR=./config pytest
```

`ALC_CONFIG_DIR=./config` keeps the settings file, the SQLite database and the
secrets out of your home directory and inside the ignored `./config` folder.
Run the suite again before you open a pull request; it must be green.

### Browser checks

The full suite includes screen checks that measure the layout at 320 px wide in
headless Chromium. Install the optional browser extra and its browser to run them:

```bash
pip install -e ".[dev,browser]"
python -m playwright install --with-deps chromium
ALC_REQUIRE_OPTIONAL_TESTS=1 python -m pytest tests/packaging/test_ci_workflow.py tests/packaging/test_publish_workflow.py tests/web/test_browser_layout.py
```

Without `ALC_REQUIRE_OPTIONAL_TESTS=1`, those modules skip locally if PyYAML,
Playwright or Chromium is unavailable. CI installs both extras and Chromium,
and sets the flag: any collection or runtime skip in those three modules fails
CI instead of silently dropping coverage. Other opt-in checks (such as Docker)
keep their own guards. Publishing reuses this same CI workflow.

### Lint and type gates

After installing the pinned dev extra, run the same gates as CI:

```bash
python -m ruff check --no-cache .
python -m pyright --pythonpath "$(command -v python)"
```

The explicit Python path makes Pyright use the active environment's runtime
dependencies, including when invoked via `python -m`. These are initial,
intentionally limited baselines, not a claim that the project is fully typed:

| Gate | Current baseline | How to tighten |
| --- | --- | --- |
| Ruff | All discovered Python files; `E4`, `E9`, `F63`, `F7`, `F82` (import placement, syntax and high-confidence correctness errors). No unused-import/variable or general style gate yet. | Add `F` and `E7` in small batches after fixing existing findings; then consider formatting and broader style rules. |
| Pyright | Application package only, Python 3.11 semantics, `basic` mode. Six diagnostic families are disabled in `pyrightconfig.json`: `reportArgumentType`, `reportAttributeAccessIssue`, `reportCallIssue`, `reportOptionalMemberAccess`, `reportOptionalOperand`, `reportOptionalSubscript`. Other basic diagnostics remain active, including missing imports, undefined variables, assignment and return types. | Fix the underlying types and re-enable each diagnostic family, starting with `Settings.database_path` nullability and optional clients; then expand coverage to scripts/tests and consider `standard`/`strict`. |

The Pyright exceptions cover existing optional-path/client narrowing, protocols,
heterogeneous payloads and dynamic attributes. They also mask **new** errors of
those kinds until re-enabled; do not add more exceptions to hide regressions.
Ruff, Pyright, pytest, PyYAML and pytest-xdist are version-pinned in `dev`;
Playwright is pinned separately in `browser`. Upgrade pins deliberately and run
the suite and both gates before changing the baseline.

`pydantic-settings` is currently unused by the package but remains in the runtime
manifest and hashed Docker lock. Remove it only with a regenerated lock and the
matching packaging contracts; do not hand-edit dependency hashes.

## Running locally

The app needs an owner login from the environment and has no built-in account.
For local development, export an example login of your own (this is an example to
type, **never a default in the code**):

```bash
export ALC_AUTH_USER=admin
export ALC_AUTH_PASSWORD=admin
ALC_CONFIG_DIR=./config flask --app almali_library_curator.wsgi run
```

Then open <http://127.0.0.1:5000>. A fresh install starts in dry-run mode, so
nothing is deleted while you develop.

## Test-first

Write the failing test first, through the public interface — the Flask test
client, the installer scripts or a fake Plex, Sonarr, Radarr, Tautulli or plex.tv server —
not a mock of the app's own code. Then make the smallest change that passes it.

1. **Red.** Add the smallest test that pins the rule you are about to implement,
   and run it. Paste the failure into the pull request description or the commit
   body if it is not obvious.
2. **Green.** Implement until that test passes.
3. **Triangulate.** Add the negative and boundary cases that actually protect the
   contract (at-the-threshold versus one day past it, a service that is
   unreachable, a request refused, a damaged scope).
4. **Refactor.** Clean up while the focused test stays green.

If a change genuinely cannot have a meaningful red — a passive documentation
change, for example — say so in the pull request and run the proportionate check
instead (the full suite, a build, a manual step) and report what you observed.
Never claim a failing test you did not see fail.

## Translations

The interface ships in **English, Catalan and Spanish** from day one, and every
user-facing string goes through a translation key: no literal copy in a
template, a script or the Python that builds a message. Add the key to all three
catalogs in `almali_library_curator/i18n/`:

- `en.json`
- `ca.json`
- `es.json`

The three files must hold the same keys with the same format fields; a test
enforces the parity, and another one fails on literal copy in either template
tree. If you add a sentence, add it three times.

## Commits

Use [Conventional Commits](https://www.conventionalcommits.org/): a type, an
optional scope, then a short imperative summary in the present tense.

```text
feat: delete through Plex when the responsible arr is absent or off
fix: give the active category filter a check icon, not only colour
test: refuse every mutating route without a CSRF token
docs: record the v0.15.0 delivery and the T12 review
build: vendor tabler 1.0.0-beta20 and local fonts
chore: release v0.15.0
```

Types used here: `feat`, `fix`, `test`, `docs`, `build`, `chore`, `refactor`.
Keep one behaviour per commit, and keep the tests and the docs for that
behaviour in the same commit, so a review can read them together.

## Pull requests

- **Open an issue first** for a new feature, a behaviour change or anything that
  touches the removal lifecycle.
- **Keep it small**: aim for about **400 changed lines**, the size a reviewer can
  read in one pass. A change that grows past that is usually two changes; split
  it into chained pull requests rather than one large one.
- **Say what and why**, link the issue, and name the behaviour the tests pin.
- **Check before you ask for review**:
  - [ ] The failing test was observed before the fix, and `pytest` is green now.
  - [ ] New or changed user-facing strings exist in `en`, `ca` and `es`.
  - [ ] Docs are updated (`README.md`, the requirements or the plan when the
    behaviour they describe changed), and `CHANGELOG.md` has an entry under
    `## [Unreleased]`.
  - [ ] The commit titles are Conventional Commits.
- Do not bump the version, tag a release or publish anything: the maintainer
  owns versions and tags.

The pull request template repeats that checklist, so you can tick it there.

## Where the specification lives

- **Requirements** (the product contract, quoted verbatim where a rule, message
  or example must not drift):
  [`docs/Almali-Library-Curator-for-Plex.md`](docs/Almali-Library-Curator-for-Plex.md)
- **Version ladder**: [`docs/ROADMAP.md`](docs/ROADMAP.md)

Read the relevant spec before you change a rule. If the code and the spec
disagree, the spec wins until the maintainer decides otherwise; say so in the
issue instead of silently changing the behaviour.

## Security

Do **not** report a vulnerability in a public issue or pull request. Follow
[SECURITY.md](SECURITY.md) and use GitHub's private vulnerability reporting on
the repository's Security tab.

When you change something security-relevant (authentication, CSRF, the session
cookie, secret storage, log redaction or the outbound rule), say so in the pull
request: those paths are the ones that get a slower, closer read.

## Licence

By contributing you agree that your contribution is licensed under the **MIT
licence** of this project (see [LICENSE](LICENSE)). Only contribute code you
have the right to submit under that licence.

## Not affiliated with Plex

This project is not affiliated with or endorsed by Plex Inc. "Plex" is a
trademark of Plex, Inc. and is used only to say what this app works with.
