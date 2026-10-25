"""core.hit_rate — the read-side readability gate of the hit-rate ledger (#25 HR-2). Offline; the store is a temp DuckDB.

Every test writes the three tables the way their writers do (``ledger.write_picks`` for the picks, ``rawstore.write_rows``
for the derived outcome and baseline rows) and reads them back through ``hit_rate.summary``. The trailing volatility of a
pick is handed in as ``vol_of`` (a ``{(ticker, date): vol}`` lookup), except in the one test that pins the default read
from the stored bars.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from RevenueChaser.core import baselines, hit_rate, ledger, outcomes, rawstore

D1, D2, D3 = date(2026, 9, 1), date(2026, 9, 8), date(2026, 9, 15)       # three Tuesdays
H = hit_rate.HEADLINE_HORIZON


def _freeze(source, day, subjects, *, model_version="v1+abcd"):
    out = ledger.write_picks(source, source, [{"subject": s, "rank": i + 1} for i, s in enumerate(subjects)],
                             model_version=model_version, forecast_date=day, run_id=f"freeze-{day}")
    assert out.frozen == len(subjects), out


def _outcome(source, day, subject, *, mfe, mae=-0.05, status=outcomes.CLOSED, horizon=H,
             model_version=outcomes.MODEL_VERSION):
    return {"source": source, "forecast_date": day, "subject": subject, "target_type": outcomes.PICK,
            "target_date": day, "horizon_days": horizon, "ref_price": 100.0 if mfe is not None else None,
            "mfe": mfe, "mae": mae, "sessions_seen": 21, "status": status, "computed_at": 0.0,
            "model_version": model_version, "run_id": "o"}


def _baseline(day, basis, *, n, ge10, vol_lo=None, vol_hi=None, status=outcomes.CLOSED, horizon=H,
              model_version=baselines.MODEL_VERSION):
    return {"forecast_date": day, "horizon_days": horizon, "basis": basis, "n": n, "ge20": 0, "ge10": ge10,
            "ge5": ge10, "mfe_mean": 0.05, "mae_mean": -0.05, "vol_lo": vol_lo, "vol_hi": vol_hi,
            "status": status, "computed_at": 0.0, "model_version": model_version, "run_id": "b"}


def _deciles(day, rates, *, n=100):
    """Ten decile rows for ``day``: decile k covers vols [k-1, k) (open at both ends), ``rates[k-1]`` its hit rate."""
    rows = []
    for k in range(1, 11):
        lo = None if k == 1 else float(k - 1)
        hi = None if k == 10 else float(k)
        rows.append(_baseline(day, baselines.decile_basis(k), n=n, ge10=round(rates[k - 1] * n), vol_lo=lo,
                              vol_hi=hi))
    return rows


def _board(source="swing", *, days=(D1, D2, D3), per_day=10, hits_per_day=4, vol=4.5, model_version="v1+abcd",
           universe_rate=0.30, peer_rate=0.50, with_baselines=True):
    """``per_day`` distinct tickers frozen on each of ``days``, all closed at 30 days, ``hits_per_day`` of them with an
    MFE of exactly +10% (a hit: inclusive) and the rest +9.99%. Every pick's vol is ``vol`` (decile 5), whose peers hit
    at ``peer_rate``; the universe hits at ``universe_rate``."""
    out_rows, base_rows, vols = [], [], {}
    for j, day in enumerate(days):
        subjects = [f"{source[:2].upper()}{j}{i:02d}" for i in range(per_day)]
        _freeze(source, day, subjects, model_version=model_version)
        for i, s in enumerate(subjects):
            out_rows.append(_outcome(source, day, s, mfe=0.10 if i < hits_per_day else 0.0999))
            vols[(s, day)] = vol
        base_rows.append(_baseline(day, baselines.UNIVERSE, n=1000, ge10=round(universe_rate * 1000)))
        rates = [0.0] * 10
        rates[4] = peer_rate
        base_rows += _deciles(day, rates)
    rawstore.write_rows("forecast_outcomes", out_rows)
    if with_baselines:
        rawstore.write_rows("forecast_baselines", base_rows)
    return vols


def _one(groups, family="swing", model_version="v1+abcd"):
    got = [g for g in groups if g["family"] == family and g["model_version"] == model_version]
    assert len(got) == 1, groups
    return got[0]


# ── the empty and the not-yet-closed store ───────────────────────────────────

def test_an_empty_store_has_no_group_and_does_not_raise(rawstore_tmp_db):
    assert hit_rate.summary(vol_of={}.get) == []


def test_a_store_that_was_never_created_reads_as_empty(isolate_rawstore):
    assert hit_rate.summary(vol_of={}.get) == []


def test_frozen_picks_with_only_open_windows_say_no_closed_window_yet(rawstore_tmp_db):
    _freeze("swing", D1, ["AAA", "BBB"])
    rawstore.write_rows("forecast_outcomes", [_outcome("swing", D1, "AAA", mfe=0.50, status=outcomes.OPEN),
                                              _outcome("swing", D1, "BBB", mfe=0.01, status=outcomes.OPEN)])
    g = _one(hit_rate.summary(vol_of={}.get))
    assert g["state"] == hit_rate.NO_CLOSED_WINDOW
    assert (g["frozen"], g["frozen_dates"]) == (2, 1)
    assert (g["records"], g["freeze_dates"]) == (0, 0)
    assert g["gate"] == {"met": False, "reason": "no closed window yet"}
    assert g["headline"] is None


def test_a_pick_with_no_outcome_row_yet_counts_as_open(rawstore_tmp_db):
    _freeze("swing", D1, ["AAA"])
    g = _one(hit_rate.summary(vol_of={}.get))
    assert g["state"] == hit_rate.NO_CLOSED_WINDOW and g["frozen"] == 1 and g["records"] == 0


# ── the gate: >= 30 records and >= 3 freeze dates, each edge ─────────────────

def test_thirty_records_over_three_freeze_dates_meet_the_gate(rawstore_tmp_db):
    vols = _board()
    g = _one(hit_rate.summary(vol_of=lambda t, d: vols.get((t, d))))
    assert (g["records"], g["freeze_dates"]) == (30, 3)
    assert g["gate"]["met"] is True and g["state"] == hit_rate.GATE_MET
    head = g["headline"]
    assert (head["hits"], head["records"], head["freeze_dates"]) == (12, 30, 3)
    assert head["rate"] == pytest.approx(0.40)


def test_twenty_nine_records_do_not_meet_the_gate_and_show_no_rate(rawstore_tmp_db):
    vols = _board(per_day=10)
    rawstore.write_rows("forecast_outcomes", [_outcome("swing", D3, "SW209", mfe=0.0999, status=outcomes.OPEN)])
    g = _one(hit_rate.summary(vol_of=lambda t, d: vols.get((t, d))))
    assert (g["records"], g["freeze_dates"]) == (29, 3)
    assert g["state"] == hit_rate.GATE_NOT_MET
    assert g["gate"] == {"met": False, "reason": "29 of 30 records"}
    assert g["headline"] is None


def test_two_freeze_dates_do_not_meet_the_gate_however_many_records(rawstore_tmp_db):
    vols = _board(days=(D1, D2), per_day=20)
    g = _one(hit_rate.summary(vol_of=lambda t, d: vols.get((t, d))))
    assert (g["records"], g["freeze_dates"]) == (40, 2)
    assert g["gate"] == {"met": False, "reason": "2 of 3 freeze dates"}
    assert g["headline"] is None


def test_both_shortfalls_are_named(rawstore_tmp_db):
    vols = _board(days=(D1, D2), per_day=5)
    g = _one(hit_rate.summary(vol_of=lambda t, d: vols.get((t, d))))
    assert g["gate"]["reason"] == "10 of 30 records, 2 of 3 freeze dates"


# ── open windows and the 90-day horizon are not in the headline ──────────────

def test_open_windows_are_excluded_from_records_and_from_the_rate(rawstore_tmp_db):
    vols = _board()
    _freeze("swing", date(2026, 9, 22), ["OPEN1", "OPEN2"])
    rawstore.write_rows("forecast_outcomes", [_outcome("swing", date(2026, 9, 22), s, mfe=0.90, status=outcomes.OPEN)
                                              for s in ("OPEN1", "OPEN2")])
    g = _one(hit_rate.summary(vol_of=lambda t, d: vols.get((t, d))))
    assert (g["frozen"], g["frozen_dates"]) == (32, 4)
    assert (g["records"], g["freeze_dates"]) == (30, 3)
    assert g["headline"]["rate"] == pytest.approx(0.40)


def test_a_closed_ninety_day_row_does_not_stand_in_for_the_thirty_day_one(rawstore_tmp_db):
    _freeze("swing", D1, ["AAA"])
    rawstore.write_rows("forecast_outcomes", [_outcome("swing", D1, "AAA", mfe=0.50, horizon=90)])
    g = _one(hit_rate.summary(vol_of={}.get))
    assert g["records"] == 0 and g["state"] == hit_rate.NO_CLOSED_WINDOW


def test_an_outcome_of_an_older_rule_version_is_not_read(rawstore_tmp_db):
    _freeze("swing", D1, ["AAA"])
    rawstore.write_rows("forecast_outcomes", [_outcome("swing", D1, "AAA", mfe=0.50, model_version="o0:old")])
    g = _one(hit_rate.summary(vol_of={}.get))
    assert g["records"] == 0


# ── the 90-day dedupe keeps the earliest record ──────────────────────────────

def test_the_same_ticker_within_ninety_days_keeps_only_the_earliest_record(rawstore_tmp_db):
    """The earliest is kept even when it is the worse record: here it is dirty and the later one is clean."""
    later = D1 + timedelta(days=89)
    _freeze("swing", D1, ["AAA"])
    _freeze("swing", later, ["AAA"])
    rawstore.write_rows("forecast_outcomes", [_outcome("swing", D1, "AAA", mfe=0.40, status=outcomes.SPLIT_CHECK),
                                              _outcome("swing", later, "AAA", mfe=0.50)])
    g = _one(hit_rate.summary(vol_of={}.get))
    assert (g["frozen"], g["duplicates"], g["records"]) == (1, 1, 0)
    assert g["dirty"] == [{"subject": "AAA", "forecast_date": D1, "reason": hit_rate.DIRTY_SPLIT}]


def test_ninety_days_apart_is_a_new_record(rawstore_tmp_db):
    later = D1 + timedelta(days=90)
    _freeze("swing", D1, ["AAA"])
    _freeze("swing", later, ["AAA"])
    rawstore.write_rows("forecast_outcomes", [_outcome("swing", D1, "AAA", mfe=0.01),
                                              _outcome("swing", later, "AAA", mfe=0.50)])
    g = _one(hit_rate.summary(vol_of={}.get))
    assert (g["frozen"], g["duplicates"], g["records"], g["freeze_dates"]) == (2, 0, 2, 2)


def test_the_window_is_anchored_on_the_kept_record_not_on_a_dropped_one(rawstore_tmp_db):
    """Kept on day 0, dropped on day 60, kept again on day 100: 100 days after the record that stayed."""
    days = [D1, D1 + timedelta(days=60), D1 + timedelta(days=100)]
    for d in days:
        _freeze("swing", d, ["AAA"])
    rawstore.write_rows("forecast_outcomes", [_outcome("swing", d, "AAA", mfe=0.01) for d in days])
    g = _one(hit_rate.summary(vol_of={}.get))
    assert (g["frozen"], g["duplicates"], g["records"]) == (2, 1, 2)


def test_the_dedupe_is_per_family(rawstore_tmp_db):
    _freeze("swing", D1, ["AAA"])
    _freeze("fundamentals", D2, ["AAA"])
    rawstore.write_rows("forecast_outcomes", [_outcome("swing", D1, "AAA", mfe=0.01),
                                              _outcome("fundamentals", D2, "AAA", mfe=0.01)])
    groups = hit_rate.summary(vol_of={}.get)
    assert _one(groups, "swing")["records"] == 1
    assert _one(groups, "fundamentals")["records"] == 1


def test_a_duplicate_does_not_count_as_a_frozen_date_of_its_own(rawstore_tmp_db):
    _freeze("swing", D1, ["AAA"])
    _freeze("swing", D2, ["AAA"])
    g = _one(hit_rate.summary(vol_of={}.get))
    assert (g["frozen"], g["frozen_dates"], g["duplicates"]) == (1, 1, 1)


# ── a model_version bump splits, never pools ─────────────────────────────────

def test_a_model_version_bump_is_a_group_of_its_own_and_restarts_the_clock(rawstore_tmp_db):
    vols = _board(model_version="v1+abcd")
    vols |= _board(days=(date(2026, 9, 22),), per_day=10, model_version="v2+ef01")
    groups = hit_rate.summary(vol_of=lambda t, d: vols.get((t, d)))
    old, new = _one(groups, model_version="v1+abcd"), _one(groups, model_version="v2+ef01")
    assert (old["records"], old["freeze_dates"], old["state"]) == (30, 3, hit_rate.GATE_MET)
    assert (new["records"], new["freeze_dates"], new["state"]) == (10, 1, hit_rate.GATE_NOT_MET)
    assert new["headline"] is None


def test_the_dedupe_does_not_reach_across_model_versions(rawstore_tmp_db):
    _freeze("swing", D1, ["AAA"], model_version="v1+abcd")
    _freeze("swing", D2, ["AAA"], model_version="v2+ef01")
    groups = hit_rate.summary(vol_of={}.get)
    assert _one(groups, model_version="v1+abcd")["frozen"] == 1
    assert _one(groups, model_version="v2+ef01")["frozen"] == 1


def test_groups_come_sorted_by_family_then_model_version(rawstore_tmp_db):
    _freeze("swing", D1, ["AAA"], model_version="v2")
    _freeze("swing", D2, ["BBB"], model_version="v1")
    _freeze("asymmetric_growth", D1, ["CCC"], model_version="v9")
    keys = [(g["family"], g["model_version"]) for g in hit_rate.summary(vol_of={}.get)]
    assert keys == [("asymmetric_growth", "v9"), ("swing", "v1"), ("swing", "v2")]


# ── the dirty-record list ────────────────────────────────────────────────────

def test_split_check_no_bars_and_a_closed_row_without_mfe_are_dirty_and_out_of_the_count(rawstore_tmp_db):
    _freeze("swing", D1, ["AAA", "BBB", "CCC", "DDD"])
    rawstore.write_rows("forecast_outcomes", [
        _outcome("swing", D1, "AAA", mfe=0.40, status=outcomes.SPLIT_CHECK),
        _outcome("swing", D1, "BBB", mfe=None, status=outcomes.NO_BARS),
        _outcome("swing", D1, "CCC", mfe=None, status=outcomes.CLOSED),
        _outcome("swing", D1, "DDD", mfe=0.01),
    ])
    g = _one(hit_rate.summary(vol_of={}.get))
    assert g["records"] == 1
    assert g["dirty"] == [
        {"subject": "AAA", "forecast_date": D1, "reason": hit_rate.DIRTY_SPLIT},
        {"subject": "BBB", "forecast_date": D1, "reason": hit_rate.DIRTY_NO_BARS},
        {"subject": "CCC", "forecast_date": D1, "reason": hit_rate.DIRTY_NO_MFE},
    ]


def test_a_dropped_duplicate_is_not_also_listed_as_dirty(rawstore_tmp_db):
    _freeze("swing", D1, ["AAA"])
    _freeze("swing", D2, ["AAA"])
    rawstore.write_rows("forecast_outcomes", [_outcome("swing", D1, "AAA", mfe=0.01),
                                              _outcome("swing", D2, "AAA", mfe=0.4, status=outcomes.SPLIT_CHECK)])
    assert _one(hit_rate.summary(vol_of={}.get))["dirty"] == []


# ── the baselines next to the headline ───────────────────────────────────────

def test_the_headline_carries_the_matched_peer_and_the_universe_rate(rawstore_tmp_db):
    vols = _board(universe_rate=0.30, peer_rate=0.50)
    head = _one(hit_rate.summary(vol_of=lambda t, d: vols.get((t, d))))["headline"]
    peers, universe = head["peers"], head["universe"]
    assert (peers["records"], peers["freeze_dates"]) == (30, 3)
    assert peers["rate"] == pytest.approx(0.40) and peers["baseline"] == pytest.approx(0.50)
    assert peers["gap"] == pytest.approx(-0.10)
    assert (universe["records"], universe["freeze_dates"]) == (30, 3)
    assert universe["baseline"] == pytest.approx(0.30) and universe["gap"] == pytest.approx(0.10)


def test_a_pick_is_matched_to_the_decile_its_own_vol_falls_in(rawstore_tmp_db):
    """Decile k covers [k-1, k): a vol of exactly 4.0 is decile 5 (lo inclusive), 3.99 is decile 4."""
    vols = _board(peer_rate=0.50)
    rates = [0.0] * 10
    rates[3], rates[4] = 0.20, 0.50
    rawstore.write_rows("forecast_baselines", [r for d in (D1, D2, D3) for r in _deciles(d, rates)])
    subjects = sorted(vols)
    for i, key in enumerate(subjects):
        vols[key] = 3.99 if i % 2 else 4.0
    peers = _one(hit_rate.summary(vol_of=lambda t, d: vols.get((t, d))))["headline"]["peers"]
    assert peers["baseline"] == pytest.approx((0.20 + 0.50) / 2)


def test_a_pick_without_a_vol_or_with_too_few_peers_leaves_the_peer_subset(rawstore_tmp_db):
    vols = _board()
    keys = sorted(vols)
    vols[keys[0]] = None                                       # no trailing vol: no peer group
    thin = [_baseline(D1, baselines.decile_basis(10), n=hit_rate.MIN_PEERS - 1, ge10=10, vol_lo=9.0)]
    rawstore.write_rows("forecast_baselines", thin)
    assert keys[1][1] == D1
    vols[keys[1]] = 50.0                                       # decile 10 of D1, which has 20 names
    g = _one(hit_rate.summary(vol_of=lambda t, d: vols.get((t, d))))
    peers = g["headline"]["peers"]
    assert peers["records"] == 28 and g["headline"]["records"] == 30
    assert peers["dropped"] == 2


def test_the_family_rate_beside_a_baseline_is_recomputed_on_that_baselines_subset(rawstore_tmp_db):
    """Drop the four hits of D1 from the peer subset: the peer comparison's own rate is 8 / 26, not 12 / 30."""
    vols = _board()
    for i in range(4):
        vols[(f"SW0{i:02d}", D1)] = None
    head = _one(hit_rate.summary(vol_of=lambda t, d: vols.get((t, d))))["headline"]
    assert head["rate"] == pytest.approx(12 / 30)
    assert head["peers"]["records"] == 26 and head["peers"]["rate"] == pytest.approx(8 / 26)


