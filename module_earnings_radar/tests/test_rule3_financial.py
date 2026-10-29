"""R3 — Financial quality (sector-adaptive). Table-driven, no network.

Pin: coverage gate (<2 signals => unavailable, never 0), -10 per matched
cluster red-flag, cluster-specific metrics (SaaS deferred/RPO), GM/OM
supplements.
"""
import pytest

from RevenueChaser.module_earnings_radar.rules.rule3_financial import (
    score,
    _score_saas,
    _score_hardware,
    _score_crypto,
    _score_marketplace,
    _score_energy,
    _zdivergence,
)


def _pad(hist, n=4):
    """``hist`` with older revenue-only, undated rows up to ``n`` usable quarters (review S5: R3 needs four). The
    padding has no date, margin or balance field, so no YoY partner, OM vote or cluster signal comes from it."""
    return list(hist) + [{"revenue": 1000} for _ in range(n - len(hist))]


def test_empty_history_unavailable():
    r = score("AAPL", "C1_saas", [])
    assert r.available is False
    assert r.score is None


def test_no_revenue_unavailable():
    r = score("AAPL", "C1_saas", [{"gross_margin": 0.5}])
    assert r.available is False
    assert r.score is None


def test_insufficient_coverage_unavailable():
    # Only revenue -> signals_avail == 1 < 2 -> unavailable (never 0)
    r = score("AAPL", "C1_saas", _pad([
        {"revenue": 1000},
        {"revenue": 800},
    ]))
    assert r.available is False
    assert r.score is None
    assert "insufficient financial coverage" in r.reasons


def test_saas_healthy_full_signals():
    hist = [
        {"revenue": 1000, "gross_margin": 0.55, "operating_margin": 0.20,
         "deferred_revenue": 300, "fiscal_date": "2026-03-31"},
        {"revenue": 800, "deferred_revenue": 200, "fiscal_date": "2025-03-31"},
    ]
    r = score("AAPL", "C1_saas", _pad(hist))
    assert r.available is True
    # rev +15, deferred-accel +25, GM +10, OM +10
    assert r.score == 60.0
    assert r.raw_metrics["signal_coverage"] == pytest.approx(0.8)  # 4/5
    assert r.coverage == 0.40                                       # four usable quarters


def test_fintech_red_flags_penalize():
    hist = [{"revenue": 1000, "gross_margin": 0.05, "operating_margin": 0.05,
             "receivables": 500}]
    r = score("AAPL", "C3_fintech_lending", _pad(hist))
    assert r.available is True
    # -15*3 (nim/eff-ratio/receivables) -30 (3 matched flags); GM is scored once (E6). Four usable quarters:
    # coverage 0.40, never 1.0 off the signals of one quarter (review S5).
    assert r.score == -75.0
    assert r.coverage == 0.40 and r.raw_metrics["signal_coverage"] == 1.0
    assert any("RED FLAGS" in reason for reason in r.reasons)
    flags = " ".join(r.reasons)
    assert "nim_compression" in flags
    assert "deteriorating_efficiency_ratio" in flags
    assert "high_receivables" in flags


def test_gross_margin_supplement_clamped():
    # Extreme positive inputs must clamp to SCORE_RANGE high (100)
    hist = [{"revenue": 1000, "gross_margin": 0.99, "operating_margin": 0.99}]
    r = score("AAPL", "unmapped", _pad(hist))
    assert r.available is True
    assert r.score <= 100.0


def test_missing_deferred_revenue_does_not_fire_deceleration():
    # SaaS with revenue + margins but NO deferred_revenue field: no deceleration
    # flag may be fabricated (the mapper now maps missing -> None, not 0).
    hist = [
        {"revenue": 1200, "gross_margin": 0.55, "operating_margin": 0.20, "fiscal_date": "2026-03-31"},
        {"revenue": 1000, "gross_margin": 0.55, "fiscal_date": "2025-03-31"},
    ]
    r = score("AAPL", "C1_saas", _pad(hist))
    assert r.available is True
    assert r.score == 35.0  # rev growth +15, GM +10, OM +10; no DR deceleration
    joined = " ".join(r.reasons)
    assert "deceleration" not in joined
    assert "RED FLAGS" not in joined


