# Almali Library Curator for Plex — rebrand (feature)

Rebrand the project around the Almali family, published under the `almali-dev` organization, so no active surface keeps the former product identity and the app reads as one of the Almali apps.

## Specs

- **S1** — "Ahir vaig crear una organització que es diu almali-dev on penjaran el projectes almali."
- **S2** — "També vaig treure el nom de plex ja que sembla que plex no ho permet i ara l'aplicació es diu Almali Libray Curator for Plex."
- **S3** — "Els repositori també ha canviat i es diu almali-repositori-curator."
- **S4** — "Hauràs de revisar tot el repo per tal de que no quedi cap rastre anterior."
- **S5** — "Que es reconegui que es un projecte dins de les aplicacions almali."
- **S6** — "I tot el que creguis que s'hauria de canviar, per exemple títol de l'aplicació a la web."

## Tasks

Routes: `inline` = parent session. One coherent mechanical unit with shared file surfaces, so no writer trigger holds; slices are separated by commit budget and risk.

| ID | Task | Specs | Route | Commit | Risk |
| --- | --- | --- | --- | --- | --- |
| T1 | Rebrand feature document, Engram mirror and visible todo list | S4, S5 | inline | tracking only | none |
| T2 | Rename the Python package `library_curator` → `almali_library_curator` (package tree, every import, `pyproject.toml`, CI, WSGI entry point) and rename the v1 feature document file to `odd/tasks/almali-library-curator.md` | S2, S3, S4 | inline | W1 | item 3 (import paths, WSGI entry point and the distribution name are contracts) |
| T3 | Rename the runtime identifiers: environment prefix `LCP_` → `ALC_`, database file name, container app-data path, GHCR image name | S3, S4 | inline | W2 | item 3 (operator-facing config contract: environment prefix and database file name) |
| T4 | Visible branding inside the app: `app.name` in en/ca/es, module docstrings, web `<title>` and header, vendored Almali symbol and favicon | S2, S5, S6 | inline | W3 | item 3 (public screen title and copy) |
| T5 | Documentation and specification sweep: README (former-name line, commands, image), requirements document, version roadmap, skill registry, mockup file name, feature-document specs | S4, S5 | inline | W4 | item 3 (documented commands and specs) |
| T6 | Repository-level sweep and proof: branch rename, GitHub metadata, zero former-name traces in active surfaces, full suite green, historical exceptions recorded | S3, S4, S5, S6 | inline | W5 | item 5 (remote branch and repository metadata) |

## Log

- **L1** (2026-11-03, user, verbatim): "Revisa on ens vam quedar. Ahir vaig crear una organització que es diu almali-dev on penjaran el projectes almali. També vaig treure el nom de plex ja que sembla que plex no ho permet i ara l'aplicació es diu Almali Libray Curator for Plex. Els repositori també ha canviat i es diu almali-repositori-curator. Hauràs de revisar tot el repo per tal de que no quedi cap rastre anterior. Que es reconegui que es un projecte dins de les aplicacions almali. I tot el que creguis que s'hauria de canviar, per exemple títol de l'aplicació a la web."
- **L2** (2026-11-03, resume point): the v1 feature continues at T5 (protection engine) with T1–T4 closed and tagged `v0.1.0`–`v0.4.0`; the authoritative v1 document is `odd/tasks/library-curator-for-plex.md` until T2 renames it. No v1 task is closed by this rebrand.
- **L3** (2026-11-03, repository evidence): `gh repo list almali-dev` returns `almali-dev/almali-library-curator` (private, default branch `main`), `almali-dev/almali-smart-cache`, `almali-dev/almali-llarTV` and the `almali-dev/.github` profile. The local checkout is `/home/mac/work/almali-library-curator` and `origin` already points at `https://github.com/almali-dev/almali-library-curator.git`. The user's spoken slug "almali-repositori-curator" therefore resolves to the real slug `almali-library-curator`; the repository rename is already done and this feature does not rename it again.
- **L4** (2026-11-03, visible-name boundary): the exact visible product name is **Almali Library Curator for Plex**. Three independent sources agree: the organization profile (`almali-dev/.github`, profile/README.md, authored by the owner) lists "Almali Library Curator for Plex"; the owner's own uncommitted README edit in this repository uses `# Almali Library Curator for Plex`; and the sibling project applied the identical rebrand yesterday as "Almali Smart Cache for Plex · Unraid" (its own `odd/tasks/almali-rebrand.md`). Prose may use the shorter "Almali Library Curator". The former name is removed as a standalone prefix — Plex's guideline is "\<Name> for Plex", so "Plex" stays only inside the allowed suffix and only where it identifies the integrated media server, its API, its tokens and the `X-Plex-*` protocol.
- **L5** (2026-11-03, identifier boundary): the project is unreleased — `v0.4.0`, private repository, no published container image and no installation exists. The Python package, the environment prefix, the database file name and the app-data path are therefore renamed outright instead of being kept as legacy compatibility contracts; unlike the sibling project, which kept `PSC_*` because it has live installations, nothing here can regress. The change is breaking only for an unpublished, uninstalled pre-1.0 tree.
- **L6** (2026-11-03, historical boundary): Git history, the annotated tags `v0.1.0`–`v0.4.0` and the Log entries of every feature document remain truthful records of the former name and are not rewritten. The final sweep reports them as immutable historical evidence; active surfaces carry no former display name. The owner's uncommitted README rebrand is part of this feature and lands with it.
- **L7** (2026-11-03, version boundary): the rebrand is an identity change, not a capability milestone. It consumes no MINOR number and creates no tag; the version ladder in `docs/ROADMAP.md` stays aligned to the v1 tasks T1–T15, and the current version stays `0.4.0`.
- **L8** (2026-11-03, out-of-repository surface): the `almali-dev/.github` organization profile already lists this project. Its project row is plain text rather than a link, unlike the other two rows; making it a link needs a commit in that separate repository and is reported to the owner instead of being done here.
