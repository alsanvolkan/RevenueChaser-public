"""Board HTML guard: every board renders a hostile fixture inertly (ADR-X1 §5, #144).

Static half of the guard (the browser half is
``module_testing/e2e/test_board_html_browser.py``). For each board in
``core/tests/board_fixtures.py``:

- (a) the payload's markup appears only escaped, never raw;
- (b) every inline ``<script>`` is balanced and holds no ``<!--`` or ``<script``
  (both are impossible when data goes through ``html_safe.script_json``);
- (c) the registry covers every dashboard board, so a new board cannot skip it.

A board with a known open item is a strict xfail: fixing it turns the test
XPASS, which fails until the note is removed.
"""
from __future__ import annotations

import inspect
import re

import pytest

from RevenueChaser.core.tests.board_fixtures import BOARDS, RAW_MARK
from RevenueChaser.core.tests.board_js import inline_scripts


def _required_boards() -> set[str]:
    from RevenueChaser.app import dashboard
    tabs = {t["key"] for t in dashboard.TABS if t["type"] == "tab"}
    files = set(re.findall(r'"(module_\w+)":', inspect.getsource(dashboard._module_file)))
    assert files, "could not read _module_file's keys"
    return tabs | files


def test_registry_covers_every_board():
    missing = _required_boards() - set(BOARDS)
    assert not missing, f"add a hostile fixture to core/tests/board_fixtures.py for {sorted(missing)}"


def assert_inert(html: str) -> None:
    raw = [m.group(1) for m in re.finditer(re.escape(RAW_MARK) + r"'(\w+)'\)", html)]
    assert not raw, f"payload unescaped (markup, or script JSON without script_json) for: {sorted(set(raw))}"
    low = html.lower()
    assert low.count("<script") == low.count("</script"), "unbalanced <script> (a breakout)"
    for i, body in enumerate(inline_scripts(html)):
        assert "<!--" not in body, f"inline script {i} holds <!--"
        assert "<script" not in body.lower(), f"inline script {i} holds <script"
    assert ("&lt;img src=x onerror=__x(" in html or "\\u003cimg src=x onerror=__x(" in html), (
        "the fixture's payload never reached the page")


@pytest.mark.parametrize("key", sorted(BOARDS))
def test_board_renders_the_hostile_fixture_inertly(key, request, tmp_path, monkeypatch,
                                                   rawstore_tmp_db):
    board = BOARDS[key]
    if board.open_item:
        request.applymarker(pytest.mark.xfail(reason=board.open_item, strict=True))
    assert_inert(board.render(tmp_path, monkeypatch))


def test_settings_fixture_renders_automation_buttons_with_the_hostile_name():
    """N1: the hostile automation row has a live state, so its buttons (and their
    data-name attribute) go through the renderer in the browser guard."""
    from RevenueChaser.core.tests.board_fixtures import BOARDS
    import inspect
    src = inspect.getsource(BOARDS["module_settings"].render)
    assert 'state"] = "enabled"' in src
