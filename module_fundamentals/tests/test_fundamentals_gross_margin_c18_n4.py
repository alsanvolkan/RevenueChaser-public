"""Review N4 (28 Oct): Quality's gross margin follows the same comparability rule as every other board
(``fiscal_year.gross_margin``, C18). Before, it divided gross profit by revenue without checks, so a filing whose
gross profit exceeded its revenue (a tagging error) passed "Gross margin ≥30%" at over 100%; and a negative
revenue turned a gross loss into a positive margin. Both are now unknown, never a pass."""
from __future__ import annotations

import pytest

from RevenueChaser.module_fundamentals.pipeline import _quality_metrics


def _gm(revenue, gross_profit):
    return _quality_metrics([{"revenue": revenue, "gross_profit": gross_profit, "ebit": 10.0,
                              "total_debt": 0.0, "cash": 0.0, "total_equity": 100.0}])["gross_margin"]


def test_gross_profit_above_revenue_is_unknown():
    assert _gm(100.0, 180.0) is None


def test_negative_revenue_is_unknown():
    assert _gm(-100.0, -50.0) is None


def test_a_normal_year_keeps_its_margin():
    assert _gm(1000.0, 400.0) == pytest.approx(0.40)
