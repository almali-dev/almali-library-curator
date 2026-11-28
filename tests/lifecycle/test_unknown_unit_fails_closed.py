"""#133 T1: a removal unit the executor does not know fails closed.

Every unit dispatch used to read ``if unit is MOVIE ... else <series>``, so
a unit added later (the version unit of #133) would have been routed as a
whole series: Sonarr ``delete_series`` or every episode through Plex. An
unknown unit now ends as ``removal.failed.unsupported_scope`` before the
claim and before any request to Radarr, Sonarr or Plex.
"""

from __future__ import annotations

from dataclasses import replace
from enum import Enum

import pytest

from almali_library_curator.detection.models import Unit
from test_s15_plex_fallback import Scenario


class _Later(str, Enum):
    """A unit value this build does not know, standing in for a future one."""

    VERSION = "version"


def _with_unknown_unit(scenario):
    read = scenario.engine._request_by_id

    def request_by_id(request_id):
        return replace(read(request_id), unit=_Later.VERSION)

    scenario.engine._request_by_id = request_by_id


@pytest.mark.parametrize("dry_run", [False, True])
def test_an_unknown_unit_is_refused_before_any_request(conn, tmp_path, dry_run):
    scenario = Scenario(conn, tmp_path, unit=Unit.SERIES, sonarr=True, radarr=True,
                        dry_run=dry_run)
    request = scenario.add()
    assert scenario.passed_ids == [request.id]
    before = scenario.row(request)
    _with_unknown_unit(scenario)

    report = scenario.run()

    assert [(o.status, o.error) for o in report.outcomes] == [
        ("failed", "removal.failed.unsupported_scope")]
    assert scenario.plex_deletes == []
    assert scenario.arr_writes == []
    assert scenario.arr_requests == 0
    assert all(path.exists() for path in scenario.files.values())
    row = scenario.row(request)
    if dry_run:
        # The preview reports the failure it would record and stores nothing.
        assert dict(row) == dict(before)
    else:
        assert row["state"] == "removal_failed"
        assert row["last_error"] == "removal.failed.unsupported_scope"
        assert row["deleted_at"] is None


def test_the_unsupported_scope_failure_reads_in_every_language():
    from almali_library_curator.i18n import translate

    for locale in ("en", "ca", "es"):
        text = translate(locale, "removal.failed.unsupported_scope")
        assert text and "removal.failed" not in text, locale


def test_the_final_check_never_passes_an_unknown_unit(conn, tmp_path):
    scenario = Scenario(conn, tmp_path, unit=Unit.SERIES)
    request = scenario.add()
    conn.execute("UPDATE removal_requests SET state = 'marked' WHERE id = ?", (request.id,))
    conn.commit()
    _with_unknown_unit(scenario)

    report = scenario.engine.run_recheck()

    assert report.passed == ()
    assert report.errors
    assert scenario.row(request)["state"] == "marked"
