"""One HTML escaper in the repo, and every page that calls it defines it (ADR-X1, #175).

Seven boards had their own client-side escaper; two skipped the quote, which is what an
attribute needs. ``html_safe.ESC_JS`` (``rcEsc``) and ``html_safe.esc`` are the only ones now.
Two scans, so a new copy fails whichever way it arrives:

- the source: no other escaper definition, alias or replace chain in a ``.py`` / ``.js`` /
  ``.html`` file that is not a test;
- the rendered pages: every board of ``board_fixtures.BOARDS`` and the shell. A page may call
  ``rcEsc`` only if it defines it, and may not call any other ``esc``-like function without
  defining it (that is a ``ReferenceError`` the first time the handler runs).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from RevenueChaser.core.tests.board_fixtures import BOARDS

ROOT = Path(__file__).resolve().parents[2]
_SKIP_DIRS = {".git", ".cache", ".store", ".claude", ".hermes", "docs", "vendor", "tests",
              "research", "module_testing", "node_modules", "__pycache__", "testWithAstra"}
ALLOWED = "core/html_safe.py"

# esc, tcEsc, rcEsc, escHtml, escapeHtml ...; not "rescore" or "descriptor"
_NAME = r"(?:esc|[a-z]{1,3}Esc|escHtml|escHTML|escapeHtml|escapeHTML|htmlEsc|htmlEscape)"
_DEFINES = [
    re.compile(r"function\s+" + _NAME + r"\s*\("),
    re.compile(r"(?<![\w$.])(?:var|let|const)\s+" + _NAME + r"\s*="),
    re.compile(r"(?<![\w$.])" + _NAME + r"\s*=\s*function"),
    re.compile(r"\.replace\(\s*/&/g"),
    re.compile(r"\.replace\(\s*/\[&<>"),
    re.compile(r"textContent\s*=\s*\w+;\s*return\s+\w+\.innerHTML"),
    re.compile(r"^\s*def\s+(?:_?esc|_h|_e|_?escape\w*|_?script_json)\s*\(", re.M),
]


def _sources():
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT)
        if (path.suffix in {".py", ".js", ".html", ".j2"} and path.is_file()
                and not set(rel.parts[:-1]) & _SKIP_DIRS):
            yield rel.as_posix(), path.read_text(encoding="utf-8", errors="replace")


SOURCES = dict(_sources())


def test_the_scan_reads_the_known_escaper():
    assert "function rcEsc(" in SOURCES[ALLOWED] and "def esc(" in SOURCES[ALLOWED]
    assert len(SOURCES) > 100


@pytest.mark.parametrize("rel", sorted(SOURCES))
def test_no_other_file_defines_an_escaper(rel):
    if rel == ALLOWED:
        return
    for rx in _DEFINES:
        m = rx.search(SOURCES[rel])
        assert not m, f"{rel}: a second escaper ({m.group(0).strip()!r}); use html_safe.esc / ESC_JS"


# ── rendered pages ────────────────────────────────────────────────────────────

_CALL = re.compile(r"(?<![\w$.])(" + _NAME + r")\(")
_DEFINED = re.compile(r"function\s+(" + _NAME + r")\s*\(")


def check_page(html: str) -> None:
    defined = set(_DEFINED.findall(html))
    assert defined <= {"rcEsc"}, f"a second escaper is defined in the page: {sorted(defined)}"
    called = {m.group(1) for m in _CALL.finditer(html)
              if not html[max(0, m.start() - 9):m.start()].endswith("function ")}
    missing = called - defined
    assert not missing, f"called but not defined in the page: {sorted(missing)} (add html_safe.ESC_JS)"


@pytest.mark.parametrize("key", sorted(BOARDS))
def test_a_rendered_board_defines_every_escaper_it_calls(key, tmp_path, monkeypatch, rawstore_tmp_db):
    check_page(BOARDS[key].render(tmp_path, monkeypatch))


def _snippets() -> dict[str, str]:
    """The shared JS snippets a board embeds, each of which may be the only one on its page."""
    from RevenueChaser.core import reporting_common
    from RevenueChaser.module_earnings_radar.report import DETAIL_JS
    found = {n: v for n, v in vars(reporting_common).items() if n.endswith("_JS") and isinstance(v, str)}
    return {**found, "DETAIL_JS": DETAIL_JS}


@pytest.mark.parametrize("name", sorted(_snippets()))
def test_a_shared_snippet_that_calls_rcesc_defines_it(name):
    """A board may embed one snippet without the others, so each carries its own ``rcEsc``."""
    snippet = _snippets()[name]
    if re.search(r"(?<![\w$.])rcEsc\(", snippet.replace("function rcEsc(", "")):
        assert "function rcEsc(" in snippet, f"{name} calls rcEsc without defining it (prepend html_safe.ESC_JS)"


def test_the_snippet_check_sees_the_snippets_that_call_the_escaper():
    called = [n for n, v in _snippets().items() if "rcEsc(" in v.replace("function rcEsc(", "")]
    assert {"FILTERS_JS", "DETAIL_JS"} <= set(called)


def test_the_shell_defines_every_escaper_it_calls():
    from RevenueChaser.app import dashboard
    check_page(dashboard.app.test_client().get("/").get_data(as_text=True))


def test_the_page_check_itself_catches_the_three_failure_modes():
    ok = "<script>function rcEsc(s){return s}\nrcEsc(1)</script>"
    check_page(ok)
    with pytest.raises(AssertionError, match="called but not defined"):
        check_page("<script>rcEsc(1)</script>")
    with pytest.raises(AssertionError, match="called but not defined"):
        check_page("<script>function rcEsc(s){}\nesc(1)</script>")
    with pytest.raises(AssertionError, match="second escaper"):
        check_page("<script>function rcEsc(s){}\nfunction esc(s){}\nesc(1)</script>")
