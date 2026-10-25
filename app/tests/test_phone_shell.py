"""The shell on a phone and in light and dark (#184): what the page source and its functions promise.

The browser half (an exact 390 px viewport, both schemes, the drawer clicked open) is
``module_testing/e2e/test_phone_shell.py``; this half pins the pieces a regression would quietly drop.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from RevenueChaser.app import dashboard
from RevenueChaser.core import reporting_common as rc
from RevenueChaser.core.tests.board_js import DOM_STUB, function_source, inline_scripts
from RevenueChaser.core.tests.open_issues_shell import strip_open_issues_data


@pytest.fixture()
def shell():
    dashboard.app.config["TESTING"] = True
    return strip_open_issues_data(dashboard.app.test_client().get("/").get_data(as_text=True))


def _style(html: str) -> str:
    return "\n".join(re.findall(r"<style>(.*?)</style>", html, re.S))


def _media(css: str, query: str) -> str:
    """The body of every ``@media <query>`` block, concatenated."""
    out, i = [], 0
    head = "@media " + query
    while (i := css.find(head, i)) >= 0:
        j = css.index("{", i) + 1
        depth = 1
        k = j
        while depth:
            depth += {"{": 1, "}": -1}.get(css[k], 0)
            k += 1
        out.append(css[j:k - 1])
        i = k
    assert out, f"no {head} block"
    return "\n".join(out)


def test_the_palette_is_defined_once_and_comes_from_reporting_common(shell):
    assert rc.THEME_CSS in shell, "the shell's palette is reporting_common's, not a copy"
    # Every shell colour that had a value of its own is a token now: each dark value appears once, in the
    # token block, and nowhere in the shell's own rules.
    css = _style(shell)
    for name in ("bg", "input-bg", "input-border", "frame-bg", "scroll-track", "topbar"):
        value = rc.THEME_TOKENS["dark"][name]
        assert css.count(value) == rc.THEME_CSS.count(value), f"{value} (--{name}) is hard-coded in the shell"


def test_the_browser_bar_follows_the_scheme(shell):
    for scheme in ("light", "dark"):
        assert re.search(r'<meta name="theme-color" media="\(prefers-color-scheme: %s\)" content="%s">'
                         % (scheme, re.escape(rc.THEME_TOKENS[scheme]["bg"])), shell), scheme


def test_the_stored_theme_is_applied_before_the_page_draws(shell):
    head = shell[:shell.index("<body")]
    assert "rcThemeApply" in head, "the theme script must run in <head>, or a light page flashes dark first"


def test_the_shell_has_a_menu_button_for_the_drawer_and_a_theme_toggle(shell):
    toggle = re.search(r'<button\b[^>]*\bid="nav-toggle"[^>]*>', shell)
    assert toggle, "no menu button"
    assert 'aria-controls="sidebar"' in toggle.group(0) and 'aria-expanded="false"' in toggle.group(0)
    assert re.search(r'<aside\b[^>]*\bid="sidebar"', shell)
    theme = re.search(r'<button\b[^>]*\bid="theme-toggle"[^>]*>', shell)
    assert theme and "aria-label=" in theme.group(0)


def test_under_900_px_the_sidebar_is_a_drawer(shell):
    narrow = _media(_style(shell), "(max-width: 900px)")
    assert re.search(r"body\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)", narrow), "one column"
    assert re.search(r"\.sidebar\{[^}]*position:fixed[^}]*transform:translateX\(-100%\)", narrow)
    assert re.search(r"body\.nav-open \.sidebar\{[^}]*transform:none", narrow)
    assert re.search(r"\.nav-toggle\{[^}]*display:inline-flex", narrow)
    assert re.search(r"\.content\{[^}]*overflow-x:hidden", narrow), "the page never scrolls sideways"


def test_a_finger_gets_44_px_targets(shell):
    touch = _media(_style(shell), "(max-width: 900px), (pointer: coarse)")
    for rule in (r"\.nav-item\{[^}]*height:44px", r"\.nav-header\{[^}]*min-height:44px",
                 r"button\{[^}]*height:44px", r"input\[type=text\][^{]*\{[^}]*height:44px"):
        assert re.search(rule, touch), f"missing: {rule}"


def _node(script: str, tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    path = tmp_path / "shell.js"
    path.write_text(script, encoding="utf-8")
    out = subprocess.run(["node", str(path)], capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def _fit(shell, tmp_path, narrow: bool, pane: int, inner: int, tab: str = "module_sec_13f") -> str:
    script = next(s for s in inline_scripts(shell) if "function fitFrameWidth" in s)
    setup = f"""
      var FILL_WIDTH = {{'runs': true}};
      document.getElementById('content').clientWidth = {pane};
      window.matchMedia = function(q){{ return {{matches: {json.dumps(narrow)} && q.indexOf('900px') >= 0}}; }};
      var f = _el('frame');
      f.getAttribute = function(k){{ return k === 'data-tab' ? {json.dumps(tab)} : null; }};
      f.offsetWidth = 1280;
      f.contentDocument = {{documentElement: {{scrollWidth: {inner}}}, body: {{scrollWidth: {inner}}}}};
      fitFrameWidth(f);
      console.log(JSON.stringify(f.style.width || ''));
    """
    narrow_line = next((ln for ln in script.splitlines() if ln.strip().startswith("var NARROW_QUERY")), "")
    return _node(DOM_STUB + narrow_line + "\n" + function_source(script, "isNarrow")
                 + "\n" + function_source(script, "fitFrameWidth") + "\n" + setup, tmp_path)


def test_on_a_phone_a_wide_board_is_never_grown_past_the_pane(shell, tmp_path):
    """The frame grew to the board's natural width (Earnings ~2600 px), so the page scrolled sideways."""
    assert _fit(shell, tmp_path, narrow=True, pane=390, inner=2600) == "390px"