def test_missing_inventory_gets_no_lean_credit():
    # Hardware with no inventory field must not be rewarded as 'lean' (inv/rev
    # ratio would be 0 < 10% -> +10 under the old 0-fill). Steady margins only.
    rows = [{"revenue": 1000, "gross_margin": 0.40, "operating_margin": 0.10, "fiscal_date": fd}
            for fd in ("2026-03-31", "2025-12-31", "2025-09-30", "2025-03-31")]
    r = score("AAPL", "C2_hardware", rows)
    assert r.available is True
    assert r.score == 0.0
    joined = " ".join(r.reasons)
    assert "inventory" not in joined


def test_fintech_reasons_call_margins_not_nim():
    # C3 uses gross/operating margin proxies — the reason text must not pretend
    # UW financials contain a real net interest margin.
    hist = [{"revenue": 1000, "gross_margin": 0.60, "operating_margin": 0.50,
             "receivables": 50}]
    r = score("AAPL", "C3_fintech_lending", _pad(hist))
    assert r.available is True
    joined = " ".join(r.reasons)
    assert "strong gross margin" in joined
    assert "NIM" not in joined


# --- _zdivergence -----------------------------------------------------------

def test_zdivergence_none_below_min_length():
    assert _zdivergence([1, 2, 3], [1, 2, 3, 4]) is None
    assert _zdivergence([1, 2, 3, 4], [1, 2, 3]) is None


def test_zdivergence_zero_for_identical_series():
    vals = [10, 20, 33, 40, 55]
    assert _zdivergence(vals, list(vals)) == pytest.approx(0.0)


def test_zdivergence_antisymmetric_under_argument_swap():
    a = [10, 20, 30, 40, 50]
    b = [10, 21, 33, 40, 41]
    assert _zdivergence(a, b) == pytest.approx(-_zdivergence(b, a))


# --- _score_hardware ----------------------------------------------------------

def test_score_hardware_gm_elasticity_om_delta_and_inventory_ratio():
    latest = {"revenue": 1000, "gross_margin": 0.20, "operating_margin": 0.05, "inventory": 500, "fiscal_date": "2026-03-31"}
    prev = {"revenue": 900, "gross_margin": 0.40, "operating_margin": 0.10, "fiscal_date": "2025-03-31"}
    s, r, f, a, t = _score_hardware(latest, [latest, prev])
    assert "negative_gross_margin_elasticity" in f
    assert "high_inventory_to_revenue" in f
    assert s == pytest.approx(-20 - 10 - 15)
    assert a == 3
    assert t == 4


def test_score_hardware_inventory_revenue_zdivergence_severe():
    # Inventory series scaled down so latest inv/rev ratio (0.15) stays in the neutral
    # zone and only the z-divergence signal fires (severe: divergence > 1.5).
    history = [
        {"revenue": 400, "inventory": 60},
        {"revenue": 380, "inventory": 40},
        {"revenue": 360, "inventory": 30},
        {"revenue": 340, "inventory": 24},
    ]
    divergence = _zdivergence([h["inventory"] for h in reversed(history)], [h["revenue"] for h in reversed(history)])
    assert divergence > 1.5
    s, r, f, a, t = _score_hardware(history[0], history)
    assert "inventory_growth_exceeds_revenue_growth" in f
    assert s == pytest.approx(-25)
    assert any("Z-div" in reason for reason in r)


def test_score_hardware_inventory_revenue_zdivergence_mild():
    history = [
        {"revenue": 67, "inventory": 23.8},
        {"revenue": 93, "inventory": 23.4},
        {"revenue": 94, "inventory": 20.0},
        {"revenue": 100, "inventory": 20.0},
    ]
    divergence = _zdivergence([h["inventory"] for h in reversed(history)], [h["revenue"] for h in reversed(history)])
    assert 0.5 < divergence < 1.5
    s, r, f, a, t = _score_hardware(history[0], history)
    assert f == []
    assert s == pytest.approx(-10)


