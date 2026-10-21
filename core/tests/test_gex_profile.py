"""Tests for core/gex_profile — strike-level GEX (volume book) profile for R4."""
from RevenueChaser.core.gex_profile import (
    normalize_strike_rows, volume_flip, magnet, charm_pin, vanna_lobes,
    build_profile,
)


def _row(strike, cg=None, pg=None, cc=None, pc=None, cv=None, pv=None):
    return {
        "strike": str(strike),
        "call_gamma_oi": None, "put_gamma_oi": None,
        "call_gamma_vol": None if cg is None else str(cg),
        "put_gamma_vol": None if pg is None else str(pg),
        "call_charm_oi": None, "put_charm_oi": None,
        "call_charm_vol": None if cc is None else str(cc),
        "put_charm_vol": None if pc is None else str(pc),
        "call_vanna_oi": None, "put_vanna_oi": None,
        "call_vanna_vol": None if cv is None else str(cv),
        "put_vanna_vol": None if pv is None else str(pv),
    }


def _coo_rows():
    """COO-like: spot ~66.8. Net gamma (vol): 60 +, 65/70 red, 75/80 green."""
    return normalize_strike_rows([
        _row(60, cg=332, pv=-2403),
        _row(65, cg=-588, pc=-984, pv=-709),
        _row(70, cg=-2304, pc=-14135, cv=14776),
        _row(75, cg=2968, pc=-22190, cv=31751),
        _row(80, cg=12941, pc=-88121, cv=252055),
    ])


class TestNormalize:
    def test_accepts_payload_dict_and_unwrapped_list(self):
        rows = _coo_rows()
        assert normalize_strike_rows({"data": _coo_rows()}) == rows
        assert rows[0]["strike"] == 60.0

    def test_empty(self):
        assert normalize_strike_rows(None) == []
        assert normalize_strike_rows({}) == []
        assert normalize_strike_rows({"data": "x"}) == []


class TestVolumeFlip:
    def test_flip_from_cumulative_crossing(self):
        rows = _coo_rows()
        flip = volume_flip(rows, 66.83)
        # cumulative net gamma (vol): 60:+332 65:-256 70:-2560 75:+408 80:13349
        # first zero cross lies between 60 and 65; that is also the nearest.
        assert flip is not None
        assert 60.0 < flip < 65.0

    def test_no_crossing_returns_none(self):
        rows = normalize_strike_rows([_row(60, cg=100), _row(70, cg=50)])
        assert volume_flip(rows, 65) is None

    def test_requires_spot(self):
        assert volume_flip(_coo_rows(), None) is None


class TestMagnet:
    def test_magnet_from_volume_net_gamma(self):
        rows = _coo_rows()
        mag = magnet(rows, 66.83)
        assert mag["strike"] in (70.0, 75.0)
        assert mag["side"] in ("above", "below")

    def test_spot_within_window(self):
        rows = normalize_strike_rows([
            _row(55, cg=-100), _row(60, cg=400), _row(80, cg=9000),
        ])
        # 9000 at 80 is far (20% > 15% window) so it is excluded; magnet = 60.
        mag = magnet(rows, 66.83)
        assert mag["strike"] == 60.0


class TestCharmPin:
    def test_far_dominant_is_peak_not_pin(self):
        rows = normalize_strike_rows([
            _row(65, pc=-984), _row(70, pc=-14135),
            _row(75, pc=-22190), _row(80, pc=-88121),
        ])
        c = charm_pin(rows, 66.83)
        assert c["label"] == "Charm Peak"
        assert c["is_dominant"] is True
        assert c["is_near_spot"] is False
        assert c["strike"] == 80.0

    def test_near_and_dominant_is_pin(self):
        rows = normalize_strike_rows([
            _row(66, pc=-9000), _row(70, pc=-1000),
        ])
        c = charm_pin(rows, 66.83)
        assert c["label"] == "Charm Pin"
        assert c["strike"] == 66.0

    def test_empty_rows(self):
        assert charm_pin([], 66.83) == {}


class TestVannaLobes:
    def test_separate_lobes_by_magnitude(self):
        rows = _coo_rows()
        v = vanna_lobes(rows, 66.83)
        assert v["put_peak"]["strike"] == 60.0
        assert v["call_peak"]["strike"] == 80.0