def test_an_open_or_older_version_baseline_row_is_not_a_baseline(rawstore_tmp_db):
    vols = _board()
    rawstore.write_rows("forecast_baselines", [
        _baseline(D1, baselines.UNIVERSE, n=1000, ge10=300, status=outcomes.OPEN),
        _baseline(D2, baselines.UNIVERSE, n=1000, ge10=300, model_version="b0:old"),
    ])
    universe = _one(hit_rate.summary(vol_of=lambda t, d: vols.get((t, d))))["headline"]["universe"]
    assert (universe["records"], universe["freeze_dates"]) == (10, 1)


def test_a_universe_row_with_no_snapshot_is_no_baseline(rawstore_tmp_db):
    """``n = 0`` is how HR-1 says "no membership snapshot that day" (the live 2026-10-06 row)."""
    vols = _board()
    rawstore.write_rows("forecast_baselines", [_baseline(D1, baselines.UNIVERSE, n=0, ge10=0)])
    universe = _one(hit_rate.summary(vol_of=lambda t, d: vols.get((t, d))))["headline"]["universe"]
    assert universe["records"] == 20


def test_no_baseline_at_all_still_shows_the_rate_with_an_empty_comparison(rawstore_tmp_db):
    vols = _board(with_baselines=False)
    head = _one(hit_rate.summary(vol_of=lambda t, d: vols.get((t, d))))["headline"]
    assert head["rate"] == pytest.approx(0.40)
    assert head["peers"] == {"records": 0, "freeze_dates": 0, "dropped": 30, "rate": None, "baseline": None,
                             "gap": None}


