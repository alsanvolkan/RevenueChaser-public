"""The fundamentals kind (build plan PR 12): registered at serving schema version 1, every dataset starts by hand,
``fundamentals/tables`` builds last, and a build with no source yet serves the spine's Tier A companies."""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.rcdata import caps, ops, serving
from RevenueChaser.rcdata.build import DATASETS, due
from RevenueChaser.rcdata.build.fundamentals import KIND, tables

from . import fund_fixture as fx

FUND = [n for n, d in DATASETS.items() if d.kind == KIND]


def test_the_fundamentals_kind_is_served_at_version_1():
    assert serving.SERVING_SCHEMA_VERSION["fundamentals"] == 1 and "fundamentals" in serving.KINDS


def test_every_fundamentals_dataset_starts_by_hand_and_the_served_tables_build_last():
    assert FUND and FUND[-1] == tables.NAME
    assert all(getattr(DATASETS[n], "starts_by_hand", False) for n in FUND)
    assert all(due.is_due(DATASETS[n])[0] is False for n in FUND)            # not before a first CLI run


def test_the_day_caps_name_the_new_providers():
    assert caps.DAY_CAPS["alphavantage"] == 25 and caps.DAY_CAPS["yfinance"] > 0 and caps.DAY_CAPS["sec"] == 40_000


def test_the_alpha_vantage_key_is_declared_by_name_only():
    from RevenueChaser.core import credentials
    assert credentials.PROVIDERS["alphavantage"].env_var == "ALPHAVANTAGE_API_KEY"


def test_a_build_with_no_source_yet_serves_the_tier_a_companies():
    fx.spine()
    check = fx.run(tables.NAME, "2026-10-28")
    assert check["result"] in ("changed", "unchanged"), check["note"]
    assert set(tables.TABLES) <= {r[0] for r in fx.q("SELECT table_name FROM duckdb_tables()")}
    rows = fx.q("SELECT ticker, cik, gvkey, gsubind, spine_as_of, source FROM fundamental_company ORDER BY ticker")
    assert [r[0] for r in rows] == fx.TIER_A                                 # WFC-PC is Tier B
    assert rows[0] == ("HEI", "46619", "005567", "20101010", date(2026, 10, 27), "spine/security")
    assert fx.q("SELECT kind, version FROM serving_schema_version") == [("fundamentals", 1)]


def test_no_spine_yet_builds_empty_tables_not_an_error():
    check = fx.run(tables.NAME, "2026-10-28")
    assert check["result"] == "unchanged" and fx.q("SELECT count(*) FROM fundamental_company") == [(0,)]


def test_the_spine_going_missing_refuses_the_swap_and_keeps_the_live_file():
    fx.spine()
    fx.run(tables.NAME, "2026-10-28")
    serving.live_path("spine").unlink()
    check = fx.run(tables.NAME, "2026-10-29")
    assert check["result"] == "failed" and "empties" in check["note"]
    assert fx.q("SELECT count(*) FROM fundamental_company") == [(len(fx.TIER_A),)]


@pytest.mark.parametrize("name", FUND)
def test_a_paused_dataset_sends_nothing(name):
    ops.schedule_pause(name, by="test")
    assert fx.run(name, "2026-10-28")["result"] == "skipped"
