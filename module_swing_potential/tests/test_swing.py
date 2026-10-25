"""Unit tests for the Swing Screener (pure functions, no network)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from RevenueChaser.module_swing_potential import config, indicators, ranking, screeners
from RevenueChaser.module_swing_potential.pipeline import build_payload
from RevenueChaser.module_swing_potential.report import render_swing_dashboard


def test_pipeline_imports_load_ticker_info():
    from RevenueChaser.module_swing_potential import pipeline as sp
    from RevenueChaser.module_fundamentals import pipeline as fp
    assert hasattr(sp, "load_ticker_info")
    assert hasattr(fp, "load_ticker_info")


def _ohlcv(periods=300) -> pd.DataFrame:
    idx = pd.bdate_range("2024-01-01", periods=periods)
    close = pd.Series(100 * np.exp(np.arange(periods) * 0.0015), index=idx)
    return pd.DataFrame({
        "Open": close.shift(1).fillna(close.iloc[0]),
        "High": close * 1.01,
        "Low": close * 0.99,
        "Close": close,
        "Volume": 1_000_000,
    })


# ── indicators ──
def test_normalize_ohlcv_requires_columns():
    assert indicators.normalize_ohlcv(pd.DataFrame({"Close": [1, 2, 3]})).empty
    df = _ohlcv()
    out = indicators.normalize_ohlcv(df)
    assert len(out) == len(df)


def test_compute_indicators_uptrend():
    ind = indicators.compute_indicators(_ohlcv())
    assert ind["price"] is not None and ind["price"] > 0
    assert ind["sma50"] is not None and ind["sma150"] is not None and ind["sma200"] is not None
    assert ind["sma50"] > ind["sma150"] > ind["sma200"]
    assert ind["adr_pct"] is not None and ind["adr_pct"] > 0
    assert ind["mom_12_1"] is not None and ind["mom_12_1"] > 0
    assert ind["consistency_6m"] is not None and 0 <= ind["consistency_6m"] <= 1


def test_compute_indicators_insufficient_history():
    ind = indicators.compute_indicators(_ohlcv(periods=20))
    assert ind["price"] is not None
    assert ind["sma200"] is None
    assert ind["mom_12_1"] is None


# ── screeners ──
def _pass_ind() -> dict:
    return {
        "price": 100.0, "sma50": 95.0, "sma150": 90.0, "sma200": 80.0,
        "sma200_prev_1m": 78.0, "sma150_prev_1m": 88.0,
        "above_52w_low_pct": 1.5, "within_52w_high_pct": 0.99,
        "vol_ratio": 2.0, "rs_vs_spy_3m": 0.05, "rs_vs_spy_12m": 0.1,
        "ema10": 96.0, "ema20": 92.0, "ema10_prev": 90.0, "ema20_prev": 88.0,
        "move_max_1_3m": 0.35, "adr_pct": 0.04, "atr14": 2.0,
        "change_pct": 0.01, "ret_3m": 0.10,
    }


def test_minervini_rs_no_longer_fails_the_gate():
    """#65 / #384 SW-S1: RS ranks only; the 7 price rules decide ``passed``, the RS rule still shows and counts."""
    g = screeners.evaluate_minervini(_pass_ind(), rs_rating=50.0)
    assert g["passed"] is True
    assert g["score"] == "7/8"
    rs_rule = next(r for r in g["rules"] if r["id"] == "rs_ge_70")
    assert rs_rule["passed"] is False
    assert rs_rule["unit"] == "rating"


def test_qullamaggie_five_of_six():
    ind = _pass_ind()
    ind["adr_pct"] = 0.01  # drop ADR below 3% -> still 5/6
    g = screeners.evaluate_qullamaggie(ind)
    assert g["passed"] is True
    assert g["score"] == "5/6"
    adr_rule = next(r for r in g["rules"] if r["id"] == "adr_ge_3")
    assert adr_rule["passed"] is False


def test_qullamaggie_fails_below_five():
    ind = _pass_ind()
    ind["adr_pct"] = 0.01    # ADR < 3%  -> fail
    ind["move_max_1_3m"] = 0.10  # move < 25% -> fail
    g = screeners.evaluate_qullamaggie(ind)
    assert g["passed"] is False
    assert g["score"] == "4/6"


