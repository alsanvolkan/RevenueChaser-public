"""#166: a leaderboard built from a partial pull says so, and never replaces a whole one.

``compute_money_flow`` used to swallow every per-ETF failure: a board ranked from 30 of 34 ETFs looked
exactly like a full one. Now the result carries ``partial`` and ``missing`` (only when something is missing:
a healthy result is byte-identical to before), and the board shows one amber note.

Since M-ROT-3 (#374) the closes and volumes are D01 (``core.prices.daily_bars``), not a UW candle: an ETF is
missing when D01 holds no bar of it in the window, and no provider status is involved any more.
"""
from __future__ import annotations

import re
from datetime import date

import pytest

from RevenueChaser.module_sector_rotation import money_flow as mf
from RevenueChaser.module_sector_rotation import report
from RevenueChaser.module_sector_rotation.tests.bars_seed import ny, pin_clock, seed
from RevenueChaser.module_sector_rotation.universe import ROTATION_ETFS

QUERIED = [t for t, i in ROTATION_ETFS.items() if i.get("group") not in ("region", "bond", "yield", "commodity")]
SESSION = date(2026, 8, 14)


def _rising(ticker: str) -> bool:
    """A fixed direction per ticker. ``hash(str)`` changes with PYTHONHASHSEED, so the set of rising ETFs (and
    the counts the tests assert) changed from run to run."""
    return sum(map(ord, ticker)) % 2 == 0


class _NoUW:
    def __getattr__(self, name):
        raise AssertionError(f"Money Flow must not call uw.{name}")


@pytest.fixture
def d01(rawstore_tmp_db, monkeypatch):
    pin_clock(monkeypatch, ny(2026, 8, 14, 18))                 # Friday after the close: the session is complete

    def _seed(skip=()):
        for t in QUERIED:
            if t not in skip:
                seed(t, SESSION, start=date(2026, 6, 1),
                     closes=(lambda i: 100.0 + i) if _rising(t) else (lambda i: 200.0 - i))
    return _seed


def test_a_healthy_result_has_exactly_the_old_keys(d01):
    d01()
    res = mf.compute_money_flow(_NoUW(), days=30, window=21, top_n=10)
    assert set(res) == {"inflows", "outflows", "generated_at", "as_of"}
    assert len(res["inflows"]) == 10 and res["as_of"] == SESSION.isoformat()


def test_an_etf_with_no_stored_bars_makes_the_result_partial_and_is_named(d01):
    d01(skip={"XLK", "SMH"})
    res = mf.compute_money_flow(_NoUW(), days=30, window=21, top_n=10)
    assert res["partial"] is True and res["missing"] == [t for t in QUERIED if t in {"XLK", "SMH"}]
    ranked = {r["ticker"] for r in res["inflows"] + res["outflows"]}
    assert "XLK" not in ranked and res["inflows"]            # the others are still ranked


def test_only_the_last_days_sessions_up_to_the_session_are_read(d01):
    d01()
    seed("XLK", date(2026, 8, 21), start=date(2026, 8, 17), closes=lambda i: 1.0)   # after the session: not read
    rows = mf.bar_rows(["XLK"], SESSION, days=30)
    assert len(rows) == 30 and rows[-1]["date"] == SESSION.isoformat()
    assert all(r["close"] != 1.0 for r in rows)


def test_nothing_stored_is_an_empty_partial_result(d01):
    res = mf.compute_money_flow(_NoUW(), days=30, window=21, top_n=10)
    assert res["inflows"] == [] and res["outflows"] == [] and res["as_of"] is None
    assert res["partial"] is True and res["missing"] == QUERIED


# ── the board note ────────────────────────────────────────────────────────────────────────

def _note_js() -> str:
    m = re.search(r"// mf-note begin(.*?)// mf-note end", _page_js(), re.S)
    assert m, "the partial-result note code is not in the page"
    return m.group(1)


def _page_js() -> str:
    src = open(report.__file__, encoding="utf-8").read()
    a = src.index("function renderMoneyFlow()")
    return src[a:src.index("document.addEventListener('DOMContentLoaded', function(){ renderMoneyFlow(); });", a)]


def test_the_note_is_built_from_dom_nodes_not_html():
    js = _note_js()
    assert "textContent" in js and "createElement" in js
    assert "innerHTML" not in js and "esc(" not in js and ".replace(" not in js


def test_the_note_names_the_missing_etfs_and_is_amber():
    js = _note_js()
    assert "d.partial" in js and "d.missing" in js and "#F59E0B" in js
