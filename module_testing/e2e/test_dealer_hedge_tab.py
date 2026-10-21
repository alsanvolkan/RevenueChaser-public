"""Dealer Hedge (demo) tab, browser half (#376): the page's Black-Scholes book against a Python reference.

The page renders to a file; headless Chrome loads it with ``--dump-dom`` and a script appended to the page
drives it like a reader would: pick the short-straddle preset, set spot 104 / IV 24 % / 3 days, read the tiles;
then open the "OPEX pinning" scenario and read the sliders. It writes what it saw into a ``data-seen``
attribute on ``<html>`` and the test reads that from the dumped DOM. The host resolver blocks every host but
localhost, so a request the page tried to make would show up as a failed load, not a pass.

Chrome is found as in ``test_board_html_browser`` ($RC_CHROME, google-chrome, chromium...). Without one the test
skips locally and fails under CI.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from RevenueChaser.app import dealer_hedge

_CANDIDATES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


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
        msg = "no Chrome/Chromium for the Dealer Hedge tab: set RC_CHROME or install google-chrome / chromium"
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail(msg)
        pytest.skip(msg)
    return path


# ── the reference: Black-Scholes, r = 0, q = 0, 1 contract = 100 shares ───────

def _n(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _pdf(x: float) -> float:
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def _book(legs, spot: float, iv_pct: float, days: float) -> dict:
    sig, t = iv_pct / 100, days / 365
    out = dict(delta=0.0, gamma=0.0, vega=0.0, vanna=0.0, charm=0.0)
    for strike, is_call, qty in legs:
        d1 = (math.log(spot / strike) + 0.5 * sig * sig * t) / (sig * math.sqrt(t))
        d2 = d1 - sig * math.sqrt(t)
        n = qty * 100
        out["delta"] += (_n(d1) if is_call else _n(d1) - 1) * n
        out["gamma"] += _pdf(d1) / (spot * sig * math.sqrt(t)) * n
        out["vega"] += spot * _pdf(d1) * math.sqrt(t) * n / 100
        out["vanna"] += -_pdf(d1) * d2 / sig * n / 100
        out["charm"] += _pdf(d1) * d2 / (2 * t) * n / 365
    return out


SHORT_STRADDLE = [(100, True, -100), (100, False, -100)]
INDEX_BOOK = [(95, False, -100), (105, True, 100)]

# What a reader does, then what the script writes into <html data-seen=...>.
_DRIVE = r"""<script>
window.addEventListener("load", function(){ setTimeout(function(){
  var seen = {};
  function set(id, v){ var el = document.getElementById(id); el.value = v; el.dispatchEvent(new Event("input", {bubbles: true})); }
  function txt(id){ return document.getElementById(id).textContent; }
  seen.start = {trade: txt("trade"), hedge: txt("hedge"), delta: txt("bookDelta")};
  document.querySelector('#presets button[data-p="shortgamma"]').click();
  set("spot", 104); set("iv", 24); set("dte", 3);
  seen.straddle = {trade: txt("trade"), delta: txt("bookDelta"), gamma: txt("bookGamma"), vanna: txt("bookVanna"),
                   charm: txt("bookCharm"), vega: txt("bookVega"), hedge: txt("hedge")};
  var pin = document.querySelector('#scen details[data-s="pin"]');
  pin.open = true; pin.dispatchEvent(new Event("toggle"));
  setTimeout(function(){
    seen.pin = {spot: document.getElementById("spot").value, iv: document.getElementById("iv").value,
                dte: document.getElementById("dte").value,
                pressed: Array.prototype.map.call(document.querySelectorAll("#presets button"),
                  function(b){ return b.getAttribute("aria-pressed"); }).join(",")};
    document.getElementById("reset").click();
    seen.reset = {spot: document.getElementById("spot").value, trade: txt("trade")};
    document.documentElement.setAttribute("data-seen", JSON.stringify(seen));
  }, 50);
}, 50); });
</script>"""


@pytest.fixture(scope="module")
def seen(chrome, tmp_path_factory) -> dict:
    tmp = tmp_path_factory.mktemp("dealer_hedge")
    html = dealer_hedge.render_page()
    i = html.rfind("</body>")  # not .lower(): the page holds characters whose lowercase has another length
    page = tmp / "dealer_hedge.html"
    page.write_text(html[:i] + _DRIVE + html[i:], encoding="utf-8")
    out = subprocess.run(
        [chrome, "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--disable-extensions",
         f"--user-data-dir={tmp / 'profile'}", "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost",
         "--virtual-time-budget=4000", "--dump-dom", page.as_uri()],
        capture_output=True, text=True, encoding="utf-8", timeout=90)
    m = re.search(r'<html[^>]*\sdata-seen="([^"]*)"', out.stdout, re.I)
    assert m, f"the page never finished its script: {out.stderr[-1500:]}"
    return json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&"))


def _num(text: str) -> float:
    """The page shows signed whole numbers with tr-TR thousands dots: '+1.234' -> 1234."""
    return float(text.replace("+", "").replace(".", "").replace("−", "-").replace(",", "."))


def test_the_book_starts_delta_neutral(seen):
    assert _num(seen["start"]["trade"]) == 0
    base = _book(INDEX_BOOK, 100, 20, 20)
    assert abs(_num(seen["start"]["delta"]) - base["delta"]) <= 1
    assert abs(_num(seen["start"]["hedge"]) + base["delta"]) <= 1, "the hedge is minus the book delta"


def test_short_straddle_matches_the_reference(seen):
    """Spot 104, IV 24 %, 3 days on the short straddle: every tile within the page's integer rounding."""
    ref = _book(SHORT_STRADDLE, 104, 24, 3)
    base = _book(SHORT_STRADDLE, 100, 20, 20)
    got = seen["straddle"]
    assert abs(_num(got["delta"]) - ref["delta"]) <= 1
    assert abs(_num(got["gamma"]) - ref["gamma"]) <= 1
    assert abs(_num(got["vanna"]) - ref["vanna"]) <= 1
    assert abs(_num(got["charm"]) - ref["charm"]) <= 1
    assert abs(_num(got["vega"]) - ref["vega"]) <= 1
    # extra trade now = -delta(now) - hedge(base) = -delta(now) + delta(base)
    assert abs(_num(got["trade"]) - (-ref["delta"] + base["delta"])) <= 1
    assert abs(_num(got["hedge"]) + base["delta"]) <= 1


def test_the_short_gamma_squeeze_makes_the_dealer_buy(seen):
    """A short straddle after a +4 move with 3 days left: delta is negative, so the dealer must buy."""
    assert _num(seen["straddle"]["trade"]) > 0 and _num(seen["straddle"]["gamma"]) < 0


def test_opening_the_opex_pin_scenario_moves_the_sliders(seen):
    assert (seen["pin"]["spot"], seen["pin"]["iv"], seen["pin"]["dte"]) == ("101.5", "18", "1")
    assert seen["pin"]["pressed"] == "false,false,false,true"


def test_reset_returns_to_the_starting_point(seen):
    assert seen["reset"]["spot"] == "100"
