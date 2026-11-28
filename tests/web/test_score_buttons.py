"""#162: the yellow IMDb button with its score, and the Rotten Tomatoes button.

Owner, verbatim: "No m'agrada. Deixa només el botó groc i posa la puntuació
de la Serie/peli... es podria afegir de la mateixa forma el de Rotten
Tomatoes?" (scores from Plex only; the Rotten Tomatoes button always, with
its score when known).
"""

from __future__ import annotations

from html import unescape
import re

from lifecycle_helpers import seed_item, seed_library
from test_imdb_links import GB, TITLE_PAGE, candidate_row, page, publish

RT_TENET = 'https://www.rottentomatoes.com/search?search=Tenet'


def link(html, kind):
    """The one ``kind`` link of the page: its href, label, tooltip and words."""
    [(href, label, tooltip, body)] = re.findall(
        rf'<a class="score-link score-link--{kind}" href="([^"]+)"\s+target="_blank"'
        r'\s+rel="noopener noreferrer"\s+aria-label="([^"]*)"\s+title="([^"]*)">(.*?)</a>',
        html, re.S)
    return unescape(href), unescape(label), unescape(tooltip), ' '.join(
        re.sub(r'<[^>]+>', ' ', body).split())


def card(app, conn, client, **scores):
    library = seed_library(conn, '1', 'Movies')
    item = seed_item(conn, library, '11', 'Tenet')
    row = candidate_row(item, 'Tenet', 10 * GB, imdb_url=TITLE_PAGE)
    row.update(scores)
    publish(conn, [row])
    return page(client, '/?tab=candidates&lang=en')


def test_the_imdb_button_carries_its_score(app, conn, client):
    html = card(app, conn, client, imdb_rating=7.3, rt_critic=82, rt_url=RT_TENET)

    href, label, tooltip, words = link(html, 'imdb')
    assert href == TITLE_PAGE
    assert words == '7.3'
    assert label == tooltip == 'Open Tenet on IMDb, rated 7.3'
    assert re.search(r'class="score-link score-link--imdb"[^>]*>\s*<span class="score-badge">\s*<img class="imdb-logo"', html)
    # Only the yellow button: no frame around it, no "opens elsewhere" arrow.
    assert 'imdb-external' not in html and '#external' not in html


def test_the_rotten_tomatoes_button_carries_the_tomatometer(app, conn, client):
    html = card(app, conn, client, imdb_rating=7.3, rt_critic=82, rt_url=RT_TENET)

    href, label, tooltip, words = link(html, 'rt')
    assert href == RT_TENET
    assert words == '82%'
    assert label == tooltip == 'Open Tenet on Rotten Tomatoes, Tomatometer 82%'
    assert re.search(r'<img class="rt-logo" src="[^"]*rt-tomato\.svg[^"]*" alt=""', html)


def test_without_scores_both_buttons_stay_without_numbers(app, conn, client):
    html = card(app, conn, client, rt_url=RT_TENET)

    assert link(html, 'imdb')[1:] == ('Open Tenet on IMDb', 'Open Tenet on IMDb', '')
    assert link(html, 'rt')[1:] == (
        'Open Tenet on Rotten Tomatoes', 'Open Tenet on Rotten Tomatoes', '')


def test_a_row_published_before_the_scores_still_gets_the_rotten_tomatoes_button(app, conn, client):
    """Owner: "no està el de rotten tomatoes". Rows a pre-0.50 scan
    published carry no address; the card builds it from the item's title."""
    html = card(app, conn, client)

    href, label, _tooltip, words = link(html, 'rt')
    assert href == RT_TENET
    assert label == 'Open Tenet on Rotten Tomatoes'
    assert words == ''


def test_a_stored_rotten_tomatoes_address_the_app_did_not_build_is_never_a_link(app, conn, client):
    html = card(app, conn, client, rt_critic=82, rt_url='https://evil.example/search?search=Tenet')

    assert 'score-link--rt' not in html
    assert 'evil.example' not in html


def test_the_scores_are_translated(app, conn, client):
    card(app, conn, client, imdb_rating=7.3, rt_critic=82, rt_url=RT_TENET)

    html = page(client, '/?tab=candidates&lang=ca')
    assert link(html, 'imdb')[1] == 'Obre Tenet a IMDb, puntuació 7.3'
    assert link(html, 'rt')[1] == 'Obre Tenet a Rotten Tomatoes, Tomatometer 82%'


def test_the_buttons_look_like_buttons():
    from pathlib import Path

    css = (Path(__file__).resolve().parents[2]
           / 'almali_library_curator/static/css/hud.css').read_text(encoding='utf-8')
    imdb = re.search(r'\.score-link--imdb \.score-badge\s*\{([^}]*)\}', css).group(1)
    rt = re.search(r'\.score-link--rt \.score-badge\s*\{([^}]*)\}', css).group(1)
    assert '#f5c518' in imdb.lower() and 'border: 0' in imdb
    assert '#fa320a' in rt.lower()
    assert re.search(r'\.score-link\s*\{[^}]*cursor:\s*pointer', css)
    assert re.search(r'\.score-link:hover[^{]*\{[^}]*(filter|transform)', css)
    assert re.search(r'\.score-link:focus-visible[^{]*\{[^}]*outline', css)


def test_the_coloured_button_is_smaller_than_its_44px_hit_area(app, conn, client):
    """Owner: "butó massa gran". The link keeps the 44 px target of every
    control; only a smaller badge inside it is painted."""
    from pathlib import Path

    html = card(app, conn, client, imdb_rating=7.3, rt_critic=82, rt_url=RT_TENET)
    for kind, logo in (('imdb', 'imdb-logo'), ('rt', 'rt-logo')):
        assert re.search(
            rf'class="score-link score-link--{kind}"[^>]*>\s*<span class="score-badge">'
            rf'\s*<img class="{logo}"', html), kind

    css = (Path(__file__).resolve().parents[2]
           / 'almali_library_curator/static/css/hud.css').read_text(encoding='utf-8')
    link_rule = re.search(r'\.score-link\s*\{([^}]*)\}', css).group(1)
    badge_rule = re.search(r'\.score-badge\s*\{([^}]*)\}', css).group(1)
    assert 'min-height: 44px' in link_rule and 'background' not in link_rule
    height = int(re.search(r'(?<!-)height:\s*(\d+)px', badge_rule).group(1))
    assert height <= 26
