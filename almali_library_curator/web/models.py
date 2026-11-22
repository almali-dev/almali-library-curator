"""Presentation-neutral, credential-free screen contracts."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ItemView:
    media_item_id: int
    unit: str
    seasons: tuple[int, ...]
    title: str
    type_badge: str
    categories: tuple[str, ...]
    reasons: tuple[str, ...]
    reason: str
    size_bytes: int
    disk: str | None
    rating: float | None
    rating_source: str | None
    protection_kind: str | None = None
    protection_reason: str | None = None
    # #47: the reasons as a key plus raw parameters, rendered in the request's
    # language; the text fields above stay for rows written before them.
    reason_parts: tuple[dict, ...] = ()
    protection_reason_parts: tuple[dict, ...] = ()
    # #48: the last activity measured with the row, carried by the cache.
    last_played_at: str | None = None


# The three tabs of the Library screen. One request shows one of them, and
# every row action posts the tab it was pressed on back (#63).
LIBRARY_TABS = ('candidates', 'leaving_soon', 'protected')

# The Library and Protected tabs show this many rows at a time, and never
# more, however large a page the request asks for (#48).
LIBRARY_PAGE_SIZE = 50
LIBRARY_MAX_PAGE_SIZE = 100


@dataclass(frozen=True)
class ItemsView:
    items: tuple[ItemView, ...]
    category_counts: dict[str, int]
    would: bool
    last_scan: dict | None
    # #48: one page of the rows, with the total the page is part of.
    page: int = 1
    page_size: int = LIBRARY_PAGE_SIZE
    pages: int = 1
    total: int = 0
    has_next: bool = False
    has_previous: bool = False


@dataclass(frozen=True)
class LeavingItem:
    id: int
    media_item_id: int
    title: str
    unit: str | None
    seasons: tuple[int, ...]
    size_bytes: int
    disk: str | None
    state: str
    days_left: int | None
    grace_days: int | None
    progress: float | None
    recheck_status: str | None
    cancel_reason: str | None
    error: str | None
    unreadable: bool = False
    # #64: when the files go, and how many files that is. The date is already
    # rendered in the reader's language; the count is the scope's own files.
    due_label: str | None = None
    file_count: int = 0


@dataclass(frozen=True)
class GoLiveView:
    """What turning dry run off puts at risk (#64): what is now due.

    Read once, before the setting is stored, so the confirmation lists the
    items, the space and the first date the deletions would run.
    """

    items: int
    size_bytes: int
    first_due: str | None
    kill_switch: bool


@dataclass(frozen=True)
class LeavingView:
    items: tuple[LeavingItem, ...]
    cancelled: tuple[LeavingItem, ...]


@dataclass(frozen=True)
class Totals:
    size_bytes: int
    count: int


@dataclass(frozen=True)
class SummaryView:
    candidates: Totals
    leaving_soon: Totals
    protected: Totals
    grace_days: int
    protection_sources: tuple[str, ...]
    freed_bytes: int
    deleted_count: int
    first_month: str | None
    last_scan: dict | None


# The History page shows the newest events in pages of this many rows.
HISTORY_PAGE_SIZE = 50


@dataclass(frozen=True)
class EventView:
    id: int
    outcome: str
    title: str
    unit: str
    category: str | None
    reason: str | None
    size_bytes: int | None
    freed_bytes: int | None
    disk: str | None
    marked_by: str | None
    occurred_at: str
    error: str | None


@dataclass(frozen=True)
class HistoryView:
    events: tuple[EventView, ...]
    freed_per_month: dict[str, int]
    outcome: str | None
    month: str | None
    # The History page shows the newest events in pages (issue #9).
    page: int = 1
    page_size: int = HISTORY_PAGE_SIZE
    has_next: bool = False
    has_previous: bool = False


@dataclass(frozen=True)
class SourcesView:
    plex_users: int
    tautulli_since: str | None
    disk_count: int


@dataclass(frozen=True)
class ServiceView:
    name: str
    status: str


@dataclass(frozen=True)
class ConnectionsView:
    services: tuple[ServiceView, ...]
    sources: SourcesView


@dataclass(frozen=True)
class ServiceFormView:
    """One optional service's key form (S20).

    Only a masked key ever reaches it, and only when the key really comes
    from the encrypted store. The address field is None for TMDB, which
    asks for no address (S20 rule 1).
    """
    name: str
    brand: str
    url_field: str | None
    key_field: str
    base_url: str
    key_mask: str | None
    env_managed: bool
    key_unreadable: bool
    errors: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PlexServerOptionView:
    """One of the owner's own servers offered as the address (S13 rule 3)."""
    name: str
    address: str


@dataclass(frozen=True)
class PlexSignInView:
    """The state of a Plex sign-in. The code is not a credential and may
    be shown; a token never reaches this view (S13 rules 1 and 5)."""
    state: str  # pending | authorized | succeeded | failed | cancelled
    code: str | None = None
    link: str | None = None
    message: str | None = None  # a translation key, not text
    servers: tuple[PlexServerOptionView, ...] = ()


@dataclass(frozen=True)
class PlexFormView:
    """The Plex connection form. Only a masked token ever reaches it (S12)."""
    base_url: str
    token_mask: str | None
    env_managed: bool
    token_unreadable: bool
    errors: dict[str, str] = field(default_factory=dict)
    signin: PlexSignInView | None = None


@dataclass(frozen=True)
class SettingsView:
    values: dict
    kill_switch: bool
    libraries: tuple[dict, ...]
    users: tuple[dict, ...]
    errors: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SetupOptionView:
    """One optional connection of the first-run checklist (S3 rule 4).

    ``brand`` is the service's own name (a proper name, the same in every
    language) or None for the host paths, which ``brand_key`` names instead.
    """
    brand: str | None
    brand_key: str | None
    state: str  # configured | not_configured
    value_key: str


@dataclass(frozen=True)
class SetupStepView:
    """One numbered step of the first-run checklist (S3).

    ``state`` is what the step says about itself: done, pending, running,
    failed or optional (an optional step still open). ``action`` is a
    symbolic name the template turns into the screen it leads to; the view
    builds no URL, so it stays usable without a request. ``item_state`` is an
    existing ``state.*`` badge for the steps that show a mode (dry run or
    real deletion) instead of a progress state.
    """
    number: int
    title_key: str
    state: str
    required: bool
    value_key: str | None = None
    value_params: dict = field(default_factory=dict)
    note_key: str | None = None
    note_params: dict = field(default_factory=dict)
    action: str | None = None
    action_key: str | None = None
    options: tuple[SetupOptionView, ...] = ()
    item_state: str | None = None
    # The first step still open: where the checklist says to start.
    active: bool = False


@dataclass(frozen=True)
class SetupChecklistView:
    """The whole first-run checklist and whether it belongs on screen.

    S3: it is shown while a required step is open (Plex not configured, or
    no finished analysis) and disappears once both are done; the optional
    steps never keep it on screen.
    """
    steps: tuple[SetupStepView, ...]
    done: int
    total: int
    remaining_key: str | None
    visible: bool


@dataclass(frozen=True)
class AboutLink:
    """One project link of the About page: a translation key and its address."""
    label_key: str
    url: str


@dataclass(frozen=True)
class AboutView:
    """The About page facts (S19 rule 1): the running version and the links.

    No database row, no service and no secret reaches this view.
    """
    version: str
    links: tuple[AboutLink, ...]


@dataclass(frozen=True)
class ScanStepView:
    """One step of the last recorded scan run and how it ended."""
    name: str  # sync | enrichment | detection | recheck | deletion
    status: str  # finished | failed | skipped | pending


@dataclass(frozen=True)
class ScanStatusView:
    """The outcome of the last scan run, as the owner reads it (S2).

    ``state`` is what happened overall: never, running, finished, failed or
    skipped. The steps carry the run's own record; a raw exception body or a
    bare class name never reaches the view.
    """
    state: str
    steps: tuple[ScanStepView, ...] = ()
    finished_at: str | None = None


@dataclass(frozen=True)
class LibraryView:
    summary: SummaryView
    candidates: ItemsView
    protected: ItemsView
    leaving_soon: LeavingView
    sources: SourcesView
    tab: str
    # S15 rule 5: unit type -> the configured arr whose deletions are off.
    warnings: dict = field(default_factory=dict)
    # Current settings, not the last scan's mode: they tell owners whether a
    # final re-check can lead to a deletion on every Library tab.
    dry_run: bool = False
    kill_switch: bool = False
