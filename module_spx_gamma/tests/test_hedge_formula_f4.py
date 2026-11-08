"""F4 (Fable report review, 5 Nov 2026): the 25-point hedge follows the report's formula.

The reports size a band's hedge as |band Net GEX per 1 %| x move %, where a 25-point move is 25 / spot x 100 %
(28 Aug Ek C p.12). The 30 Oct report (p.11) prints absorption 7735-7805, Net GEX +32.5M and a 25-point hedge of
10.72M at spot 7,580.09: 32.5M x 0.3298 = 10.72M. The board used |net| x 25 / band width, a different number.
"""
from __future__ import annotations

import pytest

from RevenueChaser.module_spx_gamma import levels


def test_hedge_for_25_points_reproduces_the_30_oct_report():
    assert levels.hedge_for_move(32.5e6, 7580.09, 25.0) == pytest.approx(10.72e6, rel=1e-3)


def test_hedge_is_none_without_spot_or_net():
    assert levels.hedge_for_move(None, 7580.0) is None
    assert levels.hedge_for_move(1e6, None) is None


def test_envelope_bands_carry_the_report_hedge():
    rows = [{"strike": 7500.0 + 10 * i, "net_gamma": (-1e6 if i < 5 else 2e6)} for i in range(12)]
    env = levels.cumulative_gex_envelope(rows, 7560.0)
    band = env["absorption"]
    assert band["per_25pt"] == pytest.approx(abs(band["net_gex"]) * 25.0 / 7560.0 * 100.0)