def test_score_hardware_inventory_revenue_zdivergence_negative():
    history = [
        {"revenue": 120, "inventory": 15.6},
        {"revenue": 95, "inventory": 16.2},
        {"revenue": 113, "inventory": 23.2},
        {"revenue": 100, "inventory": 20.0},
    ]
    divergence = _zdivergence([h["inventory"] for h in reversed(history)], [h["revenue"] for h in reversed(history)])
    assert divergence < -0.5
    s, r, f, a, t = _score_hardware(history[0], history)
    assert f == []
    assert s == pytest.approx(15)


# --- _score_crypto ------------------------------------------------------------

def test_score_crypto_bullish_signals():
    latest = {"revenue": 1200, "gross_margin": 0.65, "cash_from_operations": 400, "total_assets": 1000, "fiscal_date": "2026-03-31"}
    prev = {"revenue": 900, "fiscal_date": "2025-03-31"}
    s, r, f, a, t = _score_crypto(latest, [latest, prev], btc_price=None)
    assert f == []
    assert s == pytest.approx(20 + 15 + 15)
    assert a == 3 and t == 3


def test_score_crypto_bearish_red_flags():
    latest = {"revenue": 600, "gross_margin": 0.20, "cash_from_operations": 50, "total_assets": 1000, "fiscal_date": "2026-03-31"}
    prev = {"revenue": 900, "fiscal_date": "2025-03-31"}
    s, r, f, a, t = _score_crypto(latest, [latest, prev], btc_price=None)
    assert set(f) == {
        "revenue_decline_despite_btc",
        "cost_to_mine_rises_faster_than_btc",
        "declining_cash_btc_reserve_coverage",
    }
    assert s == pytest.approx(-20 - 20 - 15)


# --- _score_marketplace --------------------------------------------------------

def test_score_marketplace_bullish_signals():
    latest = {"gross_margin": 0.65, "operating_margin": 0.20, "fiscal_date": "2026-03-31"}
    prev = {"gross_margin": 0.55, "fiscal_date": "2025-03-31"}
    s, r, f, a, t = _score_marketplace(latest, [latest, prev])
    assert f == []
    assert s == pytest.approx(20 + 10 + 15)
    assert a == 3 and t == 3


def test_score_marketplace_bearish_red_flags():
    latest = {"gross_margin": 0.15, "operating_margin": 0.05, "fiscal_date": "2026-03-31"}
    prev = {"gross_margin": 0.25, "fiscal_date": "2025-03-31"}
    s, r, f, a, t = _score_marketplace(latest, [latest, prev])
    assert set(f) == {"take_rate_compression", "margin_compression"}
    assert s == pytest.approx(-20 - 10)
    assert a == 3


# --- _score_energy --------------------------------------------------------------

def test_score_energy_bullish_signals():
    latest = {"revenue": 1150, "gross_margin": 0.45, "operating_margin": 0.25, "cash_from_operations": 200, "fiscal_date": "2026-03-31"}
    prev = {"revenue": 1000, "gross_margin": 0.30, "fiscal_date": "2025-03-31"}
    s, r, f, a, t = _score_energy(latest, [latest, prev])
    assert f == []
    assert s == pytest.approx(15 + 15 + 10 + 10 + 15)
    # Every scored branch counts, the OM one included (review S1: a was 4 over a fixed t of 3).
    assert a == 5 and t == 5


def test_score_energy_bearish_red_flags():
    latest = {"revenue": 850, "gross_margin": 0.08, "operating_margin": 0.03, "cash_from_operations": 20, "fiscal_date": "2026-03-31"}
    prev = {"revenue": 1000, "gross_margin": 0.30, "fiscal_date": "2025-03-31"}
    s, r, f, a, t = _score_energy(latest, [latest, prev])
    assert set(f) == {
        "revenue_contraction",
        "low_energy_margins",
        "negative_gm_elasticity",
        "thin_operating_margin",
    }
    assert s == pytest.approx(-15 - 10 - 15 - 10)
    assert a == 5 and t == 5


# --- negative_operating_margin flag (score()-level supplement) -----------------

