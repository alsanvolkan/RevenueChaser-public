"""Tests for expected move + IV picking + sanity gate."""
from __future__ import annotations

import math

import pytest

from RevenueChaser.module_spx_gamma.expected_move import expected_move, pick_iv


def test_expected_move_formula():
    em = expected_move(7900.0, 0.076)
    assert em is not None
    assert em["points"] == pytest.approx(7900.0 * 0.076 / math.sqrt(252))
    assert em["lower"] == pytest.approx(7900.0 - em["points"])
    assert em["upper"] == pytest.approx(7900.0 + em["points"])


def test_expected_move_none_when_missing():
    assert expected_move(None, 0.076) is None
    assert expected_move(7900.0, None) is None


def test_expected_move_unit_sanity_gate():
    assert expected_move(7900.0, 0.0) is None       # non-positive
    assert expected_move(7900.0, -0.1) is None      # negative
    assert expected_move(7900.0, 0.0005) is None    # below IV_MIN (0.001)
    assert expected_move(7900.0, 3.0) is None       # above IV_MAX (2.50)
    assert expected_move(7900.0, 2.5) is not None   # boundary is valid


def test_pick_iv_prefers_1_day():
    rows = [{"days": 7, "volatility": "0.12"}, {"days": 1, "volatility": "0.076"}]
    assert pick_iv(rows) == 0.076


def test_pick_iv_none_when_empty():
    assert pick_iv([]) is None
    assert pick_iv([{"days": 1, "volatility": None}]) is None
