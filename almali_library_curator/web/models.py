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


@dataclass(frozen=True)
class ItemsView:
    items: tuple[ItemView, ...]
    category_counts: dict[str, int]
    would: bool
    last_scan: dict | None


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
class PlexFormView:
    """The Plex connection form. Only a masked token ever reaches it (S12)."""
    base_url: str
    token_mask: str | None
    env_managed: bool
    token_unreadable: bool
    errors: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SettingsView:
    values: dict
    kill_switch: bool
    libraries: tuple[dict, ...]
    users: tuple[dict, ...]
    errors: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LibraryView:
    summary: SummaryView
    candidates: ItemsView
    protected: ItemsView
    leaving_soon: LeavingView
    sources: SourcesView
    tab: str