def test_negative_operating_margin_flag_has_no_visible_effect_for_hardware():
    # The OM supplement in score() appends "negative_operating_margin" to an internal
    # flags list, but it's only surfaced in RuleResult.flags/reasons if it's a member of
    # the cluster's own red-flag set. C2_hardware's red-flag set doesn't include it, so
    # this branch changes signals_avail/coverage bookkeeping but is otherwise invisible
    # in the result — documenting that quirk rather than asserting it's "correct".
    latest = {"revenue": 1000, "gross_margin": 0.30, "operating_margin": -0.05, "inventory": 200, "fiscal_date": "2026-03-31"}
    prev = {"revenue": 900, "gross_margin": 0.32, "operating_margin": 0.02, "fiscal_date": "2025-03-31"}
    r = score("TICK", "C2_hardware", _pad([latest, prev]))
    assert r.available is True
    assert r.score == pytest.approx(-10.0)
    assert r.raw_metrics["signal_coverage"] == pytest.approx(0.8)
    assert r.flags == []
    assert not any("RED FLAGS" in reason for reason in r.reasons)


# ── Universal GAAP OM vote (Phase 1, COO lock) ───────────────────────────────

def _coo_history():
    """COO FY26 Q2 ended 2026-04-30 — GM held, SG&A spike, OM collapse."""
    rows = [
        # (fiscal_date, revenue, operating_income, gross_margin, sga)
        ("2026-04-30", 1.0815e9, -31.0e6, 0.6800, 676.2e6),
        ("2026-01-31", 1.0241e9, 212.8e6, 0.6788, 390.2e6),
        ("2025-10-31", 1.0652e9, 140.4e6, 0.5642, 419.2e6),
        ("2025-07-31", 1.0603e9, 175.7e6, 0.6526, 421.7e6),
        ("2025-04-30", 1.0023e9, 184.8e6, 0.6775, 399.0e6),
        ("2025-01-31", 0.9647e9, 182.0e6, 0.6844, 387.9e6),
        ("2024-10-31", 1.0184e9, 198.4e6, 0.6655, 391.4e6),
        ("2024-07-31", 1.0028e9, 192.5e6, 0.6611, 381.1e6),
    ]
    out = []
    for fd, rev, oi, gm, sga in rows:
        out.append({
            "revenue": rev, "operating_income": oi,
            "operating_margin": oi / rev, "gross_margin": gm,
            "sga": sga, "fiscal_date": fd,
        })
    return out


def test_universal_om_bearish_vote_does_not_flip_with_cluster():
    """COO: OM -21pt YoY, GM held, SG&A spike. The universal layer keeps the
    vote bearish even though the cluster (unmapped GM>50%) is positive."""
    r = score("COO", "unmapped", _coo_history())
    assert r.available is True
    assert r.score <= -15.0  # >8pt down floors negative regardless of GM +10
    joined = " ".join(r.reasons)
    assert "op. margin" in joined and "pts" in joined
    assert "opex event" in joined
    assert "SG&A z=" in joined and "core OM" in joined


def test_universal_om_unavailable_on_short_history():
    """< 4 usable quarters -> no GAAP vote; cluster-only path unchanged."""
    hist = [
        {"revenue": 1000, "gross_margin": 0.55, "operating_margin": 0.20,
         "fiscal_date": "2026-03-31"},
        {"revenue": 900, "operating_margin": 0.18, "fiscal_date": "2025-12-31"},
    ]
    r = score("AAPL", "unmapped", _pad(hist))
    assert r.available is True
    # no universal-OM reason strings appear
    assert not any("op. margin" in x and "pts" in x for x in r.reasons)


def test_universal_om_no_yoy_partner_is_reason_free():
    """Undated/short series must not fabricate a GAAP vote."""
    hist = [{"revenue": 1000, "gross_margin": 0.20, "operating_margin": 0.05, "fiscal_date": fd}
            for fd in ("2026-03-31", "2026-01-31", "2025-10-31", "2025-07-31")]
    r = score("X", "unmapped", hist)  # no 2025-03-31 partner -> no YoY
    assert r.available is True
    assert not any("pts" in x for x in r.reasons)


def test_universal_om_cluster_is_not_overridden_for_short_mild_case():
    """A >8pt OM drop floors negative, but a normal cluster signal on a short
    history is untouched (no YoY partner -> layer inactive)."""
    hist = [{"revenue": 1000, "gross_margin": 0.60, "operating_margin": 0.20,
             "fiscal_date": "2026-03-31"},
            {"revenue": 800, "gross_margin": 0.55, "fiscal_date": "2026-01-31"}]
    r = score("AAPL", "C1_saas", _pad(hist))
    assert r.available is True
    # C1 SaaS short history: layer inactive, cluster score stands
    assert not any("op. margin" in x for x in r.reasons)


