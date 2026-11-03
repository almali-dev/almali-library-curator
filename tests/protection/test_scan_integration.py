"""One scan through the public path with the protector injected (S4)."""

from __future__ import annotations

from fake_plex_server import FakePlexDataset, movie

from almali_library_curator.detection.engine import DetectionEngine
from almali_library_curator.detection.models import Category
from almali_library_curator.protection import ProtectionEngine, ProtectionKind

from helpers import NOW, default_settings, sync_dataset, ts


def filmoteca_dataset() -> FakePlexDataset:
    """Two movie libraries: an open one and the protected Filmoteca."""
    data = FakePlexDataset()
    data.sections = [
        {"key": "1", "title": "Movies", "type": "movie"},
        {"key": "3", "title": "Filmoteca", "type": "movie"},
        {"key": "2", "title": "Shows", "type": "show"},
    ]
    data.items["1"] = [movie("100", "Tenet", addedAt=ts(2024, 4, 1))]
    data.items["3"] = [movie("300", "Blade Runner", addedAt=ts(2024, 4, 1))]
    return data


def test_a_protected_item_never_comes_out_of_a_scan_as_a_candidate(conn):
    settings = default_settings(protected_libraries=("Filmoteca",))
    sync_dataset(conn, filmoteca_dataset())
    protector = ProtectionEngine(conn, settings=settings)
    engine = DetectionEngine(
        conn, clock=lambda: NOW, settings=settings, protector=protector
    )

    assert [candidate.title for candidate in engine.scan()] == ["Tenet"]

    outcome = engine.scan_with_protection()
    assert [candidate.title for candidate in outcome.candidates] == ["Tenet"]
    (protected_entry,) = outcome.protected
    assert protected_entry.candidate.title == "Blade Runner"
    assert protected_entry.protection.kind is ProtectionKind.LIBRARY
    assert protected_entry.reason == "The protected library Filmoteca covers it."
    # The category it would otherwise match is still on the candidate.
    assert protected_entry.candidate.categories == (Category.NEVER_WATCHED,)


def test_scan_without_a_protector_behaves_exactly_as_before(conn):
    sync_dataset(conn, filmoteca_dataset())
    engine = DetectionEngine(conn, clock=lambda: NOW, settings=default_settings())

    assert sorted(candidate.title for candidate in engine.scan()) == [
        "Blade Runner",
        "Tenet",
    ]
