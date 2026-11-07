"""S14: the per-item enrichment steps follow the curated scope.

A deselected library is left alone: its items are not sent to Sonarr or
Radarr for linking, its files are never read through the host filesystem,
and a service with nothing left in scope is not contacted at all. Users
and history are server-level facts, so the Tautulli import stays global.
"""

from __future__ import annotations

import os

from almali_library_curator.config import Settings
from almali_library_curator.hostfs import HostFilesystem
from almali_library_curator.plex import PlexClient, PlexSync
from almali_library_curator.radarr import RadarrClient
from almali_library_curator.sonarr import SonarrClient
from fake_plex_server import FakePlexDataset, FakePlexServer, episode, movie, season, series
from fake_radarr_server import FakeRadarrDataset, FakeRadarrServer, movie_resource
from fake_sonarr_server import FakeSonarrDataset, FakeSonarrServer, series_resource


def sync_scoped_libraries(conn, tmp_path):
    """Section 1 (Movies) and 2 (Shows) curated-able, section 3 (Vault) a
    deselected movie library, plus an episode whose media item is unknown."""
    media = tmp_path / "media"
    media.mkdir()
    kept = media / "kept.mkv"
    kept.write_bytes(b"kept-data")
    vault = media / "vault.mkv"
    vault.write_bytes(b"vault-data")
    episode_file = media / "episode.mkv"
    episode_file.write_bytes(b"episode-data")
    orphan = media / "orphan.mkv"
    orphan.write_bytes(b"orphan-data")

    data = FakePlexDataset()
    data.sections.append({"key": "3", "title": "Vault", "type": "movie"})
    data.items["1"] = [movie("100", "Kept", Guid=[{"id": "tmdb://42"}],
                             Part=[{"file": str(kept), "size": 999}])]
    data.items["3"] = [movie("150", "Vault item", Guid=[{"id": "tmdb://43"}],
                             Part=[{"file": str(vault), "size": 777}])]
    data.items["2"] = [series("200", "Show", Guid=[{"id": "tvdb://84"}])]
    data.children["200"] = [season("210", "200", 1)]
    data.children["210"] = [episode("300", "200", "210", 1,
                                    Part=[{"file": str(episode_file), "size": 888}])]
    with FakePlexServer(data) as server:
        PlexSync(PlexClient(server.base_url, "test-token", rate_limit_per_second=0), conn).run()
    conn.execute(
        "INSERT INTO episodes (plex_rating_key, media_item_id, series_rating_key, season_number,"
        " episode_number, title, file_path, file_size_bytes)"
        " VALUES ('399', NULL, '200', 1, 9, 'Orphan', ?, 1)", (str(orphan),))
    conn.commit()
    return media


def radarr_with(*resources):
    data = FakeRadarrDataset()
    data.movies = list(resources)
    return data


def sonarr_with(*resources):
    data = FakeSonarrDataset()
    data.series = list(resources)
    return data


def clients(radarr_server, sonarr_server):
    return (RadarrClient(radarr_server.base_url, "test-api-key", rate_limit_per_second=0),
            SonarrClient(sonarr_server.base_url, "test-api-key", rate_limit_per_second=0))


def test_link_arr_links_only_curated_items_and_leaves_the_rest_alone(conn, tmp_path):
    from almali_library_curator.enrichment import link_arr

    sync_scoped_libraries(conn, tmp_path)
    scoped = Settings(config_dir=tmp_path, curated_libraries=("1", "2"))
    with FakeRadarrServer(radarr_with(movie_resource(10, "Kept", tmdbId=42),
                                      movie_resource(11, "Vault item", tmdbId=43))) as r, \
            FakeSonarrServer(sonarr_with(series_resource(20, "Show", tvdbId=84))) as s:
        radarr_client, sonarr_client = clients(r, s)
        summary = link_arr(conn, radarr=radarr_client, sonarr=sonarr_client, settings=scoped)

    assert summary.radarr_linked == 1 and summary.sonarr_linked == 1
    # A curated library's item still links.
    assert conn.execute(
        "SELECT radarr_id FROM media_items WHERE plex_rating_key='100'").fetchone()[0] == 10
    # The deselected library's item is not linked.
    assert conn.execute(
        "SELECT radarr_id FROM media_items WHERE plex_rating_key='150'").fetchone()[0] is None