# ── #196 E-1: R3 correctness (E2, E6, E7, E8) ───────────────────────────────

from RevenueChaser.module_earnings_radar.rules.rule3_financial import _yoY_index


def _dated(rows, dates):
    return [dict(r, fiscal_date=d) for r, d in zip(rows, dates)]


_Q6 = ["2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31", "2024-12-31"]


def test_e2_inventory_build_on_flat_revenue_fires_in_newest_first_history():
    # history is newest-first (the mapper contract). Latest inventory doubles on flat
    # revenue: a real build, read oldest-first the divergence is severe.
    rows = [{"revenue": 1000, "inventory": inv} for inv in (200, 100, 100, 100, 100, 100)]
    hist = _dated(rows, _Q6)
    s, r, f, a, t = _score_hardware(hist[0], hist)
    assert "inventory_growth_exceeds_revenue_growth" in f


def test_e2_inventory_release_is_not_flagged_as_a_build():
    # Mirror image: the OLDEST quarter holds the spike and the latest is flat. Read
    # newest-first the old spike was scored as if it were the latest delta.
    rows = [{"revenue": 1000, "inventory": inv} for inv in (100, 100, 100, 100, 100, 200)]
    hist = _dated(rows, _Q6)
    s, r, f, a, t = _score_hardware(hist[0], hist)
    assert "inventory_growth_exceeds_revenue_growth" not in f


def test_e2_zdivergence_pairs_inventory_and_revenue_by_quarter():
    # A quarter missing its inventory must drop out of BOTH series; filtering each
    # series on its own shifts revenue against inventory by one quarter.
    invs = (200, None, 100, 100, 100, 100, 100)
    revs = (1000, 5000, 1000, 1000, 1000, 1000, 1000)
    dates = _Q6 + ["2024-09-30"]
    rows = [{"revenue": rv, "inventory": iv, "fiscal_date": d} for rv, iv, d in zip(revs, invs, dates)]
    s, r, f, a, t = _score_hardware(rows[0], rows)
    assert "inventory_growth_exceeds_revenue_growth" in f


def test_e6_unmapped_counts_margins_once():
    hist = [{"revenue": 1000, "gross_margin": 0.60, "operating_margin": 0.20}]
    r = score("X", "unmapped", _pad(hist))
    assert r.score == 20.0  # GM +10, OM +10 once each (was 40)
    assert r.raw_metrics["signal_coverage"] == pytest.approx(2 / 3, abs=0.01)  # gm + om, no growth partner


def test_e6_unknown_cluster_label_counts_margins_once():
    hist = [{"revenue": 1000, "gross_margin": 0.60, "operating_margin": 0.20}]
    assert score("X", "C7_healthcare", _pad(hist)).score == 20.0


def test_e6_fintech_counts_gross_margin_once():
    hist = [{"revenue": 1000, "gross_margin": 0.60, "operating_margin": 0.50, "receivables": 50}]
    r = score("X", "C3_fintech_lending", _pad(hist))
    # nim +15, efficiency +15, low credit exposure +10; no extra GM/OM supplement
    assert r.score == 40.0


def test_e6_saas_keeps_the_generic_margin_supplements():
    # C1 does not score margins itself, so the supplements still apply once.
    hist = [{"revenue": 1000, "gross_margin": 0.60, "operating_margin": 0.20}]
    assert score("X", "C1_saas", _pad(hist)).score == 20.0


def test_e7_seasonal_quarter_is_not_read_as_growth():
    # +30% QoQ (Q4 seasonality) but +1.6% YoY: no "+15 revenue growth".
    hist = _dated(
        [{"revenue": 1300}, {"revenue": 1000}, {"revenue": 1010}, {"revenue": 1020}, {"revenue": 1280}],
        ["2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31"],
    )
    s, r, f, a, t = _score_saas(hist[0], hist)
    assert s == 0.0 and not any("rev growth" in x for x in r)
    assert a == 1


