"""The report's §2 regime conditions and §15 Net Delta weekly trend, and the §17 IVTS ratio.

The §2 case below is the 30 Oct 2026 report's own: spot 7,580.09 under a 7,750 flip, put support 7,500, VIX 19.42,
Net DEX +727.10, charm -17.39 pinned at 7,800. Its published conditions are a close above 7,750, the close staying
below it with 7,500 the next support, a VIX jump with backwardation, Net DEX through its 25% threshold (a 181.78
move, below 545.33), and charm turning from minus to plus.
"""
from __future__ import annotations

from RevenueChaser.module_spx_gamma import regime
from RevenueChaser.module_spx_gamma.vix import ivts


def _snapshot(**over) -> dict:
    snap = {
        "current_level": 7580.09,
        "asof_date": "2026-10-30",
        "levels": {"regime_threshold": 7750.0, "upper_resistance_band": 8100.0, "lower_support_band": 7500.0},
        "charm_pin_report": {"strike": 7800.0, "net_charm": -17.39},
        "vix": {"vix": 19.42, "vix3m": 20.78, "term_mode": "contango"},
        # The report prints this in billions: +727,10 milyar dolar.
        "net_dex": 727.10e9,
    }
    snap.update(over)
    return snap


# ── §2, the regime conditions ─────────────────────────────────────────────────


def test_the_five_conditions_of_the_30_oct_report():
    rows = regime.change_conditions(_snapshot())
    assert len(rows) == 5
    triggers = " | ".join(r["trigger"] for r in rows)
    assert "ABOVE 7,750" in triggers, "the flip, taken back up"
    assert "BELOW 7,750" in triggers
    assert "7,500 the next support" in " | ".join(r["then"] for r in rows)
    assert "VIX 19.42" in triggers and "backwardation" in triggers
    # 25% of +727,10 bn is 181,775 and 75% is 545,325 — the report prints a -181,78 bn move to 545,33 bn.
    assert "$181.78B" in triggers and "$545.33B" in triggers
    assert "Charm changes sign (from minus to plus)" in triggers
    assert "7,800" in " | ".join(r["then"] for r in rows), "the pin the sign change reverses"


def test_above_the_flip_states_the_mirror_conditions():
    rows = regime.change_conditions(_snapshot(current_level=7900.0))
    triggers = " | ".join(r["trigger"] for r in rows)
    assert "BELOW 7,750" in triggers and "ABOVE 7,750" in triggers
    assert "into negative gamma" in " | ".join(r["then"] for r in rows)


def test_a_condition_whose_level_is_missing_is_left_out_not_blank():
    bare = regime.change_conditions({"current_level": 7580.09, "levels": {}})
    assert bare == [], "no levels, no conditions"
    no_vix = regime.change_conditions(_snapshot(vix={}))
    assert not any("VIX" in r["trigger"] for r in no_vix)
    assert len(no_vix) == 4
    no_charm = regime.change_conditions(_snapshot(charm_pin_report={}))
    assert not any("Charm" in r["trigger"] for r in no_charm)


def test_a_zero_net_dex_states_no_threshold():
    """A 25% threshold off zero is zero, which would read as a condition that is always met."""
    rows = regime.change_conditions(_snapshot(net_dex=0.0))
    assert not any("Net DEX" in r["trigger"] for r in rows)


# ── §15, the Net Delta weekly trend ───────────────────────────────────────────

HISTORY = {
    "2026-10-21": {"net_dex": 1.0},
    "2026-10-23": {"net_dex": 123_299_826.10, "is_friday_close": True},
    "2026-10-27": {"net_dex": 2.0},
    "2026-10-30": {"net_dex": 125_970_898.49, "is_friday_close": True},
    "2026-11-03": {"net_dex": 66_616_757.02},
}


def test_the_trend_is_the_friday_closes_oldest_first():
    """The two Fridays are the 30 Oct report's own rows: +123,30 M$ and +125,97 M$."""
    got = regime.net_delta_trend(HISTORY, asof="2026-11-04")
    assert [r["date"] for r in got["rows"]] == ["2026-10-23", "2026-10-30"]
    assert got["rows"][0]["net_delta"] == 123_299_826.10
    assert got["rows"][1]["net_delta"] == 125_970_898.49
    assert (got["have"], got["want"], got["direction"]) == (2, 5, "up")


def test_the_day_being_rendered_never_appears_in_its_own_trend():
    got = regime.net_delta_trend(HISTORY, asof="2026-10-30")
    assert [r["date"] for r in got["rows"]] == ["2026-10-23"]
    assert got["direction"] is None, "one row is not a trend"


def test_only_the_last_weeks_fridays_are_kept():
    history = {f"2026-0{m}-0{d}": {"net_dex": float(i), "is_friday_close": True}
               for i, (m, d) in enumerate([(1, 2), (1, 9), (2, 6), (3, 6), (4, 3), (5, 1)], start=1)}
    got = regime.net_delta_trend(history, asof="2026-12-31", weeks=5)
    assert got["have"] == 5 and len(got["rows"]) == 5
    assert got["rows"][0]["date"] == "2026-01-09", "the oldest of six is dropped"


def test_no_history_is_an_empty_trend_not_an_error():
    for empty in ({}, None):
        got = regime.net_delta_trend(empty)
        assert got["rows"] == [] and got["have"] == 0 and got["direction"] is None


def test_a_friday_without_the_value_is_not_a_row():
    got = regime.net_delta_trend({"2026-10-23": {"is_friday_close": True}}, asof="2026-11-04")
    assert got["rows"] == []


# ── §17, IVTS ─────────────────────────────────────────────────────────────────


def test_ivts_is_vix_over_vix3m():
    """30 Oct: VIX 19.42 and VIX3M 20.78 give the report's 0,9346."""
    assert round(ivts({"^VIX": 19.42, "^VIX3M": 20.78}), 4) == 0.9346


def test_ivts_without_both_closes_is_none():
    assert ivts({"^VIX": 19.42}) is None
    assert ivts({"^VIX3M": 20.78}) is None
    assert ivts({"^VIX": 19.42, "^VIX3M": 0}) is None, "no division by a zero close"
