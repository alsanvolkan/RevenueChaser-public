"""The funnel names the thresholds the prefilter used (Swing review finding 1, PR 6).

The prefilter drops on the board's effective settings (a Settings save of the price floor applies to the next
Fetch), but the funnel used to build its "Price under $X" text from ``config``'s defaults: a $10 floor dropped every
$5-10 name and the funnel said "Price under $5.00". The ``Prefiltered`` result now carries the thresholds it used,
and the funnel reads them from there.
"""
from __future__ import annotations

from types import SimpleNamespace

from RevenueChaser.core import screen_common as sc

DEFAULTS = SimpleNamespace(MIN_PRICE=5.0, MIN_DOLLAR_VOLUME=250_000)
SAVED = SimpleNamespace(MIN_PRICE=10.0, MIN_DOLLAR_VOLUME=1_000_000)
QUOTES = {"CHEAP": {"price": 7.0, "avg_volume": 1_000_000}, "THIN": {"price": 20.0, "avg_volume": 10_000},
          "OK": {"price": 50.0, "avg_volume": 1_000_000}}


def test_the_funnel_labels_say_the_floors_the_prefilter_used():
    pre = sc.prefilter([{"ticker": t} for t in QUOTES], SAVED, quotes=QUOTES)
    assert pre.drops == {"low_price": ["CHEAP"], "low_dollar_volume": ["THIN"]}
    steps = {s["key"]: s for s in sc.funnel_steps(universe=3, excluded_types=0, prefiltered=pre, dropped={},
                                                   scored=1, passed=1, shown=1, cfg=DEFAULTS)}
    assert steps["low_price"]["label"] == "Price under $10.00"
    assert steps["low_dollar_volume"]["label"] == "Average daily dollar volume under $1,000,000"


def test_a_prefilter_result_without_thresholds_keeps_the_boards_config():
    pre = sc.Prefiltered([], {"low_price": ["A"]})
    steps = {s["key"]: s for s in sc.funnel_steps(universe=1, excluded_types=0, prefiltered=pre, dropped={},
                                                   scored=0, passed=0, shown=0, cfg=DEFAULTS)}
    assert steps["low_price"]["label"] == "Price under $5.00"