# ── ranking ──
def test_percentile_ranks():
    ranks = ranking.percentile_ranks([("a", 1.0), ("b", 2.0), ("c", 3.0), ("d", None)])
    assert ranks["a"] == 0
    assert ranks["b"] == pytest.approx(50.0, abs=0.5)
    assert ranks["c"] == 100
    assert "d" not in ranks


def test_composite_weights():
    assert ranking.composite(100, 100, 100) == 100
    assert ranking.composite(0, 0, 0) == 0
    assert ranking.composite(100, 50, 50) == pytest.approx(75.0)


def test_execution_levels_atr():
    ind = _pass_ind()
    lv = ranking.execution_levels(ind)
    assert lv["entry"] == pytest.approx(100.0)
    assert lv["stop"] == pytest.approx(100.0 - 2 * 2.0)  # 96.0
    assert lv["target"] == pytest.approx(100.0 + 2 * 4.0)  # 108.0
    assert lv["rr"] == pytest.approx(2.0)
    assert lv["atr"] == pytest.approx(2.0)


def test_ma_alignment():
    assert ranking.ma_alignment(_pass_ind()) == "aligned"
    ind = _pass_ind()
    ind["sma50"] = 75.0  # below 150 -> broken
    assert ranking.ma_alignment(ind) == "broken"


# ── pipeline (build_payload, no network) ──
def test_build_payload_shape():
    rows = [
        {"ticker": "AAA", "name": "Alpha", "ind": _pass_ind(), "market_cap": 1e9,
         "sector": "Technology", "industry": "Software - Application", "themes": ["Software"]},
        {"ticker": "BBB", "name": "Beta", "ind": {**_pass_ind(), "rs_vs_spy_12m": 0.05},
         "market_cap": 5e9, "sector": "Healthcare", "industry": "Biotechnology", "themes": []},
    ]
    p = build_payload(rows, top_n=10)
    assert p["universe_total"] == 2
    assert p["selection_version"] == config.SELECTION_VERSION
    assert set(p["passing"].keys()) == {"minervini", "weinstein", "qullamaggie", "direction"}
    assert len(p["candidates"]) == 2
    c = p["candidates"][0]
    assert c["direction_ok"] is True
    assert c["methods"] == ["minervini", "weinstein", "qullamaggie"]
    assert c["bucket"] == "minervini+weinstein+qullamaggie"
    assert "multiplier" not in c and "direction" not in c
    assert c["market_cap"] == 1e9
    assert c["themes"] == ["Software"]
    assert c["atr"] == pytest.approx(2.0)
    assert set(c["gates"].keys()) == {"minervini", "weinstein", "qullamaggie", "direction"}
    assert c["flow"] == {"status": "not_scanned"}
    assert (c["sector"], c["industry"]) == ("Technology", "Software - Application")
    assert "industry_group" not in c and "sub_industry" not in c


def test_run_scan_passes_refresh_universe_flag(monkeypatch):
    from RevenueChaser.module_swing_potential import pipeline as sp
    captured = {}
    monkeypatch.setattr(sp, "fetch_universe_stats",
                        lambda **kw: captured.update(kw) or ([], {"raw_total": 0}))
    sp.run_scan(refresh_universe=True)
    assert captured["refresh"] is True
    assert captured["refreshed_by"] == "Swing Potential"


def test_run_scan_default_uses_cache(monkeypatch):
    from RevenueChaser.module_swing_potential import pipeline as sp
    captured = {}
    monkeypatch.setattr(sp, "fetch_universe_stats",
                        lambda **kw: captured.update(kw) or ([], {"raw_total": 0}))
    sp.run_scan()
    assert captured["refresh"] is False


def test_render_dashboard():
    payload = build_payload([], run_stats={})
    html = render_swing_dashboard(payload)
    assert "Swing Screener" in html
    assert "Flow not scanned" in html


