"""OCC option-symbol parser (shared by SPX Gamma and Implied Probability)."""
from __future__ import annotations

from RevenueChaser.core.options import parse_option_symbol


def test_parses_call_and_put():
    assert parse_option_symbol("SPXW260814C07800000") == {
        "root": "SPXW", "expiry": "2026-08-14", "type": "C", "strike": 7800.0}
    assert parse_option_symbol("SPXW260814P07765000")["strike"] == 7765.0
    assert parse_option_symbol("AAPL240202P00185000") == {
        "root": "AAPL", "expiry": "2024-02-02", "type": "P", "strike": 185.0}


def test_root_without_w_suffix():
    assert parse_option_symbol("SPX260814C07800000")["root"] == "SPX"


def test_rejects_garbage_and_bad_dates():
    assert parse_option_symbol("garbage") is None
    assert parse_option_symbol("") is None
    assert parse_option_symbol(None) is None
    # month 13 is not a real date
    assert parse_option_symbol("AAPL261302C00185000") is None
