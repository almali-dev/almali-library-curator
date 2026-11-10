"""CSV exports neutralize spreadsheet prefixes without changing plain cells (S4)."""

import csv
import io

import pytest


@pytest.mark.parametrize(
    "value, escaped",
    [
        ("=1+1", True),
        ("+1+1", True),
        ("-1+1", True),
        ("@SUM(A1)", True),
        ("\tordinary text", True),
        ("\rordinary text", True),
        ("\t", True),
        ("\r", True),
        ("  =1+1", True),
        ("  +1+1", True),
        ("  -1+1", True),
        ("  @SUM(A1)", True),
        ("\n\t =1+1", True),
        ("Plain text", False),
        ("  Plain text", False),
        ("Text =1+1", False),
        ("", False),
    ],
)
def test_history_csv_escapes_untrusted_fields(client, conn, value, escaped):
    cursor = conn.execute(
        "INSERT INTO removal_events (outcome, title, unit, reason, size_bytes,"
        " freed_bytes, disk, marked_by, occurred_at, error)"
        " VALUES ('failed', ?, 'movie', ?, 100, 0, ?, ?, ?, ?)",
        (value, value, value, value, "2026-11-01T12:00:00+00:00", value),
    )
    conn.commit()

    response = client.get('/history.csv')

    assert response.status_code == 200
    assert response.mimetype == 'text/csv'
    assert response.headers['Content-Disposition'] == 'attachment; filename="history.csv"'
    rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True), newline='')))
    expected = "'" + value if escaped else value
    assert rows == [{
        'id': str(cursor.lastrowid),
        'outcome': 'failed',
        'title': expected,
        'unit': 'movie',
        'category': '',
        'reason': expected,
        'size_bytes': '100',
        'freed_bytes': '0',
        'disk': expected,
        'marked_by': expected,
        'occurred_at': '2026-11-01T12:00:00+00:00',
        'error': expected,
    }]
    stored = conn.execute(
        "SELECT title, reason, disk, marked_by, error FROM removal_events WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    assert tuple(stored) == (value,) * 5
