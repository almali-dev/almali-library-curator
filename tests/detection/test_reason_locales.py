"""Reason sentences render the same facts through ca and es templates."""

from __future__ import annotations

import pytest

from fake_plex_server import FakePlexDataset, movie

from scenarios import (
    FixedSeriesStatus,
    MOVIE_SIZE,
    add_series,
    default_settings,
    full_play,
    partial_play,
    scan,
    sync_dataset,
    ts,
)
from test_cancelled_no_viewers import cancelled_dataset
from test_everyone_finished import everyone_finished_dataset
from test_never_watched import never_watched_dataset
from test_stalled_series import stalled_dataset


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        (
            "ca",
            {
                "stalled": "Anna va mirar S1 (març de 2025). S2 i S3 no s'han obert mai.",
                "never_watched": (
                    "Afegida (abril de 2024). No s'ha reproduït mai cap episodi."
                ),
                "cancelled": (
                    "Cancel·lada el 2022. Darrera reproducció (agost de 2023),"
                    " ningú a mitja temporada."
                ),
                "everyone_finished": (
                    "Marc i Anna (els únics seguidors) tots dos han acabat S1."
                    " S2 està en curs."
                ),
                "movies_not_rewatched": (
                    "Vista una vegada per Marc (febrer de 2024). Ningú des de llavors."
                ),
            },
        ),
        (
            "es",
            {
                "stalled": "Anna vio S1 en marzo de 2025. S2 y S3 nunca se abrieron.",
                "never_watched": (
                    "Añadida en abril de 2024. Nunca se ha reproducido ningún episodio."
                ),
                "cancelled": (
                    "Cancelada en 2022. Última reproducción en agosto de 2023,"
                    " nadie a mitad de temporada."
                ),
                "everyone_finished": (
                    "Marc y Anna (los únicos seguidores) han terminado S1."
                    " S2 está en curso."
                ),
                "movies_not_rewatched": (
                    "Vista una vez por Marc en febrero de 2024. Nadie desde entonces."
                ),
            },
        ),
    ],
)
def test_each_category_renders_its_reason_in_the_locale(conn, locale, expected):
    settings = default_settings(locale=locale)

    # Stalled series.
    sync_dataset(conn, stalled_dataset())
    (stalled,) = scan(conn, settings=settings)
    assert stalled.reasons == (expected["stalled"],)
    conn.execute("DELETE FROM play_events")
    conn.execute("DELETE FROM media_items")
    conn.execute("DELETE FROM episodes")

    # Never watched.
    sync_dataset(conn, never_watched_dataset())
    (never,) = scan(conn, settings=settings)
    assert never.reasons == (expected["never_watched"],)
    conn.execute("DELETE FROM play_events")
    conn.execute("DELETE FROM media_items")
    conn.execute("DELETE FROM episodes")

    # Cancelled, no viewers.
    sync_dataset(conn, cancelled_dataset())
    (cancelled,) = scan(
        conn, settings=settings, series_status=FixedSeriesStatus("cancelled", ended_year=2022)
    )
    assert cancelled.reasons == (expected["cancelled"],)
    conn.execute("DELETE FROM play_events")
    conn.execute("DELETE FROM media_items")
    conn.execute("DELETE FROM episodes")

    # Seasons everyone finished.
    sync_dataset(conn, everyone_finished_dataset())
    (finished,) = scan(conn, settings=settings)
    assert finished.reasons == (expected["everyone_finished"],)
    conn.execute("DELETE FROM play_events")
    conn.execute("DELETE FROM media_items")
    conn.execute("DELETE FROM episodes")

    # Movies not rewatched.
    data = FakePlexDataset()
    data.items["1"] = [movie("100", "Tenet", addedAt=ts(2020, 12, 1))]
    data.history = [full_play("100", 1, ts(2024, 2, 15))]
    sync_dataset(conn, data)
    (rewatched,) = scan(conn, settings=settings)
    assert rewatched.reasons == (expected["movies_not_rewatched"],)
    assert rewatched.size_bytes == MOVIE_SIZE