def test_link_arr_contacts_no_arr_whose_items_are_all_out_of_scope(conn, tmp_path):
    from almali_library_curator.enrichment import link_arr

    sync_scoped_libraries(conn, tmp_path)
    scoped = Settings(config_dir=tmp_path, curated_libraries=("2",))  # shows only: no movies
    with FakeRadarrServer(radarr_with(movie_resource(10, "Kept", tmdbId=42))) as r, \
            FakeSonarrServer(sonarr_with(series_resource(20, "Show", tvdbId=84))) as s:
        radarr_client, sonarr_client = clients(r, s)
        summary = link_arr(conn, radarr=radarr_client, sonarr=sonarr_client, settings=scoped)

        assert r.requests == []  # no curated movie: Radarr is not contacted at all
        assert s.requests  # the curated show still links through Sonarr
    assert summary.radarr_linked == 0 and summary.sonarr_linked == 1


def test_a_scoped_refresh_keeps_a_deselected_library_stale_link_untouched(conn, tmp_path):
    from almali_library_curator.enrichment import link_arr

    sync_scoped_libraries(conn, tmp_path)
    unscoped = Settings(config_dir=tmp_path)
    scoped = Settings(config_dir=tmp_path, curated_libraries=("1", "2"))
    data = radarr_with(movie_resource(10, "Kept", tmdbId=42),
                       movie_resource(11, "Vault item", tmdbId=43))
    with FakeRadarrServer(data) as server:
        client = RadarrClient(server.base_url, "test-api-key", rate_limit_per_second=0)
        assert link_arr(conn, radarr=client, settings=unscoped).radarr_linked == 2

        # The deselected movie now disappears from Radarr: unscoped, a refresh
        # would clear its stale link; scoped, it is left alone.
        data.movies = [movie_resource(10, "Kept", tmdbId=42)]
        assert link_arr(conn, radarr=client, settings=scoped).radarr_linked == 1
        assert conn.execute(
            "SELECT radarr_id FROM media_items WHERE plex_rating_key='150'").fetchone()[0] == 11


def test_refresh_files_reads_only_the_curated_libraries_files(conn, tmp_path):
    from almali_library_curator.enrichment import refresh_files

    media = sync_scoped_libraries(conn, tmp_path)
    stat_calls: list[str] = []

    def recording_stat(path):
        stat_calls.append(os.fspath(path))
        return os.stat(path)

    scoped = Settings(config_dir=tmp_path, curated_libraries=("1", "2"))
    summary = refresh_files(conn, HostFilesystem("", media, stat=recording_stat), settings=scoped)

    assert (summary.movies, summary.episodes, summary.unavailable) == (1, 1, 0)
    assert any("kept" in call for call in stat_calls)  # the curated movie is still read
    assert not any("vault" in call for call in stat_calls)  # deselected library: never read
    assert not any("orphan" in call for call in stat_calls)  # unknown media item: outside every scope
    # The deselected item keeps Plex's facts untouched; nothing was invented.
    row = conn.execute(
        "SELECT file_size_bytes, hardlink_count, disk FROM media_items WHERE plex_rating_key='150'"
    ).fetchone()
    assert tuple(row) == (777, None, None)


def test_without_a_selection_every_step_behaves_exactly_as_before(conn, tmp_path):
    from almali_library_curator.enrichment import link_arr, refresh_files

    media = sync_scoped_libraries(conn, tmp_path)
    unscoped = Settings(config_dir=tmp_path)  # curated_libraries == ()
    stat_calls: list[str] = []

    def recording_stat(path):
        stat_calls.append(os.fspath(path))
        return os.stat(path)

    summary = refresh_files(conn, HostFilesystem("", media, stat=recording_stat), settings=unscoped)
    assert (summary.movies, summary.episodes) == (2, 2)  # both movies and both episodes
    assert any("vault" in call for call in stat_calls)
    assert any("orphan" in call for call in stat_calls)

    with FakeRadarrServer(radarr_with(movie_resource(10, "Kept", tmdbId=42),
                                      movie_resource(11, "Vault item", tmdbId=43))) as server:
        client = RadarrClient(server.base_url, "test-api-key", rate_limit_per_second=0)
        assert link_arr(conn, radarr=client, settings=unscoped).radarr_linked == 2


def test_run_enrichment_passes_the_scope_to_the_per_item_steps(conn, tmp_path):
    from almali_library_curator.enrichment import run_enrichment

    sync_scoped_libraries(conn, tmp_path)
    scoped = Settings(config_dir=tmp_path, curated_libraries=("2",))  # no movies in scope
    with FakeRadarrServer(radarr_with(movie_resource(10, "Kept", tmdbId=42))) as server:
        client = RadarrClient(server.base_url, "test-api-key", rate_limit_per_second=0)
        summary = run_enrichment(conn, scoped, radarr=client)

        assert server.requests == []  # the runner wires the scope into every step
    assert summary.linking.radarr_linked == 0
