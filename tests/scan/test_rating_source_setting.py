"""#128 S2: the rating source setting chooses the number the app uses.

Every scenario goes through the scan entry point: a fake Plex server and a
fake TMDB server are read by one real ``ScanRunner`` run, and the finished
detection snapshot carries the number and its source.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from almali_library_curator.config import SETTINGS_FILE_NAME, Settings, save_settings
from almali_library_curator.db import init_db
from almali_library_curator.scan import ScanRunner
from fake_plex_server import FakePlexDataset, FakePlexServer, movie
from fake_tmdb_server import FakeTmdbDataset, FakeTmdbServer, tmdb_movie

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


def run_scan(tmp_path, monkeypatch, plex_data, tmdb_data, *, drop_rating_source=False,
             **overrides):
    """One public scan over the fake servers, returning its detection snapshot."""
    monkeypatch.setenv("ALC_PLEX_TOKEN", "test-token")
    monkeypatch.setenv("ALC_TMDB_API_TOKEN", tmdb_data.access_token)
    with FakePlexServer(plex_data) as plex_server, FakeTmdbServer(tmdb_data) as tmdb_server:
        settings = Settings(
            config_dir=tmp_path,
            scheduler_enabled=False,
            plex_base_url=plex_server.base_url,
            plex_rate_limit_per_second=1000,
            tmdb_base_url=tmdb_server.base_url,
            **overrides,
        )
        save_settings(settings)
        if drop_rating_source:
            path = tmp_path / SETTINGS_FILE_NAME
            saved = json.loads(path.read_text(encoding="utf-8"))
            saved.pop("rating_source", None)
            path.write_text(json.dumps(saved), encoding="utf-8")
        init_db(settings.database_path)
        report = ScanRunner(
            settings,
            clock=lambda: NOW,
            enrichment_step=lambda *args, **kwargs: {},
        ).run("manual")
    assert report.status == "finished"
    return report.steps["detection"]["counts"]


def rows(snapshot, kind):
    return {row["title"]: row for row in snapshot[kind]}


def with_tmdb(data, plex_key, title, tmdb_id, vote_average, **extra):
    data.items["1"].append(
        movie(plex_key, title, Guid=[{"id": f"tmdb://{tmdb_id}"}], **extra)
    )


def test_the_default_uses_the_critic_rating_then_tmdb_never_the_audience_one(
        tmp_path, monkeypatch):
    """A settings.json without the key behaves as the released critic choice."""
    plex = FakePlexDataset()
    with_tmdb(plex, "100", "Audience with TMDB", 550, 7.2,
              rating=None, audienceRating=8.9)
    plex.items["1"].append(movie("101", "Audience only", rating=None, audienceRating=8.7))
    tmdb = FakeTmdbDataset()
    tmdb.movies[550] = tmdb_movie(550, "Audience with TMDB", vote_average=7.2)

    snapshot = run_scan(tmp_path, monkeypatch, plex, tmdb, drop_rating_source=True)
    candidates = rows(snapshot, "candidate_rows")

    assert snapshot["protected_rows"] == []
    assert candidates["Audience with TMDB"]["rating"] == 7.2
    assert candidates["Audience with TMDB"]["rating_source"] == "TMDB"
    assert candidates["Audience only"]["rating"] is None
    assert candidates["Audience only"]["rating_source"] is None


def test_audience_prefers_the_audience_rating_then_critic_then_tmdb(
        tmp_path, monkeypatch):
    plex = FakePlexDataset()
    with_tmdb(plex, "100", "Audience wins", 550, 7.0,
              rating=6.0, audienceRating=8.5)
    with_tmdb(plex, "101", "Critic fallback", 551, 9.9,
              rating=7.4, audienceRating=None)
    with_tmdb(plex, "102", "TMDB fallback", 552, 7.1,
              rating=None, audienceRating=None)
    tmdb = FakeTmdbDataset()
    tmdb.movies[550] = tmdb_movie(550, "Audience wins", vote_average=7.0)
    tmdb.movies[551] = tmdb_movie(551, "Critic fallback", vote_average=9.9)
    tmdb.movies[552] = tmdb_movie(552, "TMDB fallback", vote_average=7.1)

    candidates = rows(run_scan(tmp_path, monkeypatch, plex, tmdb,
                               rating_source="audience",
                               rating_protection_enabled=False), "candidate_rows")

    assert (candidates["Audience wins"]["rating"],
            candidates["Audience wins"]["rating_source"]) == (8.5, "Plex audience")
    assert (candidates["Critic fallback"]["rating"],
            candidates["Critic fallback"]["rating_source"]) == (7.4, "Plex")
    assert (candidates["TMDB fallback"]["rating"],
            candidates["TMDB fallback"]["rating_source"]) == (7.1, "TMDB")


def test_highest_picks_the_maximum_and_labels_the_source_that_won(
        tmp_path, monkeypatch):
    plex = FakePlexDataset()
    with_tmdb(plex, "100", "Plex critic wins", 550, 7.0,
              rating=9.0, audienceRating=8.0)
    with_tmdb(plex, "101", "Plex audience wins", 551, 7.0,
              rating=6.0, audienceRating=8.5)
    with_tmdb(plex, "102", "TMDB wins", 552, 9.2,
              rating=6.0, audienceRating=7.0)
    tmdb = FakeTmdbDataset()
    tmdb.movies[550] = tmdb_movie(550, "Plex critic wins", vote_average=7.0)
    tmdb.movies[551] = tmdb_movie(551, "Plex audience wins", vote_average=7.0)
    tmdb.movies[552] = tmdb_movie(552, "TMDB wins", vote_average=9.2)

    candidates = rows(run_scan(tmp_path, monkeypatch, plex, tmdb,
                               rating_source="highest",
                               rating_protection_enabled=False), "candidate_rows")

    assert (candidates["Plex critic wins"]["rating"],
            candidates["Plex critic wins"]["rating_source"]) == (9.0, "Plex")
    assert (candidates["Plex audience wins"]["rating"],
            candidates["Plex audience wins"]["rating_source"]) == (8.5, "Plex audience")
    assert (candidates["TMDB wins"]["rating"],
            candidates["TMDB wins"]["rating_source"]) == (9.2, "TMDB")


@pytest.mark.parametrize(("rating_source", "protected"), [
    ("critic", False),
    ("audience", True),
    ("highest", True),
])
def test_rating_protection_follows_the_chosen_number(
        tmp_path, monkeypatch, rating_source, protected):
    plex = FakePlexDataset()
    with_tmdb(plex, "100", "Mixed rating", 550, 7.0,
              rating=6.0, audienceRating=8.5)
    tmdb = FakeTmdbDataset()
    tmdb.movies[550] = tmdb_movie(550, "Mixed rating", vote_average=7.0)

    snapshot = run_scan(tmp_path, monkeypatch, plex, tmdb,
                        rating_source=rating_source, minimum_rating=8.0)
    candidates = rows(snapshot, "candidate_rows")
    protected_rows = rows(snapshot, "protected_rows")

    if protected:
        assert candidates == {}
        row = protected_rows["Mixed rating"]
        assert row["rating"] == 8.5
        assert row["protection_kind"] == "rating"
    else:
        assert protected_rows == {}
        assert candidates["Mixed rating"]["rating"] == 6.0
