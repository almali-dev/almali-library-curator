"""T10 guards: no literal owner-facing copy in templates or Python (S7, S9).

S7: "UI text in English, Catalan and Spanish from day one; no hard-coded
strings in templates or scripts." S9: "every string through translation keys."
"""
from __future__ import annotations

import ast
from html.parser import HTMLParser
import json
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[2] / "almali_library_curator"
TEMPLATE_DIRS = (ROOT / "templates", ROOT / "web" / "templates" / "web")
# Attributes a person reads or hears; their value must come from a key.
HUMAN_ATTRIBUTES = ("aria-label", "title", "placeholder", "alt")
CATALOGS = {locale: json.loads((ROOT / f"i18n/{locale}.json").read_text())
            for locale in ("en", "ca", "es")}
# Owner-facing key families the Python side hands to the templates.
KEY_PREFIXES = ("web.flash.", "web.form.", "error.")
PYTHON_SOURCES = sorted((ROOT / "web").glob("*.py")) + [ROOT / "app.py"]


class _Copy(HTMLParser):
    """Collects literal text and literal human-text attribute values."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in HUMAN_ATTRIBUTES and value and re.search(r"[^\W\d_]", value):
                self.found.append(f"{tag}[{name}]={value!r}")

    handle_startendtag = handle_starttag

    def handle_data(self, data):
        if re.search(r"[^\W\d_]", data):
            self.found.append(data.strip())


def literal_copy(template: str) -> list[str]:
    """Literal letters left once every Jinja tag, expression and comment is gone."""
    stripped = re.sub(r"\{#.*?#\}|\{%.*?%\}|\{\{.*?\}\}", "", template, flags=re.S)
    parser = _Copy()
    parser.feed(stripped)
    parser.close()
    return parser.found


@pytest.mark.parametrize("snippet,expected", [
    ("<p>Hello</p>", ["Hello"]),
    ('<a href="/" aria-label="Close">{{ t("x") }}</a>', ["a[aria-label]='Close'"]),
    ('<abbr title="Gigabyte">{{ t("x") }}</abbr>', ["abbr[title]='Gigabyte'"]),
    ('<input placeholder="Search" />', ["input[placeholder]='Search'"]),
    ('<img src="/x.svg" alt="Logo" />', ["img[alt]='Logo'"]),
    ('<img src="/x.svg" alt="" /><a aria-label="{{ t(\'nav.main\') }}">{{ t("y") }}</a>', []),
    ('{# A comment #}{% if x %}<b>{{ t("k") }}</b>{% endif %} 12 · 3', []),
])
def test_the_scanner_flags_literal_text_and_human_attributes(snippet, expected):
    assert literal_copy(snippet) == expected


def _templates():
    paths = [path for directory in TEMPLATE_DIRS for path in sorted(directory.rglob("*.html"))]
    assert {path.parent for path in paths} >= set(TEMPLATE_DIRS) | {ROOT / "templates" / "macros"}
    return paths


@pytest.mark.parametrize("path", _templates(), ids=lambda path: str(path.relative_to(ROOT)))
def test_no_template_carries_literal_copy_in_text_or_human_attributes(path):
    assert literal_copy(path.read_text()) == []


def _string_constants(source: str):
    """(line, value) of every string literal that is not a docstring."""
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value) for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value


def _flash_literals(source: str):
    """Literal first arguments of every ``flash(...)`` call."""
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "flash"
                and node.args and isinstance(node.args[0], ast.Constant)):
            yield node.lineno, node.args[0].value


def python_copy_problems(source: str) -> list[str]:
    problems = []
    for line, value in _string_constants(source):
        if value.startswith(KEY_PREFIXES) and not value.endswith((".html", ".json", ".csv")):
            # A literal may be a prefix completed at run time ('web.form.plex_' + outcome).
            missing = [locale for locale, catalog in CATALOGS.items()
                       if value not in catalog and not any(key.startswith(value) for key in catalog)]
            if missing:
                problems.append(f"{line}: key {value!r} missing in {missing}")
    for line, value in _flash_literals(source):
        if value not in CATALOGS["en"]:
            problems.append(f"{line}: flash({value!r}) is not a translation key")
    return problems


@pytest.mark.parametrize("source,expected", [
    ("flash('Settings saved.')", ["1: flash('Settings saved.') is not a translation key"]),
    ("errors['x'] = 'web.form.does_not_exist'", ["1: key 'web.form.does_not_exist' missing in ['en', 'ca', 'es']"]),
    ("flash('web.flash.settings_saved')\nkey = 'web.form.' + outcome", []),
])
def test_the_python_guard_flags_literals_and_unknown_keys(source, expected):
    assert python_copy_problems(source) == expected


@pytest.mark.parametrize("path", PYTHON_SOURCES, ids=lambda path: path.name)
def test_python_owner_copy_goes_through_existing_translation_keys(path):
    assert python_copy_problems(path.read_text()) == []
