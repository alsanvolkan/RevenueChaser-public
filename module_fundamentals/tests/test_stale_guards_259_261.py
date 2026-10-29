"""#259/#261 on the Fundamentals path: the stale info marker is stripped and both guards are counted."""
from __future__ import annotations

import pandas as pd

from RevenueChaser.module_fundamentals import pipeline


def _patch_row_inputs(monkeypatch, info: dict, frame: pd.DataFrame):
    monkeypatch.setattr(pipeline.sec_edgar, "get_fundamentals", lambda t, no_fetch=False: ([{"x": 1}], [], {}))
    monkeypatch.setattr(pipeline.screen_common, "load_bars", lambda *a, **k: (frame, frame))     # D01 (C12)
    monkeypatch.setattr(pipeline, "load_ticker_info", lambda *a, **k: dict(info))
    monkeypatch.setattr(pipeline, "ohlc_drop_reason", lambda df: None)
    monkeypatch.setattr(pipeline, "compute_indicators", lambda df, spy, **k: {"price": 10.0, "sma200": 5.0,
                                                                              "within_52w_high_pct": 0.9})
    monkeypatch.setattr(pipeline, "_ttm_metrics", lambda q, a: {k: None for k in (
        "rev_growth_ttm", "fcf_margin", "ro40", "ro40_basis", "ttm_revenue", "ttm_net_income")})
    monkeypatch.setattr(pipeline, "_quality_metrics", lambda a: {k: None for k in (
        "gross_margin", "roic", "net_debt_ebitda", "total_equity")})


def test_the_stale_marker_is_stripped_and_counted(monkeypatch):
    frame = pd.DataFrame({"Close": [1.0]})
    frame.attrs["stale"] = True
    frame.attrs["short_refetch_refused"] = True
    _patch_row_inputs(monkeypatch, {"marketCap": 2e9, "_stale": True}, frame)
    row = pipeline._compute_row({"ticker": "AAA"}, None, spine={})
    assert row["info_stale"] is True and "short_refetch_refused" not in row    # D01 bars are never refused (PR 6)
    assert "'_stale'" not in repr(row)                   # the marker key never reaches a card field
    quality = pipeline.build_payload([row], run_stats={})["data_quality"]
    assert quality["stale_info"] == 1 and "short_refetch_refused" not in quality   # the count left with SPY's
    # fallback in PR 10 (F4 A, part 1)


def test_a_fresh_row_counts_nothing(monkeypatch):
    _patch_row_inputs(monkeypatch, {"marketCap": 2e9}, pd.DataFrame({"Close": [1.0]}))
    row = pipeline._compute_row({"ticker": "AAA"}, None, spine={})
    assert row["info_stale"] is False and "short_refetch_refused" not in row
