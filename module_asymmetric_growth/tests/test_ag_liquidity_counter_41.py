"""#41 (#66 checkbox 5): the AG run summary says how many scored names are thin and how many have no EDGAR numbers.

Both figures are reporting only. AG has no liquidity floor (a thin name still gets a card), so the thin count is a
funnel **note**, never a drop: the drops must still add up to ``universe - scored``. The unreadable-SEC count is the
scored card whose ``source`` is not EDGAR — the Yahoo-statements fallback the card already carries; a name that got
no card for a filings reason is a drop (``no_annual`` / ``fetch_failed``) and is never counted here too.

The two figures OVERLAP by design: they answer independent questions — is this name too thin to read, and were
its financials read from EDGAR — so a name that is both thin and non-EDGAR is counted in both. "Counted once"
belongs to drops, which must sum to the universe; ``core.funnel.validate`` sums only ``kind == "drop"`` (#106
review).

Synthetic store, pinned session, no network.
"""
from __future__ import annotations

import copy

from RevenueChaser.core import funnel, rawstore
from RevenueChaser.core.providers import cache as cache_manager
from RevenueChaser.module_asymmetric_growth import engine
from RevenueChaser.module_asymmetric_growth.report import render_asymmetric_growth_hunter_html
from RevenueChaser.module_asymmetric_growth.tests.test_ag_dated_momentum_380 import (bar, sessions, store_bars,
                                                                            universe)
from RevenueChaser.module_asymmetric_growth.tests.test_ag_info_only_150 import _QUOTE, _YF_ANNUAL, _edgar

# The bars close at 102 and their 63-session mean volume is 1.095x the base volume, so price * vol_90d is
# ~111.7 * base. 100 a day is thin (11,171); 10,000 a day is not (1.1M).
THIN_VOLUME = 100.0
LIQUID_VOLUME = 10_000.0


def _seed_bars(volumes: dict[str, float]) -> None:
    """Stored bars that pass all three momentum rules, at the chosen base volume per ticker (plus SPY, the anchor)."""
    days = sessions()
    rows = []
    for ticker, volume in {**volumes, "SPY": LIQUID_VOLUME}.items():
        for k, day in enumerate(days):
            tail = k >= len(days) - 10
            rows.append(bar(ticker, day, close=102.0 if tail else 100.0,
                            volume=volume * 1.6 if tail else volume))
    store_bars(rows)


def _edgar_rows(*tickers) -> None:
    rawstore.write_rows("edgar_financials", [_edgar(t) for t in tickers])


def _yahoo_rows(*tickers) -> None:
    """A stored full Yahoo payload: annual figures, so the name scores, with no EDGAR record behind it."""
    for t in tickers:
        cache_manager.save(t, {"info": {"country": "United States", "exchange": "NMS", "longName": f"{t} Inc"},
                               "annual": copy.deepcopy(_YF_ANNUAL), "market_cap": 5e9, "source": "yfinance"})


def _scan(monkeypatch, tickers):
    rows = [{"ticker": t, "quote": dict(_QUOTE), "market_cap": 5e9} for t in tickers]
    universe(monkeypatch, rows)                 # the universe, 13F totals and the spine are stubbed
    return engine.run_asymmetric_growth_hunter(None, max_workers=1, no_fetch=True)


def _steps(result):
    return {s["key"]: s for s in result["funnel_steps"]}


# ── A. low liquidity: a note about scored names, never a drop ────────────────────────────────────────────────────

def test_three_thin_scored_names_are_counted(rawstore_tmp_db, monkeypatch):
    _edgar_rows("THIN1", "THIN2", "THIN3", "FAT1")
    _seed_bars({"THIN1": THIN_VOLUME, "THIN2": THIN_VOLUME, "THIN3": THIN_VOLUME, "FAT1": LIQUID_VOLUME})

    result = _scan(monkeypatch, ["THIN1", "THIN2", "THIN3", "FAT1"])

    assert result["summary"]["low_liquidity"] == 3


# ── B. unreadable SEC: the scored card whose financials are not EDGAR's ──────────────────────────────────────────

def test_two_scored_names_without_edgar_financials_are_counted(rawstore_tmp_db, monkeypatch):
    _edgar_rows("SEC1")
    _yahoo_rows("YF1", "YF2")
    _seed_bars({"SEC1": LIQUID_VOLUME, "YF1": LIQUID_VOLUME, "YF2": LIQUID_VOLUME})

    result = _scan(monkeypatch, ["SEC1", "YF1", "YF2"])

    assert {c["ticker"]: c["source"] for c in result["cards"]} == {
        "SEC1": "sec_edgar", "YF1": "yfinance", "YF2": "yfinance"}
    assert result["summary"]["unreadable_sec"] == 2
    assert result["summary"]["low_liquidity"] == 0


# ── C. counted exactly once: low liquidity first, then unreadable SEC ───────────────────────────────────────────

def test_a_name_that_is_both_thin_and_non_edgar_is_counted_in_both(rawstore_tmp_db, monkeypatch):
    """#106 review: each note answers its own question, so BOTH is in both. Excluding it from ``unreadable_sec``
    would make that figure disagree with its own label — "the financials are Yahoo statements" — and would give
    a different answer on a Refresh, where no liquidity basis was read."""
    _yahoo_rows("BOTH")
    _seed_bars({"BOTH": THIN_VOLUME})

    result = _scan(monkeypatch, ["BOTH"])

    assert result["summary"]["low_liquidity"] == 1
    assert result["summary"]["unreadable_sec"] == 1


