"""Every browser write sends ``X-RC-Panel`` (#231).

The server refuses a write without the header (``app/tests/test_panel_header_231.py``);
this is the other half: each ``fetch(..., {method: 'POST'})`` a page ships must send it,
or that button silently stops working. Two scans, so neither can go blind alone:

- the source: every ``method: 'POST'`` in a ``.py``/``.html``/``.js`` file that is not a test;
- the rendered pages: every board of ``board_fixtures.BOARDS`` plus the shell at ``/``.

A write that is not a ``fetch`` (a form, an ``XMLHttpRequest``) cannot carry the header
from markup, so the source scan also fails on those.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from RevenueChaser.core.tests.board_fixtures import BOARDS

ROOT = Path(__file__).resolve().parents[2]
HEADER = "X-RC-Panel"
_POST = re.compile(r"""method\s*:\s*['"]POST['"]""")
_NOT_FETCH = re.compile(r"""<form[^>]*method\s*=\s*['"]?post|XMLHttpRequest|sendBeacon""", re.I)
_SKIP_DIRS = {".git", ".cache", ".store", ".claude", ".hermes", "docs", "vendor", "tests",
              "research", "module_testing", "node_modules", "__pycache__"}


def post_calls(text: str) -> list[str]:
    """The text of each ``fetch( ... )`` call that carries ``method: 'POST'``."""
    calls = []
    for m in _POST.finditer(text):
        start = text.rfind("fetch(", max(0, m.start() - 600), m.start())
        assert start >= 0, f"a POST that is not in a fetch call: {text[m.start() - 60:m.end() + 60]!r}"
        depth, quote, i = 0, "", start + len("fetch")
        while i < len(text):
            ch = text[i]
            if quote:
                if ch == "\\":
                    i += 1
                elif ch == quote:
                    quote = ""
            elif ch in "'\"":
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        calls.append(text[start:i + 1])
    return calls


def _source_files():
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT)
        if (path.suffix in {".py", ".html", ".js", ".j2"} and path.is_file()
                and not set(rel.parts[:-1]) & _SKIP_DIRS):
            yield path, rel.as_posix()


SOURCES = {rel: path.read_text(encoding="utf-8", errors="replace") for path, rel in _source_files()}
SOURCE_POSTS = {rel: post_calls(text) for rel, text in SOURCES.items() if _POST.search(text)}


def test_the_source_scan_sees_the_known_write_sites():
    """10 fetch POSTs on e83aa26; a count that drops means the scan went blind."""
    assert sum(len(v) for v in SOURCE_POSTS.values()) >= 10
    assert {"templates/dashboard.html", "core/reporting_common.py", "module_settings/report.py",
            "module_leaps/report.py", "module_uw_interaction/report.py",
            "module_discord/report.py"} <= set(SOURCE_POSTS)


@pytest.mark.parametrize("rel", sorted(SOURCE_POSTS))
def test_every_post_in_the_source_sends_the_panel_header(rel):
    missing = [c for c in SOURCE_POSTS[rel] if HEADER not in c]
    assert not missing, f"{rel}: a POST without {HEADER}:\n" + "\n---\n".join(missing)


def test_the_shell_sends_the_header_in_both_branches_of_fetch_tab():
    """``fetchTab`` builds its headers with a ternary (form body or none): both need it."""
    call = next(c for c in SOURCE_POSTS["templates/dashboard.html"] if "body:body" in c)
    assert call.count(HEADER) >= 2, call


@pytest.mark.parametrize("rel", sorted(SOURCES))
def test_no_write_goes_around_fetch(rel):
    hit = _NOT_FETCH.search(SOURCES[rel])
    assert not hit, f"{rel}: {hit.group(0)!r} cannot send {HEADER}; use fetch with the header"


# ── the rendered pages ─────────────────────────────────────────────────────────

# POST calls each page ships (JOB_JS is shared by the boards that carry a settings reset).
EXPECTED_POSTS = {"module_discord": 2, "module_uw_interaction": 1, "module_leaps": 1,
                  "module_settings": 4}


@pytest.mark.parametrize("key", sorted(BOARDS))
def test_a_rendered_board_sends_the_panel_header(key, tmp_path, monkeypatch, rawstore_tmp_db):
    html = BOARDS[key].render(tmp_path, monkeypatch)
    calls = post_calls(html)
    assert len(calls) >= EXPECTED_POSTS.get(key, 0), (key, len(calls))
    missing = [c for c in calls if HEADER not in c]
    assert not missing, f"{key}: a POST without {HEADER}:\n" + "\n---\n".join(missing)


def test_the_rendered_shell_sends_the_panel_header():
    from RevenueChaser.app import dashboard
    html = dashboard.app.test_client().get("/").get_data(as_text=True)
    calls = post_calls(html)
    assert len(calls) == 2, "fetchTab and cancelJob"
    assert all(HEADER in c for c in calls), calls
