"""Seeded demo data that fills every screen with every kind of control.

Shared by the T10 status-cue test and the 320 px browser test, so both look
at the same pages: candidates with a re-download warning, protected rows,
Leaving soon with a failed and a cancelled removal, history events of every
outcome, libraries and users for Settings and the wizard. The long titles
are deliberate: they are what would overflow a 320 px screen.
"""
from dataclasses import replace
from datetime import datetime, timezone

from almali_library_curator.config import save_settings
from almali_library_curator.lifecycle import LifecycleEngine
from almali_library_curator.scan import ScanRunner
from lifecycle_helpers import FakeCollections, make_candidate, seed_item, seed_library, seed_user

NOW = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)
GB = 1_000_000_000
LONG_TITLE = 'Doctor Strangelove or How I Learned to Stop Worrying and Love the Bomb 1964'


def configure_arrs(monkeypatch):
    """Sonarr and Radarr configured; the secrets only come from the environment."""
    for name in ('RADARR', 'SONARR'):
        monkeypatch.setenv(f'ALC_{name}_BASE_URL', f'http://{name.lower()}.invalid')
        monkeypatch.setenv(f'ALC_{name}_API_KEY', 'key')
    for name in ('PLEX_BASE_URL', 'PLEX_TOKEN'):
        monkeypatch.delenv(f'ALC_{name}', raising=False)


def _row(identifier, title, size, *, unit='movie', seasons=(), categories=('never_watched',), **extra):
    return dict(media_item_id=identifier, title=title, unit=unit, seasons=list(seasons),
                type_badge=unit.upper(), categories=list(categories),
                reasons=['Added April 2024. 0 episodes played by any of the 3 users.'],
                reason='Added April 2024. 0 episodes played by any of the 3 users.',
                size_bytes=size, disk='disk3', rating=7.4, rating_source='IMDb', **extra)


def seed_demo(app, conn):
    """Fill the database and the settings file of ``app`` with demo data."""
    # Radarr switched off for deletions: every movie row carries the warning.
    settings = replace(app.config['SETTINGS'], radarr_deletes=False)
    save_settings(settings)

    movies = seed_library(conn, '1', 'Movies')
    seed_library(conn, '2', 'TV Shows', section_type='show')
    seed_library(conn, '3', 'Filmoteca', protected=True)
    for index, name in enumerate(('Anna', 'Marc', 'Guest account with a long name')):
        seed_user(conn, str(index + 1), name)

    candidates = [
        _row(1, LONG_TITLE, 142 * GB),
        _row(2, 'Lost in Space S2–S3', 291 * GB, unit='seasons', seasons=(2, 3),
             categories=('stalled_series',)),
        _row(5, 'Foundation', 80 * GB, unit='series', categories=('cancelled_no_viewers',)),
    ]
    protected = [
        _row(3, 'Severance', 120 * GB, unit='series', protection_kind='rating',
             protection_reason='Rated 8.7, at or above the minimum rating 8.0.'),
        _row(4, 'The Office (US)', 90 * GB, unit='series', protection_kind='keep_forever',
             protection_reason='Kept forever by hand.'),
    ]

    def factory(*args):
        return {**{name: lambda: {} for name in ('sync', 'enrichment', 'recheck', 'deletion')},
                'detection': lambda: dict(candidates=len(candidates), candidate_rows=candidates,
                                          protected_rows=protected, protected=len(protected))}
    ScanRunner(settings, steps_factory=factory, clock=lambda: NOW).run('manual')

    engine = LifecycleEngine(conn, settings=settings, clock=lambda: datetime.now(timezone.utc),
                             collections=FakeCollections())
    marked = seed_item(conn, movies, '11', 'Tenet')
    failed = seed_item(conn, movies, '12', LONG_TITLE)
    cancelled = seed_item(conn, movies, '13', 'Heat')
    engine.mark_for_removal(make_candidate(marked, '11', 'Tenet'), marked_by='owner')
    failing = engine.mark_for_removal(make_candidate(failed, '12', LONG_TITLE), marked_by='owner')
    conn.execute("UPDATE removal_requests SET state='removal_failed', last_error=? WHERE id=?",
                 ('removal.failed.service', failing.id))
    conn.commit()
    engine.keep_it(engine.mark_for_removal(make_candidate(cancelled, '13', 'Heat'), marked_by='owner'))

    for outcome, month, freed, title, error in (
            ('deleted', '2026-07', 2 * GB, 'Tenet', None),
            ('cancelled', '2026-10', None, 'Heat', None),
            ('failed', '2026-11', None, LONG_TITLE, 'removal.failed.service')):
        conn.execute('INSERT INTO removal_events (outcome, title, unit, size_bytes, freed_bytes, disk,'
                     ' reason, marked_by, error, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (outcome, title, 'movie', 2 * GB, freed, 'disk3', None, 'owner', error,
                      month + '-01T12:00:00+00:00'))
    conn.commit()
    return settings