# ── the read is a read ───────────────────────────────────────────────────────

def test_summary_writes_nothing(rawstore_tmp_db, monkeypatch):
    vols = _board()

    def refuse(*a, **k):
        raise AssertionError("hit_rate wrote to the store")

    for name in ("write_rows", "execute", "update_columns", "replace_all", "delete", "clear"):
        monkeypatch.setattr(rawstore, name, refuse)
    assert _one(hit_rate.summary(vol_of=lambda t, d: vols.get((t, d))))["state"] == hit_rate.GATE_MET


def test_the_default_vol_is_the_baselines_trailing_vol_from_the_stored_bars(rawstore_tmp_db):
    """No ``vol_of``: the pick's vol is read from the adjusted bars with the rule HR-1 places its peers by."""
    from RevenueChaser.core import calendar as cal

    rows, closes, d = [], [], D1 - timedelta(days=60)
    i = 0
    while d <= D1 + timedelta(days=5):
        if cal.is_trading_day(d):
            c = 100.0 * (1.05 if i % 2 else 1.0)
            i += 1
            if d <= D1:
                closes.append(c)
            rows.append({"ticker": "AAA", "session_date": d, "open": c, "high": c, "low": c, "close": c,
                         "adj_close": None, "volume": 1000.0, "split_ratio": None, "adj_source": None,
                         "source": "yfinance", "fetched_at": 0.0, "run_id": "seed", "run_at": 0.0})
        d += timedelta(days=1)
    rawstore.write_rows("price_bars_daily", rows)
    got = hit_rate.trailing_vols([("AAA", D1), ("ZZZ", D1)])
    assert got[("AAA", D1)] == pytest.approx(baselines.realised_vol(closes))
    assert got[("ZZZ", D1)] is None