def test_on_a_desktop_a_wide_board_still_grows(shell, tmp_path):
    assert _fit(shell, tmp_path, narrow=False, pane=1040, inner=2600) == "2640px"


def test_a_fill_width_board_tracks_the_pane_on_a_phone(shell, tmp_path):
    assert _fit(shell, tmp_path, narrow=True, pane=390, inner=900, tab="runs") == "390px"


def test_the_theme_toggle_cycles_auto_light_dark(shell, tmp_path):
    script = next(s for s in inline_scripts(shell) if "function themeNext" in s)
    tail = "console.log(JSON.stringify(['auto', 'light', 'dark', 'bogus'].map(themeNext)));"
    assert _node(DOM_STUB + function_source(script, "themeNext") + "\n" + tail, tmp_path) == \
        ["light", "dark", "auto", "light"]


def test_the_choice_reaches_the_board_frames(shell, tmp_path):
    """Boards are same-origin iframes: the shell marks each loaded one, so a board on the shared tokens
    follows the toggle without reading storage itself."""
    script = next(s for s in inline_scripts(shell) if "function themeFrames" in s)
    tail = """
      var a = {}, b = {};
      function doc(store){ return {documentElement: {setAttribute: function(k, v){ store[k] = v; },
                                                     removeAttribute: function(k){ delete store[k]; }}}; }
      var fs = [{contentDocument: doc(a)}, {contentDocument: doc(b)},
                {get contentDocument(){ throw new Error('cross-origin'); }}];
      frames = function(){ return fs; };
      themeFrames('light'); var one = [a['data-theme'], b['data-theme']];
      themeFrames('auto'); var two = [a['data-theme'] || null, b['data-theme'] || null];
      console.log(JSON.stringify([one, two]));
    """
    assert _node(DOM_STUB + "var frames;\n" + function_source(script, "themeFrames") + "\n" + tail, tmp_path) == \
        [["light", "light"], [None, None]]
