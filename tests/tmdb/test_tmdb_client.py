from __future__ import annotations

from types import SimpleNamespace

import pytest

from fake_tmdb_server import FakeTmdbDataset, FakeTmdbServer, tmdb_movie, tmdb_show
from almali_library_curator.tmdb import (
    TmdbAuthError,
    TmdbClient,
    TmdbError,
    TmdbUnavailableError,
)

SECRET = "super-secret-access-token"


@pytest.fixture()
def dataset() -> FakeTmdbDataset:
    return FakeTmdbDataset(access_token=SECRET)


@pytest.fixture()
def server(dataset):
    with FakeTmdbServer(dataset) as running:
        yield running


def build_client(server, **overrides) -> TmdbClient:
    options = {
        "base_url": server.base_url,
        "access_token": SECRET,
        "rate_limit_per_second": 1000.0,
        "backoff_seconds": 0.0,
    }
    options.update(overrides)
    return TmdbClient(**options)


def test_movie_returns_typed_details(server, dataset):
    dataset.movies[550] = tmdb_movie(
        550, "Fight Club", vote_average=8.4, release_date="1999-10-15"
    )

    movie = build_client(server).movie(550)

    assert movie.id == 550
    assert movie.title == "Fight Club"
    assert movie.vote_average == 8.4
    assert movie.status == "Released"
    assert movie.release_date == "1999-10-15"


def test_tv_returns_typed_details(server, dataset):
    dataset.shows[4629] = tmdb_show(
        4629, "Lost in Space", status="Canceled", last_air_date="2021-12-01"
    )

    show = build_client(server).tv(4629)

    assert show.id == 4629
    assert show.name == "Lost in Space"
    assert show.vote_average == 8.1
    assert show.status == "Canceled"
    assert show.last_air_date == "2021-12-01"


def test_the_token_travels_as_a_bearer_header_never_the_url(server, dataset):
    dataset.movies[550] = tmdb_movie(550, "Fight Club")

    build_client(server).movie(550)

    request = server.requests_for("/3/movie/550")[0]
    assert request["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert SECRET not in request["path"]
    assert SECRET not in request["query"]
    # No request the client made ever carries the token in a URL part,
    # where a log record or an error message could pick it up.
    for recorded in server.requests:
        assert SECRET not in recorded["path"]
        assert SECRET not in recorded["query"]


def test_the_token_never_reaches_an_error_or_a_log_record(server, dataset, caplog):
    dataset.movies[550] = tmdb_movie(550, "Fight Club")
    dataset.fail_always("/3/movie/550", status=503)
    client = build_client(server, max_retries=0)

    with pytest.raises(TmdbUnavailableError) as failure:
        client.movie(550)

    assert SECRET not in str(failure.value)
    assert SECRET not in caplog.text


def test_rejected_credentials_raise_an_auth_error_without_retrying(server, dataset):
    dataset.movies[550] = tmdb_movie(550, "Fight Club")
    client = build_client(server, access_token="wrong-token")

    with pytest.raises(TmdbAuthError) as failure:
        client.movie(550)

    assert "wrong-token" not in str(failure.value)
    assert len(server.requests_for("/3/movie/550")) == 1


def test_a_transient_503_is_retried_once_then_succeeds(server, dataset):
    dataset.movies[550] = tmdb_movie(550, "Fight Club")
    dataset.fail_once("/3/movie/550", status=503)
    client = build_client(server, max_retries=2)

    movie = client.movie(550)

    assert movie.title == "Fight Club"
    assert len(server.requests_for("/3/movie/550")) == 2


def test_a_redirect_is_refused_and_never_followed(server, dataset):
    dataset.movies[550] = tmdb_movie(550, "Fight Club")
    dataset.redirects["/3/movie/550"] = "http://127.0.0.1:9/steal-the-token"

    with pytest.raises(TmdbError) as failure:
        build_client(server).movie(550)

    assert not isinstance(failure.value, TmdbUnavailableError)
    assert "redirect" in str(failure.value)
    assert SECRET not in str(failure.value)
    assert len(server.requests_for("/3/movie/550")) == 1


def test_an_unknown_id_raises_a_structured_error(server, dataset):
    client = build_client(server)

    with pytest.raises(TmdbError) as failure:
        client.movie(550)

    assert "/3/movie/550" in str(failure.value)


def test_from_settings_builds_the_client_from_the_settings_attributes(server, dataset):
    dataset.movies[550] = tmdb_movie(550, "Fight Club")
    settings = SimpleNamespace(
        tmdb_base_url=server.base_url, tmdb_api_token=SECRET
    )

    client = TmdbClient.from_settings(
        settings, rate_limit_per_second=1000.0, backoff_seconds=0.0
    )

    assert TmdbClient(access_token=SECRET).base_url == "https://api.themoviedb.org"
    assert SECRET not in repr(client)
    assert client.movie(550).title == "Fight Club"
