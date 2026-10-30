"""board_settings — overrides in the store, defaults always from config (#40)."""
from __future__ import annotations

import pytest

from RevenueChaser.core import board_settings as bs
from RevenueChaser.core import rawstore
from RevenueChaser.module_comovement import config

B = "module_comovement"


def test_default_comes_from_config_at_call_time(rawstore_tmp_db, monkeypatch):
    assert bs.get(B, "WINSOR_K") == config.WINSOR_K
    monkeypatch.setattr(config, "WINSOR_K", 6.0)
    assert bs.get(B, "WINSOR_K") == 6.0          # not copied at import or on first read
    assert bs.values(B)["overridden"] == []


def test_save_then_get_and_values(rawstore_tmp_db):
    out = bs.save(B, {"WINSOR_K": 6.5, "MIN_OBS": 100})
    assert bs.get(B, "WINSOR_K") == 6.5
    assert bs.get(B, "MIN_OBS") == 100 and isinstance(bs.get(B, "MIN_OBS"), int)
    assert out["overridden"] == ["MIN_OBS", "WINSOR_K"]
    v = bs.values(B)
    assert v["values"]["WINSOR_K"] == 6.5
    assert v["values"]["WINDOW"] == config.WINDOW            # untouched key = default
    assert v["defaults"]["WINSOR_K"] == config.WINSOR_K
    assert rawstore.count("board_settings") == 2


def test_saving_the_default_value_drops_the_override(rawstore_tmp_db):
    bs.save(B, {"WINSOR_K": 6.5})
    bs.save(B, {"WINSOR_K": config.WINSOR_K})
    assert rawstore.count("board_settings") == 0
    assert not bs.is_custom(B)


def test_is_custom(rawstore_tmp_db):
    assert bs.is_custom(B) is False
    bs.save(B, {"BENCH_STALE_SESSIONS": 4})
    assert bs.is_custom(B) is True


def test_reset_deletes_rows_so_defaults_follow_config(rawstore_tmp_db, monkeypatch):
    bs.save(B, {"WINSOR_K": 6.5, "MIN_OBS": 100})
    assert rawstore.count("board_settings") == 2
    bs.reset(B)
    assert rawstore.count("board_settings") == 0     # deleted, not rewritten as defaults
    assert not bs.is_custom(B)
    monkeypatch.setattr(config, "WINSOR_K", 7.0)
    assert bs.get(B, "WINSOR_K") == 7.0


def test_reset_some_keys_only(rawstore_tmp_db):
    bs.save(B, {"WINSOR_K": 6.5, "MIN_OBS": 100})
    bs.reset(B, ["WINSOR_K"])
    assert bs.values(B)["overridden"] == ["MIN_OBS"]


@pytest.mark.parametrize("change,needle", [
    ({"WINDOW": 100}, "WINDOW must be one of 21, 42, 126, 189, 252, 378"),
    ({"MIN_OBS": 59}, "MIN_OBS must be at least 60"),
    ({"MIN_OBS": 300}, "MIN_OBS must be between 60 and WINDOW (252)"),
    ({"OOS_HOLDOUT": 41}, "OOS_HOLDOUT must be between 42 and 126"),
    ({"OOS_HOLDOUT": 127}, "OOS_HOLDOUT must be between 42 and 126"),
    ({"WINSOR_K": 3.9}, "WINSOR_K must be between 4 and 8"),
    ({"WINSOR_K": 8.1}, "WINSOR_K must be between 4 and 8"),
    ({"MIN_PERIODS": 19}, "MIN_PERIODS must be at least 20"),
    ({"BENCH_STALE_SESSIONS": -1}, "BENCH_STALE_SESSIONS must be between 0 and 5"),
    ({"BENCH_STALE_SESSIONS": 6}, "BENCH_STALE_SESSIONS must be between 0 and 5"),
    ({"MIN_OBS": 63.5}, "MIN_OBS must be a whole number"),
    ({"WINSOR_K": "abc"}, "WINSOR_K must be a number"),
    ({"MIN_OBS": True}, "MIN_OBS must be a whole number"),
    ({"NOPE": 1}, "unknown setting 'NOPE'"),
])
def test_validate_every_bound_names_its_rule(rawstore_tmp_db, change, needle):
    msgs = bs.validate(B, change)
    assert any(needle in m for m in msgs), msgs


def test_cross_rule_min_periods_not_above_min_obs(rawstore_tmp_db):
    msgs = bs.validate(B, {"MIN_OBS": 80, "MIN_PERIODS": 90})
    assert any("MIN_PERIODS must be between 20 and MIN_OBS (80)" in m for m in msgs), msgs
    assert bs.validate(B, {"MIN_OBS": 80, "MIN_PERIODS": 80}) == []


def test_cross_rule_min_obs_is_checked_against_the_merged_window(rawstore_tmp_db):
    bs.save(B, {"WINDOW": 189})
    msgs = bs.validate(B, {"MIN_OBS": 200})
    assert any("MIN_OBS must be between 60 and WINDOW (189)" in m for m in msgs), msgs
    assert bs.validate(B, {"WINDOW": 378, "MIN_OBS": 200}) == []


