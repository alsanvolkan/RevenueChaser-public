"""Quick Remarks engine testleri (3-sinyal 0-9, synthetic data, ağ yok)."""
from __future__ import annotations

import pytest

import RevenueChaser.core.ticker_store as ts
from RevenueChaser.module_asymmetric_growth import engine as agh
from RevenueChaser.module_financials.engines import quick_remarks_engine as qr


def _card(total_points: int = 7) -> dict:
    return {
        "ticker": "TEST",
        "name": "Test Corp",
        "price": 50.0,
        "sector": "Technology",
        "total_points": total_points,
        "dilution_warning": False,
        "dilution_yoy": 0.01,
        "tech_breakthrough": True,
        "rd_intensity": 0.2,
        "signals": [
            {"key": "growth", "name": "Accelerated Growth", "passed": 3, "total": 3, "subs": [
                {"label": "Revenue growth ≥ 20%", "value": 0.25, "threshold": "≥ 20%", "unit": "pct", "op": "gte", "param": "rev_growth", "passed": True},
                {"label": "Growth accelerating YoY", "value": [0.25, 0.1], "threshold": "this > last", "unit": "pair_pct", "op": "pair", "param": "accelerating", "passed": True},
                {"label": "Gross margin ≥ 40%", "value": 0.5, "threshold": "≥ 40%", "unit": "pct", "op": "gte", "param": "gross_margin", "passed": True},
            ]},
            {"key": "survival", "name": "Survival / Runway", "passed": 2, "total": 3, "subs": [
                {"label": "Free cash flow positive", "value": 10e6, "threshold": "> 0", "unit": "usd", "op": "gt", "param": "fcf", "passed": True},
                {"label": "Net debt / assets ≤ 10%", "value": 0.02, "threshold": "≤ 10%", "unit": "pct", "op": "lte", "param": "net_debt_assets", "passed": True},
                {"label": "Cash runway ≥ 2 years", "value": 999.0, "threshold": "≥ 2y", "unit": "runway", "op": "gte", "param": "runway", "passed": True},
            ]},
            {"key": "momentum", "name": "Momentum / Volume", "passed": 2, "total": 3, "subs": [
                {"label": "Within 10% of 52-week high", "value": 0.05, "threshold": "≤ 10%", "unit": "pct", "op": "lte", "param": "dist_52w", "passed": True},
                {"label": "10d volume ≥ 1.4× 90d", "value": 1.4, "threshold": "≥ 1.4×", "unit": "ratio", "op": "gte", "param": "vol_ratio", "passed": True},
                {"label": "Price above 50-day SMA", "value": True, "threshold": "> SMA50", "unit": "bool", "op": "bool", "param": "above_sma50", "passed": True},
            ]},
        ],
    }


def test_store_path(monkeypatch):
    monkeypatch.setattr(ts, "get", lambda t: _card(7))
    res = qr.run_quick_remarks("TEST")
    assert res["source"] == "ticker_scores"
    assert res["score"] == 7
    assert res["max"] == 9
    assert len(res["signals"]) == 3
    assert res["signals"][0]["passed"] == 3
    assert res["tech_breakthrough"] is True
    assert res["dilution_warning"] is False


def test_store_fallback_to_yfinance(monkeypatch):
    monkeypatch.setattr(ts, "get", lambda t: None)
    monkeypatch.setattr(qr.yfinance_client, "get_financials", lambda t: {})
    monkeypatch.setattr(qr, "fetch_technical", lambda t: {})
    monkeypatch.setattr(agh, "score_ticker", lambda t, f, tech, meta=None: _card(5))
    res = qr.run_quick_remarks("TEST")
    assert res["source"] == "yfinance"
    assert res["score"] == 5
    assert res["max"] == 9


def test_store_unavailable_and_no_data(monkeypatch):
    monkeypatch.setattr(ts, "get", lambda t: None)
    monkeypatch.setattr(qr.yfinance_client, "get_financials", lambda t: {})
    monkeypatch.setattr(qr, "fetch_technical", lambda t: {})
    monkeypatch.setattr(agh, "score_ticker", lambda t, f, tech, meta=None: None)
    res = qr.run_quick_remarks("TEST")
    assert res["source"] == "yfinance"
    assert res["score"] is None
    assert res["signals"] == []


_EVIL = '"><img src=x onerror=alert(1)></script><script>alert(1)</script>'


def _evil_result() -> dict:
    return {
        "ticker": _EVIL, "company": _EVIL, "sector": _EVIL, "source": _EVIL,
        "score": 5, "max": 9, "price": 10.0,
        "signals": [{"name": _EVIL, "passed": 1, "total": 3, "subs": [
            {"label": _EVIL, "value": _EVIL, "threshold": _EVIL, "unit": "text", "passed": True},
        ]}],
    }


@pytest.mark.parametrize("fn", ["render_quick_remarks_section", "render_quick_remarks_html"])
def test_render_escapes_provider_strings(fn):
    """FN-1: company, sector, source, signal and sub text are escaped."""
    from RevenueChaser.module_financials.reporting import quick_remarks_html as qh
    html = getattr(qh, fn)(_evil_result())
    assert "<img" not in html
    assert "<script" not in html
    assert "&lt;img src=x" in html


# ── #151: an unknown AG sub-criterion is shown as unknown, not as a failed one ──

def _signals_result():
    card = _card(7)
    growth = card["signals"][0]
    growth["subs"][0]["passed"] = None
    growth["subs"][1]["passed"] = None
    growth.update(passed=1, known=1)
    return {"ticker": "TEST", "name": "T", "signals": card["signals"]}


def test_an_unknown_sub_has_a_question_mark_and_its_own_tone():
    from RevenueChaser.module_financials.reporting import quick_remarks_html as qh
    html = qh._signals_html(_signals_result())
    assert html.count("m-grey") == 2 and html.count("&#63;") == 2
    assert html.count("m-red") == 0 and html.count("m-green") == 7


def test_the_signal_score_names_the_known_count_only_when_something_is_unknown():
    from RevenueChaser.module_financials.reporting import quick_remarks_html as qh
    html = qh._signals_html(_signals_result())
    assert "1/3 of 1 known" in html
    assert html.count("2/3") == 2 and html.count("known") == 1   # the other two carry no known field


def test_a_stored_card_without_a_known_field_shows_the_plain_score():
    from RevenueChaser.module_financials.reporting import quick_remarks_html as qh
    card = _card(7)
    assert all("known" not in s for s in card["signals"])
    assert "known" not in qh._signals_html({"signals": card["signals"]})