def test_e7_growth_uses_the_year_ago_quarter():
    hist = _dated(
        [{"revenue": 1300}, {"revenue": 1250}, {"revenue": 1200}, {"revenue": 1100}, {"revenue": 1000}],
        ["2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31"],
    )
    s, r, f, a, t = _score_saas(hist[0], hist)
    assert s == 15.0 and any("rev growth 30%" in x for x in r)


def test_e7_no_year_ago_row_leaves_growth_unavailable_not_qoq():
    hist = _dated([{"revenue": 1300}, {"revenue": 1000}], ["2026-03-31", "2025-12-31"])
    s, r, f, a, t = _score_saas(hist[0], hist)
    assert s == 0.0 and a == 0


def test_e7_undated_history_gets_no_prior():
    from RevenueChaser.module_earnings_radar.rules.rule3_financial import _yoy_prior
    assert _yoy_prior([{"revenue": 1}, {"revenue": 2}]) is None


def test_e8_52_53_week_calendar_finds_its_partner():
    # A 52/53-week filer: Jan 28 2024 / Feb 2 2025 / Feb 1 2026 quarter ends.
    rows = [{"fiscal_date": d} for d in ("2026-02-01", "2025-11-02", "2025-08-03", "2025-05-04", "2025-02-02")]
    assert _yoY_index(rows, "2026-02-01") == 4


def test_e8_partner_outside_the_window_is_rejected():
    rows = [{"fiscal_date": d} for d in ("2026-03-31", "2025-12-31", "2025-02-10")]
    assert _yoY_index(rows, "2026-03-31") is None  # 414 days back


def test_e8_closest_partner_wins():
    rows = [{"fiscal_date": d} for d in ("2026-03-31", "2025-03-20", "2025-04-01")]
    assert _yoY_index(rows, "2026-03-31") == 2


def test_e8_universal_om_votes_on_a_drifting_fiscal_calendar():
    dates = ["2026-02-01", "2025-11-02", "2025-08-03", "2025-05-04", "2025-02-02", "2024-11-03"]
    oms = [0.02, 0.18, 0.18, 0.18, 0.20, 0.20]
    hist = [{"revenue": 1000, "operating_margin": om, "gross_margin": 0.5, "fiscal_date": d}
            for om, d in zip(oms, dates)]
    r = score("COST", "unmapped", hist)
    assert any("pts" in x for x in r.reasons)  # the GAAP OM vote ran


def test_e2_undated_history_is_read_as_newest_first():
    # The mapper contract is newest-first; with no dates to sort by the scorer must
    # still read the series oldest-to-newest.
    rows = [{"revenue": 1000, "inventory": inv} for inv in (200, 100, 100, 100, 100, 100)]
    s, r, f, a, t = _score_hardware(rows[0], rows)
    assert "inventory_growth_exceeds_revenue_growth" in f


def test_e2_a_quarter_without_inventory_does_not_shift_the_revenue_series():
    # Flat inventory, one quarter with a huge revenue jump but NO inventory figure.
    # Aligned, that quarter is dropped from both series and nothing diverges.
    invs = (100, None, 100, 100, 100, 100, 100)
    revs = (1000, 5000, 1000, 1000, 1000, 1000, 1000)
    rows = [{"revenue": rv, "inventory": iv, "fiscal_date": d}
            for rv, iv, d in zip(revs, invs, _Q6 + ["2024-09-30"])]
    s, r, f, a, t = _score_hardware(rows[0], rows)
    assert not any("Z-div" in x for x in r)
    assert s == 0.0


def test_e8_partner_372_days_back_is_found():
    rows = [{"fiscal_date": d} for d in ("2026-04-04", "2025-03-28")]
    assert _yoY_index(rows, "2026-04-04") == 1


def test_e2_a_quarter_without_revenue_is_dropped_from_both_series():
    invs = (200, 100, 100, 100, 100, 100, 100)
    revs = (1000, None, 1000, 1000, 1000, 1000, 1000)
    rows = [{"revenue": rv, "inventory": iv, "fiscal_date": d}
            for rv, iv, d in zip(revs, invs, _Q6 + ["2024-09-30"])]
    s, r, f, a, t = _score_hardware(rows[0], rows)
    assert "inventory_growth_exceeds_revenue_growth" in f