def test_the_funnel_still_adds_up_with_both_figures(rawstore_tmp_db, monkeypatch):
    """THIN1 is thin; YF1 is non-EDGAR; BOTH is thin AND non-EDGAR (counted in both); GHOST has no data;
    FORN is a foreign filer."""
    rawstore.write_rows("edgar_financials", [_edgar("THIN1"), _edgar("FORN", is_us=False)])
    _yahoo_rows("YF1", "BOTH")
    _seed_bars({"THIN1": THIN_VOLUME, "YF1": LIQUID_VOLUME, "BOTH": THIN_VOLUME, "FORN": LIQUID_VOLUME})

    result = _scan(monkeypatch, ["THIN1", "YF1", "BOTH", "GHOST", "FORN"])
    steps = _steps(result)

    assert funnel.validate(result["funnel_steps"]) == []
    assert steps["universe"]["count"] == 5 and steps["scored"]["count"] == 3
    assert sum(s["count"] for s in result["funnel_steps"] if s["kind"] == "drop") == 2
    # the two figures are notes: they do not enter the drop arithmetic
    assert steps["low_liquidity"]["kind"] == "note" and steps["low_liquidity"]["count"] == 2
    assert steps["unreadable_sec"]["kind"] == "note" and steps["unreadable_sec"]["count"] == 2   # YF1 and BOTH
    # a note counts a subset of the scored names; the two may overlap, so neither sum means anything beyond this
    for key in ("low_liquidity", "unreadable_sec"):
        assert steps[key]["count"] <= steps["scored"]["count"]
    # the names that got no card for a filings reason stay drops, and are not counted again above
    assert steps["no_annual"]["count"] == 1 and steps["no_annual"]["tickers"] == ["GHOST"]
    assert steps["foreign"]["count"] == 1 and "fetch_failed" not in steps


def test_both_figures_are_in_the_funnel_panel(rawstore_tmp_db, monkeypatch):
    _edgar_rows("THIN1")
    _yahoo_rows("YF1", "BOTH")
    _seed_bars({"THIN1": THIN_VOLUME, "YF1": LIQUID_VOLUME, "BOTH": THIN_VOLUME})

    result = _scan(monkeypatch, ["THIN1", "YF1", "BOTH"])
    panel = funnel.funnel_panel_html(result["funnel_steps"])
    page = render_asymmetric_growth_hunter_html(result)

    assert funnel.validate(result["funnel_steps"]) == []
    for html in (panel, page):
        # A note carries no minus sign: these names are IN the result, so a minus would read as a removal.
        assert "Scored, under $250,000 average daily dollar volume</td><td>2</td>" in html
        assert "Scored, but the financials are Yahoo statements, not EDGAR filings</td><td>2</td>" in html


# ── the zero case and the untested case ─────────────────────────────────────────────────────────────────────────

def test_nothing_qualifies_reports_zero_for_both_keys(rawstore_tmp_db, monkeypatch):
    _edgar_rows("CLEAN")
    _seed_bars({"CLEAN": LIQUID_VOLUME})

    result = _scan(monkeypatch, ["CLEAN"])
    summary = result["summary"]

    assert "low_liquidity" in summary and summary["low_liquidity"] == 0
    assert "unreadable_sec" in summary and summary["unreadable_sec"] == 0
    # a zero is not a funnel row either: nothing to report about names that are all fine
    assert "low_liquidity" not in _steps(result) and "unreadable_sec" not in _steps(result)


def test_a_name_with_no_90_day_volume_is_untested_not_thin(rawstore_tmp_db, monkeypatch):
    _edgar_rows("NBARS")                        # no stored bars at all: the price falls back to the quote

    result = _scan(monkeypatch, ["NBARS"])

    assert [c["ticker"] for c in result["cards"]] == ["NBARS"]
    assert result["summary"]["low_liquidity"] == 0


# ── no network: a store-only scan reads the store and nothing else ──────────────────────────────────────────────

def test_the_counters_need_no_provider_call(rawstore_tmp_db, monkeypatch):
    _edgar_rows("THIN1")
    _yahoo_rows("YF1")
    _seed_bars({"THIN1": THIN_VOLUME, "YF1": LIQUID_VOLUME})

    def offline(*a, **k):
        raise AssertionError("the scan went online: a store-only run must not call a provider")
    monkeypatch.setattr(engine.yfinance_client, "get_info", offline)
    monkeypatch.setattr(engine.yfinance_client, "get_financials", offline)
    monkeypatch.setattr(engine.sec_edgar, "_cik_map", offline)

    result = _scan(monkeypatch, ["THIN1", "YF1"])

    assert len(result["cards"]) == 2
    assert result["summary"]["low_liquidity"] == 1 and result["summary"]["unreadable_sec"] == 1


def test_a_summary_counted_from_cards_alone_reports_unknown_liquidity_not_zero():
    """A Refresh whose stored header does not match the table counts the summary from the cards, which do not carry
    ``vol_90d``. ``low_liquidity`` is then None (unknown) and never 0: printing 0 for a figure nobody measured
    would be a claim about data that was never read. ``unreadable_sec`` is a card property, so it still counts."""
    cards = [{"ticker": "AAA", "sector": "Technology", "total_points": 5, "tier": 2, "themes": [],
              "tech_breakthrough": False, "flags": [], "source": "yfinance"},
             {"ticker": "BBB", "sector": "Technology", "total_points": 5, "tier": 2, "themes": [],
              "tech_breakthrough": False, "flags": [], "source": "sec_edgar"}]

    summary = engine._summary(cards, None, {})

    assert summary["low_liquidity"] is None
    assert summary["unreadable_sec"] == 1
    # the funnel leaves an unknown note out rather than showing it as zero
    steps = engine._funnel_steps(2, 0, engine._Drops(), {}, 2,
                                 summary["low_liquidity"], summary["unreadable_sec"])
    assert [st["key"] for st in steps if st["kind"] == "note"] == ["unreadable_sec"]
    assert funnel.validate(steps) == []
