"""Tests for expiry row normalization + DTE bands."""
from __future__ import annotations

from RevenueChaser.module_spx_gamma.expiry_table import dte_band, normalize_expiry_rows


def test_normalize_expiry_rows_net_fields():
    payload = {"data": [
        {"expiry": "2026-08-14", "dte": 0, "call_gex": "1", "put_gex": "-1",
         "call_delta": "2", "put_delta": "-1", "call_charm": "3", "put_charm": "-1",
         "call_vanna": "4", "put_vanna": "-1"},
    ]}
    rows = normalize_expiry_rows(payload)
    assert rows[0]["expiry"] == "2026-08-14"
    assert rows[0]["net_gex"] == 0.0
    assert rows[0]["net_dex"] == 1.0
    assert rows[0]["band"] == "0DTE"


def test_normalize_expiry_rows_sorted():
    payload = {"data": [
        {"expiry": "2026-09-18", "dte": 35},
        {"expiry": "2026-08-14", "dte": 0},
    ]}
    rows = normalize_expiry_rows(payload)
    assert [r["expiry"] for r in rows] == ["2026-08-14", "2026-09-18"]


def test_dte_band():
    assert dte_band(None) == ""
    assert dte_band(-1) == "expired"
    assert dte_band(0) == "0DTE"
    assert dte_band(7) == "1–7D"
    assert dte_band(30) == "8–30D"
    assert dte_band(90) == "31–90D"
    assert dte_band(91) == "90D+"
