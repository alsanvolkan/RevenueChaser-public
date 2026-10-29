"""The 13F block names its quarter, and "QoQ" compares two adjacent quarters only (AG review SF-3, PR 6).

The card's institutional block showed holders, flows and "Shares QoQ" with no period; when the stored totals skip a
quarter (a 13F quarter not ingested), "QoQ" compared quarters six months apart. ``_institutional`` now carries the
quarter it read and the one it compared, and gives no QoQ change when they are not adjacent.
"""
from __future__ import annotations

from RevenueChaser.module_asymmetric_growth import engine, report
from RevenueChaser.module_sec_13f.tests.ownership_store import _store, market  # noqa: F401 - the fixture


def _p(shares: float) -> dict:
    return {"holders": 10, "new_holders": 2, "exited_holders": 1, "buy_flow": 5.0, "sell_flow": 1.0, "shares": shares}


def test_the_block_names_its_quarter_and_compares_the_one_before():
    out = engine._institutional("AAA", {"AAA": {"2025-12-31": _p(100.0), "2026-03-31": _p(110.0)}})
    assert out["inst_quarter"] == "2026 Q1" and out["inst_prev_quarter"] == "2025 Q4"
    assert abs(out["inst_shares_delta_pct"] - 0.10) < 1e-12


def test_a_skipped_quarter_gives_no_qoq_change():
    out = engine._institutional("AAA", {"AAA": {"2025-09-30": _p(100.0), "2026-03-31": _p(150.0)}})
    assert out["inst_quarter"] == "2026 Q1" and out["inst_prev_quarter"] is None
    assert out["inst_shares_delta_pct"] is None


def test_the_year_boundary_is_adjacent():
    out = engine._institutional("AAA", {"AAA": {"2025-09-30": _p(100.0), "2025-12-31": _p(90.0)}})
    assert out["inst_prev_quarter"] == "2025 Q3" and abs(out["inst_shares_delta_pct"] + 0.10) < 1e-12


def test_the_quarters_ride_with_the_stored_card_so_a_refresh_shows_them(rawstore_tmp_db):
    from RevenueChaser.core import ticker_store
    card = {"ticker": "AAA", "period_ends": {"revenue": [None, None, None]}}
    inst = engine._institutional("AAA", {"AAA": {"2025-12-31": _p(100.0), "2026-03-31": _p(110.0)}})
    card["period_ends"]["inst_13f"] = [inst.pop("inst_quarter"), inst.pop("inst_prev_quarter")]
    card.update(inst)
    ticker_store.write_from_cards([card], asof="2026-10-28")
    assert ticker_store.all_cards()[0]["period_ends"]["inst_13f"] == ["2026 Q1", "2025 Q4"]
    src = report.__loader__.get_source(report.__name__)
    assert ".inst_13f" in src and "(13F " in src and "(vs " in src


# ── G9 (build plan PR 17): the block reads the ownership store, and a quarter still filing is not its quarter ──

def test_a_quarter_still_filing_is_not_the_blocks_quarter():
    filing = dict(_p(50.0), complete=False)       # late filers still arrive: 50 would read as selling
    out = engine._institutional("AAA", {"AAA": {"2025-12-31": _p(100.0), "2026-03-31": _p(110.0),
                                                "2026-06-30": filing}})
    assert out["inst_quarter"] == "2026 Q1" and out["inst_prev_quarter"] == "2025 Q4"
    assert out["inst_shares"] == 110.0 and abs(out["inst_shares_delta_pct"] - 0.10) < 1e-12


def test_only_quarters_still_filing_is_no_block():
    out = engine._institutional("AAA", {"AAA": {"2026-06-30": dict(_p(50.0), complete=False)}})
    assert out["inst_quarter"] is None and out["inst_shares"] is None


def test_the_scan_reads_the_13f_block_from_the_ownership_store(rawstore_tmp_db, market, monkeypatch):
    import RevenueChaser.module_sec_13f.engine as s13
    from RevenueChaser.module_asymmetric_growth.tests.test_ag_dated_momentum_380 import seed_momentum
    from RevenueChaser.module_asymmetric_growth.tests.test_ag_honest_signals_380 import RUN_DAY, _dated
    from RevenueChaser.module_asymmetric_growth.tests.test_ag_info_only_150 import _ANNUAL, _QUOTE, _edgar
    from RevenueChaser.core import rawstore

    def old(*_a, **_k):
        raise AssertionError("the 13F block must not read the old sec13f_totals")
    monkeypatch.setattr(s13, "load_totals", old)
    _store(market)                     # AAA: Q1 100, Q2 200 complete, Q3 50 still filing
    rawstore.write_rows("edgar_financials", [_edgar("AAA", _dated(_ANNUAL, 2025))])
    seed_momentum(["AAA"])
    monkeypatch.setattr(engine, "fetch_universe_stats",
                        lambda *a, **k: ([{"ticker": "AAA", "quote": dict(_QUOTE), "market_cap": 5e9}], {}))
    monkeypatch.setattr(engine._spine, "spine_gics", lambda *a, **k: {})
    monkeypatch.setattr(engine._spine, "excluded_tickers", lambda: set())
    monkeypatch.setattr(engine, "_run_day", lambda: RUN_DAY)
    (card,) = engine.run_asymmetric_growth_hunter(None, max_workers=1, no_fetch=True)["cards"]
    assert card["inst_shares"] == 200.0 and abs(card["inst_shares_delta_pct"] - 1.0) < 1e-12
    assert card["period_ends"]["inst_13f"] == ["2025 Q2", "2025 Q1"]