class TestBuildProfile:
    def test_full_profile(self):
        p = build_profile(_coo_rows(), 66.83, em_pct=7.43)
        assert p["book"] == "directionalized_volume"
        # EM band 61.9-71.8 covers 65 and 70 (both red) -> short gamma.
        assert p.get("regime") == "short"
        assert "magnet" in p
        assert "charm" in p
        assert "vanna" in p

    def test_wide_default_window_can_flip_regime(self):
        # Without EM the default band includes 75 (green) and reads long.
        p = build_profile(_coo_rows(), 66.83)
        assert p.get("regime") == "long"

    def test_missing_spot_empty(self):
        assert build_profile(_coo_rows(), None) == {}


class TestCumulativeCrossings:
    def test_interpolates_each_sign_change(self):
        from RevenueChaser.core.gex_profile import cumulative_crossings
        # cumulative: +10, -10 (cross between 100 and 110), +10 (cross between 110 and 120)
        pts = [(100, 10.0), (110, -20.0), (120, 20.0)]
        assert cumulative_crossings(pts) == [105.0, 115.0]

    def test_unsorted_input_is_sorted_by_strike(self):
        from RevenueChaser.core.gex_profile import cumulative_crossings
        assert cumulative_crossings([(110, -20.0), (100, 10.0)]) == [105.0]

    def test_none_values_are_skipped_and_flat_zero_is_no_crossing(self):
        from RevenueChaser.core.gex_profile import cumulative_crossings
        assert cumulative_crossings([(100, None), (110, 5.0), (120, 0.0), (130, 5.0)]) == []

    def test_nearest_crossing(self):
        from RevenueChaser.core.gex_profile import nearest_crossing
        assert nearest_crossing([105.0, 115.0], 112.0) == 115.0
        assert nearest_crossing([], 112.0) is None
        assert nearest_crossing([105.0, 115.0], None) is None

    def test_volume_flip_is_the_same_walk(self):
        from RevenueChaser.core.gex_profile import cumulative_crossings, nearest_crossing
        rows = _coo_rows()
        pts = [(r["strike"], (r["call_gamma_vol"] or 0) + (r["put_gamma_vol"] or 0)) for r in rows]
        assert volume_flip(rows, 67.0) == nearest_crossing(cumulative_crossings(pts), 67.0)


class TestRegime:
    def test_spot_above_oi_flip_is_long_gamma(self):
        from RevenueChaser.core.gex_profile import regime
        r = regime(7800.0, 7700.0)
        assert r["state"] == "long" and r["label"] == "Long Gamma — Dampening"
        assert r["flip"] == 7700.0 and r["flip_source"] == "oi"

    def test_spot_below_oi_flip_is_short_gamma(self):
        from RevenueChaser.core.gex_profile import regime
        r = regime(7600.0, 7700.0)
        assert r["state"] == "short" and r["label"] == "Short Gamma — Amplifying"

    def test_spot_on_the_flip(self):
        from RevenueChaser.core.gex_profile import regime
        r = regime(7700.0, 7700.0)
        assert r["state"] == "at_flip" and r["label"] == "At the gamma flip"

    def test_missing_spot_or_flip_is_unknown_not_a_signal(self):
        from RevenueChaser.core.gex_profile import regime
        for args in ((None, 7700.0), (7700.0, None), (None, None)):
            r = regime(*args)
            assert r["state"] == "unknown" and r["label"] is None

    def test_oi_and_volume_flips_shown_side_by_side(self):
        from RevenueChaser.core.gex_profile import regime
        r = regime(7750.0, 7700.0, flip_vol=7800.0, cumulative_flip=7690.0)
        assert r["flips"] == {"oi": 7700.0, "vol": 7800.0, "cumulative": 7690.0}
        assert r["sides"] == {"oi": "long", "vol": "short", "cumulative": "long"}
        assert r["agree"] is False
        assert r["state"] == "long"  # the OI flip decides; the others are shown, not voted

    def test_agree_when_every_available_flip_gives_the_same_side(self):
        from RevenueChaser.core.gex_profile import regime
        assert regime(7750.0, 7700.0, flip_vol=7600.0)["agree"] is True
        assert regime(7750.0, 7700.0)["agree"] is True  # nothing to disagree with

    def test_unavailable_cross_checks_have_no_side(self):
        from RevenueChaser.core.gex_profile import regime
        r = regime(7750.0, 7700.0)
        assert r["sides"]["vol"] is None and r["sides"]["cumulative"] is None
