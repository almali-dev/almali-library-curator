"""Every rule carries its reason as a key plus raw parameters (#47, S1a).

The parts hold what was observed — an ISO date, the user's name, the
season numbers, a count — and the locale-dependent wording is applied when
the reason is rendered. Rendering the parts in the scan's locale must give
back the exact sentence the engine stores today.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from fake_plex_server import FakePlexDataset, movie

from almali_library_curator.detection import text

from scenarios import (
    FixedSeriesStatus,
    default_settings,
    full_play,
    scan,
    sync_dataset,
    ts,
)
from test_cancelled_no_viewers import cancelled_dataset
from test_everyone_finished import everyone_finished_dataset
from test_never_watched import never_watched_dataset
from test_stalled_series import stalled_dataset

LOCALES = ("en", "ca", "es")

# The moment the stalled and never-watched scenarios report.
MOMENT = datetime(2025, 3, 15, 12, 0, tzinfo=timezone.utc)
ISO_MOMENT = MOMENT.isoformat()


def _movie_dataset() -> FakePlexDataset:
    """The S3 movie example: watched once in February 2024, never again."""
    data = FakePlexDataset()
    data.items["1"] = [movie("100", "Tenet", addedAt=ts(2020, 12, 1))]
    data.history = [full_play("100", 1, ts(2024, 2, 15))]
    return data


SCENARIOS = {
    "stalled_series": (stalled_dataset, None),
    "never_watched": (never_watched_dataset, None),
    "cancelled_no_viewers": (
        cancelled_dataset,
        FixedSeriesStatus("cancelled", ended_year=2022),
    ),
    "seasons_everyone_finished": (everyone_finished_dataset, None),
    "movies_not_rewatched": (_movie_dataset, None),
}

# The sentences the engine rendered before reasons carried parts: rendering
# the parts must reproduce them byte for byte.
EXPECTED = {
    "en": {
        "stalled_series": (
            "Anna watched S1 in March 2025. S2 and S3 were never opened."
        ),
        "never_watched": (
            "Added April 2024. No episode has ever been played."
        ),
        "cancelled_no_viewers": (
            "Cancelled in 2022. Last play August 2023, nobody mid-season."
        ),
        "seasons_everyone_finished": (
            "Marc and Anna (the only followers) both finished S1."
            " S2 is in progress."
        ),
        "movies_not_rewatched": (
            "Watched once by Marc in February 2024. Nobody since."
        ),
    },
    "ca": {
        "stalled_series": (
            "Anna va mirar S1 (març de 2025). S2 i S3 no s'han obert mai."
        ),
        "never_watched": (
            "Afegida (abril de 2024). No s'ha reproduït mai cap episodi."
        ),
        "cancelled_no_viewers": (
            "Cancel·lada el 2022. Darrera reproducció (agost de 2023),"
            " ningú a mitja temporada."
        ),
        "seasons_everyone_finished": (
            "Marc i Anna (els únics seguidors) tots dos han acabat S1."
            " S2 està en curs."
        ),
        "movies_not_rewatched": (
            "Vista una vegada per Marc (febrer de 2024). Ningú des de llavors."
        ),
    },
    "es": {
        "stalled_series": "Anna vio S1 en marzo de 2025. S2 y S3 nunca se abrieron.",
        "never_watched": (
            "Añadida en abril de 2024. Nunca se ha reproducido ningún episodio."
        ),
        "cancelled_no_viewers": (
            "Cancelada en 2022. Última reproducción en agosto de 2023,"
            " nadie a mitad de temporada."
        ),
        "seasons_everyone_finished": (
            "Marc y Anna (los únicos seguidores) han terminado S1."
            " S2 está en curso."
        ),
        "movies_not_rewatched": (
            "Vista una vez por Marc en febrero de 2024. Nadie desde entonces."
        ),
    },
}


def _candidate(conn, rule: str, locale: str):
    dataset, series_status = SCENARIOS[rule]
    sync_dataset(conn, dataset())
    (candidate,) = scan(
        conn,
        settings=default_settings(locale=locale),
        series_status=series_status,
    )
    return candidate


@pytest.mark.parametrize("rule", sorted(SCENARIOS))
@pytest.mark.parametrize("locale", LOCALES)
def test_each_rule_renders_its_parts_into_the_stored_sentence(conn, rule, locale):
    candidate = _candidate(conn, rule, locale)
    (match,) = candidate.matches

    assert text.render_reason(locale, match.reason_parts) == match.reason
    assert match.reason == EXPECTED[locale][rule]
    assert candidate.reason_parts == match.reason_parts


@pytest.mark.parametrize("locale", LOCALES)
def test_parts_keep_raw_values_and_survive_a_json_round_trip(conn, locale):
    candidate = _candidate(conn, "stalled_series", locale)
    (match,) = candidate.matches
    (part,) = match.reason_parts

    assert part["key"] == "detection.reason.stalled.plural"
    params = part["params"]
    assert params["name"] == "Anna"
    assert params["season"] == 1
    assert params["unopened"] == [2, 3]
    # A raw ISO date, not a pre-formatted "March 2025" in any locale.
    assert params["date"] == ISO_MOMENT
    assert json.loads(json.dumps(params["date"])) == ISO_MOMENT

    stored = json.loads(json.dumps(candidate.reason_parts))
    assert text.render_reason(locale, stored) == candidate.reasons[0]


@pytest.mark.parametrize("locale", LOCALES)
def test_every_finished_parts_carry_the_names_and_seasons_raw(conn, locale):
    candidate = _candidate(conn, "seasons_everyone_finished", locale)

    (finished, pending) = candidate.reason_parts
    assert finished["params"]["name1"] == "Marc"
    assert finished["params"]["name2"] == "Anna"
    assert finished["params"]["seasons"] == [1]
    assert pending["params"]["seasons"] == [2]
    # No joined list and no "S1" label in the stored values.
    assert "Marc" not in finished["params"]["seasons"]
    assert isinstance(finished["params"]["seasons"], list)


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize(
    ("key", "raw", "expected"),
    [
        (
            "detection.reason.stalled.singular",
            {"name": "Anna", "season": 1, "date": ISO_MOMENT, "unopened": [2]},
            lambda locale: {
                "name": "Anna",
                "season": "S1",
                "date": text.month_year(locale, MOMENT),
                "unopened": text.season_names(locale, [2]),
            },
        ),
        (
            "detection.reason.stalled.plural",
            {"name": "Anna", "season": 1, "date": ISO_MOMENT, "unopened": [2, 3]},
            lambda locale: {
                "name": "Anna",
                "season": "S1",
                "date": text.month_year(locale, MOMENT),
                "unopened": text.season_names(locale, [2, 3]),
            },
        ),
        (
            "detection.reason.never_watched.series",
            {"date": ISO_MOMENT, "users": 3},
            lambda locale: {"date": text.month_year(locale, MOMENT), "users": 3},
        ),
        (
            "detection.reason.never_watched.movie",
            {"date": ISO_MOMENT, "users": 3},
            lambda locale: {"date": text.month_year(locale, MOMENT), "users": 3},
        ),
        (
            "detection.reason.never_watched.series_no_plays",
            {"date": ISO_MOMENT},
            lambda locale: {"date": text.month_year(locale, MOMENT)},
        ),
        (
            "detection.reason.never_watched.movie_no_plays",
            {"date": ISO_MOMENT},
            lambda locale: {"date": text.month_year(locale, MOMENT)},
        ),
        (
            "detection.reason.cancelled.played",
            {"year": 2022, "date": ISO_MOMENT},
            lambda locale: {"year": 2022, "date": text.month_year(locale, MOMENT)},
        ),
        (
            "detection.reason.cancelled.played_no_year",
            {"year": None, "date": ISO_MOMENT},
            lambda locale: {"year": None, "date": text.month_year(locale, MOMENT)},
        ),
        (
            "detection.reason.cancelled.never_played",
            {"year": 2022},
            lambda locale: {"year": 2022},
        ),
        (
            "detection.reason.cancelled.never_played_no_year",
            {"year": None},
            lambda locale: {"year": None},
        ),
        (
            "detection.reason.everyone_finished.one",
            {"name": "Marc", "seasons": [1]},
            lambda locale: {
                "name": "Marc",
                "seasons": text.season_runs([1]),
            },
        ),
        (
            "detection.reason.everyone_finished.two",
            {"name1": "Marc", "name2": "Anna", "seasons": [1]},
            lambda locale: {
                "name1": "Marc",
                "name2": "Anna",
                "seasons": text.season_runs([1]),
            },
        ),
        (
            "detection.reason.everyone_finished.many",
            {"names": ["Marc", "Anna", "Joe"], "seasons": [1, 2, 3]},
            lambda locale: {
                "names": text.join_and(locale, ["Marc", "Anna", "Joe"]),
                "seasons": text.season_runs([1, 2, 3]),
            },
        ),
        (
            "detection.reason.pending.singular",
            {"seasons": [2]},
            lambda locale: {"seasons": text.season_names(locale, [2])},
        ),
        (
            "detection.reason.pending.plural",
            {"seasons": [2, 3]},
            lambda locale: {"seasons": text.season_names(locale, [2, 3])},
        ),
        (
            "detection.reason.not_rewatched.once",
            {"name": "Marc", "date": ISO_MOMENT},
            lambda locale: {
                "name": "Marc",
                "date": text.month_year(locale, MOMENT),
            },
        ),
        (
            "detection.reason.not_rewatched.twice",
            {"name": "Marc", "date": ISO_MOMENT},
            lambda locale: {
                "name": "Marc",
                "date": text.month_year(locale, MOMENT),
            },
        ),
        (
            "detection.reason.not_rewatched.times",
            {"count": 3, "names": ["Marc", "Anna"], "date": ISO_MOMENT},
            lambda locale: {
                "count": 3,
                "names": text.join_and(locale, ["Marc", "Anna"]),
                "date": text.month_year(locale, MOMENT),
            },
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_the_renderer_declares_every_variant_and_its_raw_kinds(
    locale, key, raw, expected
):
    parts = [{"key": key, "params": raw}]

    assert text.render_reason(locale, parts) == text.render(
        locale, key, **expected(locale)
    )
