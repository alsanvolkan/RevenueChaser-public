"""A2: priced report structures on the target Friday's quoted chain (labelled report structure, not advice)."""
from __future__ import annotations

import pytest

from RevenueChaser.module_spx_gamma.structures import build_structures, price_structure, snap_strike

BANDS = {"regime": "below_flip", "consolidation": (7600.0, 7721.8), "upside": (7721.8, 7775.0),
         "downside": (7500.0, 7600.0)}


def _row(k, t, mid, src="quote"):
    return {"strike": float(k), "type": t, "mid": mid, "price_source": src, "expiry": "2026-11-06"}


def test_snap_strike_to_the_25_grid():
    assert snap_strike(7721.8) == 7725.0 and snap_strike(7600.0) == 7600.0 and snap_strike(7612.4) == 7600.0


def test_put_debit_spread_payoff():
    chain = [_row(7575, "P", 30.0), _row(7500, "P", 10.0)]
    out = price_structure("x", [(+1, "P", 7575.0), (-1, "P", 7500.0)], chain)
    assert out["net"] == pytest.approx(-20.0)          # a debit
    assert out["max_loss"] == pytest.approx(20.0)
    assert out["max_profit"] == pytest.approx(55.0)    # 75 wide less the debit
    assert out["breakevens"] == [pytest.approx(7555.0)]
    assert out["reward_risk"] == pytest.approx(2.75)
    assert out["xsp_net"] == pytest.approx(-2.0)


def test_iron_condor_credit_and_two_breakevens():
    chain = [_row(7550, "P", 5.0), _row(7600, "P", 12.0), _row(7725, "C", 11.0), _row(7775, "C", 4.0)]
    legs = [(+1, "P", 7550.0), (-1, "P", 7600.0), (-1, "C", 7725.0), (+1, "C", 7775.0)]
    out = price_structure("ic", legs, chain)
    assert out["net"] == pytest.approx(14.0)
    assert out["max_profit"] == pytest.approx(14.0) and out["max_loss"] == pytest.approx(36.0)
    assert out["breakevens"] == [pytest.approx(7586.0), pytest.approx(7739.0)]


def test_a_missing_or_modeled_leg_leaves_it_unpriced():
    chain = [_row(7575, "P", 30.0), _row(7500, "P", 10.0, src="modeled")]
    out = price_structure("x", [(+1, "P", 7575.0), (-1, "P", 7500.0)], chain)
    assert out["net"] is None and out["missing"] == ["P 7,500"]


def test_build_structures_from_the_bands():
    strikes = range(7400, 7901, 25)
    chain = [_row(k, t, 10.0) for k in strikes for t in ("P", "C")]
    out = build_structures(BANDS, chain, "2026-11-06")
    names = [s["name"] for s in out["structures"]]
    assert names == ["Iron condor", "Put credit spread", "Long put condor", "Put debit spread", "Call debit spread"]
    by = {s["name"]: s for s in out["structures"]}
    assert [l["strike"] for l in by["Put debit spread"]["legs"]] == [7575.0, 7500.0]
    assert [l["strike"] for l in by["Iron condor"]["legs"]] == [7550.0, 7600.0, 7725.0, 7775.0]
    assert [l["strike"] for l in by["Long put condor"]["legs"]] == [7650.0, 7600.0, 7500.0, 7450.0]
    assert [l["strike"] for l in by["Call debit spread"]["legs"]] == [7750.0, 7775.0]
    assert out["expiry"] == "2026-11-06" and "not advice" in out["label"]


def test_build_structures_none_without_bands_or_chain():
    assert build_structures(None, [_row(7500, "P", 1.0)], "2026-11-06") is None
    assert build_structures(BANDS, [], "2026-11-06") is None


def test_page_shows_the_structures_and_the_hourly_column():
    from RevenueChaser.module_spx_gamma import render
    from RevenueChaser.module_spx_gamma.scenarios import scenario_bands
    from .conftest import make_snapshot
    snap = make_snapshot()
    bands = scenario_bands(snap)
    assert bands is not None
    strikes = {snap_strike(v) + d for pair in (bands["consolidation"], bands["downside"], bands["upside"])
               for v in pair for d in (-50, -25, 0, 25, 50)}
    snap["target_chain_rows"] = [_row(k, t, 5.0) for k in strikes for t in ("P", "C")]
    snap["hourly_closes"] = {"rows": [{"time": "a", "amsterdam": "Mon 16:30", "close": bands["downside"][1] - 1}],
                             "ratio": 10.0, "week_start": "2026-11-02", "source": "SPY"}
    html = render.render_html(snap)
    assert "Trade structures (2026-11-06)" in html and "Report structure, not advice" in html
    assert "Put debit spread" in html and "Hourly close" in html and "TRIGGERED Mon 16:30" in html
    md = render.render_markdown(snap)
    assert "### Trade structures" in md and "TRIGGERED Mon 16:30" in md


def test_a_backfilled_day_shows_no_structures_book_or_hourly_state():
    """Fable #4: the chains and SPY bars are live files; a past day never prices on a later session's quotes."""
    from RevenueChaser.module_spx_gamma import render
    from RevenueChaser.module_spx_gamma.scenarios import scenario_bands
    from .conftest import make_snapshot
    snap = make_snapshot()
    bands = scenario_bands(snap)
    strikes = {snap_strike(v) + d for pair in (bands["consolidation"], bands["downside"], bands["upside"])
               for v in pair for d in (-50, -25, 0, 25, 50)}
    snap["target_chain_rows"] = [_row(k, t, 5.0) for k in strikes for t in ("P", "C")]
    snap["hourly_closes"] = {"rows": [{"time": "a", "amsterdam": "Mon 16:30", "close": bands["downside"][1] - 1}],
                             "ratio": 10.0, "week_start": "2026-11-02", "source": "SPY"}
    snap["historical_live_files"] = ["target_chain.json", "option_chain.json", "spy_ohlc_30m.json"]
    html = render.render_html(snap)
    assert "Trade structures (" not in html and "<h2 class=\"sum-h\">Dealer book</h2>" not in html
    assert "TRIGGERED" not in html and "Hourly close" not in html