def test_render_uses_the_shared_filter_kit():
    """#383 C1c: the universal filters are the rc_filters bar, not the old Sector cascade / MC / EWEEK / INDEX controls."""
    html = render_swing_dashboard(build_payload([], run_stats={}))
    for frag in ('id="rc-filters"', "RCFilters.mount", 'data-method="minervini"'):
        assert frag in html, f"missing shared filter fragment: {frag}"
    for gone in ("sectorOk(", "mcRange(", 'id="mc-tabs"', 'id="eweek-tabs"', 'id="index-tabs"',
                 'id="theme"', 'id="sector"', 'id="industry"',
                 'id="industry-group"', 'id="sub-industry"', "IGROUP", "ISUB",
                 "industry_group", "sub_industry", "data-pt", 'class="mult"'):
        assert gone not in html, f"the old filter markup is gone, found: {gone}"


def test_render_dashboard_universe_provenance():
    payload = build_payload([], run_stats={})
    payload["universe_refreshed_by"] = "Swing Potential"
    payload["universe_generated_at"] = "2026-08-24T10:00:00+00:00"
    html = render_swing_dashboard(payload)
    assert "Universe refreshed by" in html
    assert "Swing Potential" in html


def test_render_dashboard_no_provenance_when_missing():
    html = render_swing_dashboard(build_payload([], run_stats={}))
    assert "Universe refreshed by" not in html


# ── #153 U14 / U15 / U16 ──
def test_gate_thresholds_come_from_config(monkeypatch):
    from RevenueChaser.module_swing_potential import config
    ind = {"above_52w_low_pct": 1.3, "within_52w_high_pct": 0.8, "vol_ratio": 1.6,
           "move_max_1_3m": 0.3, "adr_pct": 0.04}

    def passed(res, rule_id):
        return next(r for r in res["rules"] if r["id"] == rule_id)["passed"]

    assert passed(screeners.evaluate_minervini(ind, 75), "above_52w_low")
    monkeypatch.setattr(config, "ABOVE_52W_LOW", 1.4)
    monkeypatch.setattr(config, "WITHIN_52W_HIGH", 0.9)
    monkeypatch.setattr(config, "RS_MIN", 80)
    res = screeners.evaluate_minervini(ind, 75)
    assert not passed(res, "above_52w_low")
    assert not passed(res, "within_52w_high")
    assert not passed(res, "rs_ge_70")
    res = screeners.evaluate_weinstein(ind)
    assert passed(res, "overhead_breakout") is False  # 0.8 < the default 0.98
    monkeypatch.setattr(config, "BREAKOUT_HIGH_PCT", 0.75)
    monkeypatch.setattr(config, "VOL_CONFIRM", 2.0)
    res = screeners.evaluate_weinstein(ind)
    assert passed(res, "overhead_breakout") is True   # 0.8 >= 0.75: only a wired threshold passes
    assert passed(res, "volume_confirmation") is False  # 1.6 < 2.0
    monkeypatch.setattr(config, "MOVE_MIN", 0.5)
    monkeypatch.setattr(config, "ADR_MIN", 0.05)
    res = screeners.evaluate_qullamaggie(ind)
    assert not passed(res, "large_move")
    assert not passed(res, "adr_ge_3")


def test_mom_12_1_is_return_from_12_months_to_1_month_ago():
    idx = pd.bdate_range("2024-01-01", periods=300)
    close = pd.Series(100.0, index=idx)
    close.iloc[-253] = 100.0
    close.iloc[-22] = 150.0   # price one month ago
    close.iloc[-1] = 120.0    # latest month fell back
    df = pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                       "Close": close, "Volume": 1_000_000})
    ind = indicators.compute_indicators(df)
    assert ind["mom_12_1"] == pytest.approx(150.0 / 100.0 - 1.0)


@pytest.mark.parametrize("price,sma,sign", [(500, 400, "&gt;"), (350, 400, "&lt;")])
def test_summary_spy_vs_200dma_wording(price, sma, sign):
    from RevenueChaser.module_swing_potential import report as sw
    from RevenueChaser.module_fundamentals import report as fu
    s = {"regime": "RISK-ON" if price > sma else "RISK-OFF", "spy_price": price,
         "spy_sma200": sma, "passing": {}}
    for mod in (sw, fu):
        html = mod._summary_html(s)
        assert f"{sign} 200DMA" in html
