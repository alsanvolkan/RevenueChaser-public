"""Board HTML guard, browser half: no hostile fixture runs script (ADR-X1 §5, #144).

Each board in ``core/tests/board_fixtures.py`` renders its hostile fixture to a
file; headless Chrome loads it with ``--dump-dom``. A hook prepended to the page
defines ``window.__x(tag)``, which appends the tag to a ``data-xss`` attribute on
``<html>``; the dumped DOM must not carry one. This is the only check that sees
what client JS puts into ``innerHTML`` without a hand-written test per board.

No pip dependency: Chrome is found as ``$RC_CHROME``, ``google-chrome``,
``google-chrome-stable``, ``chromium`` or ``chromium-browser``. GitHub's
``ubuntu-latest`` image ships ``google-chrome``. Without a browser the test skips
locally and **fails under CI** (``CI=true``): a silent skip would leave the guard
unenforced.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from RevenueChaser.core.tests.board_fixtures import BOARDS, no_comment_opener

_CANDIDATES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")

# Runs before any board script: records hits, and keeps the page off the network.
_HOOK = ("<script>window.__x=function(t){var d=document.documentElement;"
         "d.setAttribute('data-xss',(d.getAttribute('data-xss')||'')+t+' ');};"
         "window.alert=function(){};window.confirm=function(){return false;};"
         "window.fetch=function(){return new Promise(function(){});};</script>")

# Runs after load: the generic interactions that reach click/hover-only sinks
# (gate modals, chips, selects, details), skipping anything that navigates.
_POKE = r"""<script>
window.addEventListener("load", function(){ setTimeout(function(){
  document.querySelectorAll("details").forEach(function(d){ d.open = true; });
  document.querySelectorAll(".chip, [data-t], [data-ticker]").forEach(function(el){
    el.dispatchEvent(new MouseEvent("mouseover", {bubbles: true}));
    el.dispatchEvent(new FocusEvent("focusin", {bubbles: true}));
  });
  document.querySelectorAll("select").forEach(function(s){
    for(var i = 0; i < s.options.length && i < 4; i++){
      s.selectedIndex = i; s.dispatchEvent(new Event("change", {bubbles: true}));
    }
  });
  var skip = /location|href|reload|submit|run\(|fetch|rcOpenTab|window\.open/i;
  Array.prototype.slice.call(document.querySelectorAll(
      "[onclick], .gate, .card, .tab, tr[data-ticker], li[role=option]"), 0, 300)
    .forEach(function(el){
      if(el.tagName === "A" || skip.test(el.getAttribute("onclick") || "")) return;
      try { el.click(); } catch(e) {}
    });
  document.querySelectorAll("[oninput]").forEach(function(el){
    try { el.dispatchEvent(new Event("input", {bubbles: true})); } catch(e) {}
  });
}, 0); });
</script>"""


def _chrome() -> str | None:
    override = os.environ.get("RC_CHROME")
    if override:
        return override if Path(override).exists() or shutil.which(override) else None
    for name in _CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


@pytest.fixture(scope="module")
def chrome() -> str:
    path = _chrome()
    if path is None:
        msg = ("no Chrome/Chromium for the board HTML guard: set RC_CHROME or install "
               "google-chrome / chromium")
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail(msg + " (a skip on CI would leave the guard unenforced)")
        pytest.skip(msg)
    return path


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    """Renderers that read the store (Rotation) get an empty temp one."""
    from RevenueChaser.core import rawstore
    monkeypatch.setattr(rawstore, "DB_PATH", tmp_path / "raw.duckdb")
    rawstore.ensure_all_tables()


def _with_hooks(html: str) -> str:
    html = re.sub(r"^(\ufeff?\s*<!doctype[^>]*>)?", lambda m: (m.group(1) or "") + _HOOK,
                  html, count=1, flags=re.I)
    i = html.lower().rfind("</body>")
    return html[:i] + _POKE + html[i:] if i >= 0 else html + _POKE


def _hits(chrome: str, page: Path, profile: Path) -> list[str]:
    out = subprocess.run(
        [chrome, "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run",
         "--disable-extensions", f"--user-data-dir={profile}",
         "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost",
         "--virtual-time-budget=3000", "--dump-dom", page.as_uri()],
        capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert "<html" in out.stdout.lower(), f"Chrome dumped no DOM: {out.stderr[-1500:]}"
    m = re.search(r'<html[^>]*\sdata-xss="([^"]*)"', out.stdout, re.I)
    return sorted(set(m.group(1).split())) if m else []


@pytest.mark.parametrize("key", sorted(BOARDS))
def test_hostile_fixture_runs_no_script(key, chrome, request, tmp_path, monkeypatch, tmp_store):
    board = BOARDS[key]
    if board.executes:
        request.applymarker(pytest.mark.xfail(reason=board.executes, strict=True))
    page = tmp_path / f"{key}.html"
    with no_comment_opener():
        html = board.render(tmp_path, monkeypatch)
    page.write_text(_with_hooks(html), encoding="utf-8")
    hits = _hits(chrome, page, tmp_path / "profile")
    assert not hits, f"{key}: hostile payload executed for {hits}"