def test_cross_rule_window_plus_holdout_fits_stored_sessions(rawstore_tmp_db):
    # 504 when SPY is not stored: the widest allowed pair (378 + 126) fits exactly
    assert bs.stored_sessions() == 504
    assert bs.validate(B, {"WINDOW": 378, "OOS_HOLDOUT": 126}) == []


def test_stored_sessions_reads_spy_from_the_prices_store(rawstore_tmp_db):
    """Co-movement reads the prices store since PR 8 (D01 since #379 before), so the rule counts SPY's sessions
    there, not swing_ohlc's nor D01's."""
    from RevenueChaser.core.tests.d01_seed import seed_d01
    from RevenueChaser.rcdata.tests import prices_store
    prices_store.put_bars([{"ticker": "SPY", "session_date": f"2025-01-{d:02d}", "close": 1.0} for d in range(1, 11)])
    seed_d01("SPY", [f"2025-02-{d:02d}" for d in range(1, 21)], 1.0)                 # the old D01: not counted
    rawstore.write_rows("swing_ohlc", [{"ticker": "SPY", "ts": f"2024-02-{d:02d}T00:00:00", "open": 1.0,
                                        "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0, "cached_at": 1.0}
                                       for d in range(1, 21)])
    assert bs.stored_sessions() == 10
    msgs = bs.validate(B, {"OOS_HOLDOUT": 63})
    assert any("WINDOW + OOS_HOLDOUT must not exceed the 10 stored sessions" in m
               for m in msgs), msgs


def test_short_default_window_skips_the_long_window_rules(rawstore_tmp_db):
    assert bs.validate(B, {"WINDOW": 21}) == []
    bs.save(B, {"WINDOW": 21})
    assert bs.get(B, "WINDOW") == 21


def test_save_rejects_and_writes_nothing(rawstore_tmp_db):
    with pytest.raises(bs.SettingsError) as exc:
        bs.save(B, {"WINSOR_K": 6.0, "MIN_OBS": 1})
    assert any("MIN_OBS" in m for m in exc.value.messages)
    assert rawstore.count("board_settings") == 0     # all-or-nothing


def test_empty_change_set_is_invalid(rawstore_tmp_db):
    assert bs.validate(B, {}) and bs.validate(B, None)


def test_unknown_board_raises(rawstore_tmp_db):
    with pytest.raises(KeyError):
        bs.values("module_nope")
    assert bs.boards() == ["module_comovement", "module_fundamentals", "module_swing_potential"]


def test_describe_lists_every_field_with_value_default_and_override_flag(rawstore_tmp_db):
    bs.save(B, {"MIN_OBS": 100})
    rows = {r["key"]: r for r in bs.describe(B)}
    assert set(rows) == {"WINDOW", "MIN_OBS", "OOS_HOLDOUT", "WINSOR_K",
                         "MIN_PERIODS", "BENCH_STALE_SESSIONS", "OPERATING_ONLY"}
    m = rows["MIN_OBS"]
    assert m["value"] == 100 and m["default"] == config.MIN_OBS
    assert m["overridden"] is True and rows["WINSOR_K"]["overridden"] is False
    assert m["kind"] == "int" and m["lo"] == config.MIN_OBS_LO and m["hi"] is None
    assert rows["WINDOW"]["choices"] == list(config.WINDOW_OPTIONS)
    assert rows["WINSOR_K"]["kind"] == "float"
    assert rows["OOS_HOLDOUT"]["lo"] == config.OOS_HOLDOUT_LO
    assert rows["OOS_HOLDOUT"]["hi"] == config.OOS_HOLDOUT_HI


# ── PR 2: the type filter and Fundamentals' thresholds are settings ──────────────────────────────
@pytest.mark.parametrize("board,default", [("module_comovement", 0), ("module_swing_potential", 1),
                                           ("module_fundamentals", 1)])
def test_every_screening_board_has_a_type_filter_setting_whose_default_keeps_its_old_set(
        rawstore_tmp_db, board, default):
    from RevenueChaser.core.universe import BOARD_OPERATING_ONLY
    assert bs.get(board, "OPERATING_ONLY") == default == int(BOARD_OPERATING_ONLY[board])
    row = next(r for r in bs.describe(board) if r["key"] == "OPERATING_ONLY")
    assert row["kind"] == "int" and row["choices"] == [0, 1]
    assert bs.validate(board, {"OPERATING_ONLY": 2}) == ["OPERATING_ONLY must be one of 0, 1; got 2"]
    bs.save(board, {"OPERATING_ONLY": 1 - default})
    assert bs.get(board, "OPERATING_ONLY") == 1 - default


def test_fundamentals_price_and_volume_floors_are_settings_with_todays_defaults(rawstore_tmp_db):
    from RevenueChaser.module_fundamentals import config as fcfg
    f = "module_fundamentals"
    assert bs.get(f, "MIN_PRICE") == fcfg.MIN_PRICE == 5.0
    assert bs.get(f, "MIN_DOLLAR_VOLUME") == fcfg.MIN_DOLLAR_VOLUME == 250_000
    bs.save(f, {"MIN_PRICE": 0.5, "MIN_DOLLAR_VOLUME": 0})        # a user may switch the floor off
    assert bs.get(f, "MIN_PRICE") == 0.5 and bs.get(f, "MIN_DOLLAR_VOLUME") == 0
    assert bs.validate(f, {"MIN_PRICE": -1})
