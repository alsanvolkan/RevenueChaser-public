"""Every MANIFEST table declares a history mode, and the 22 G1 tables match their frozen DDL (#281, #292).

``history_mode`` is one of five values (data backbone review ADR-1; w4_manifest_results.md Part A):
event, session_snapshot, change_history, latest_cache, operational. It is a label only: rawstore does
not read it yet, so adding it changes no behaviour. ``test_g1_entries_match_frozen_ddl`` pins the 22 tables
added by the schema bump #292 to a frozen copy of the reviewed DDL (``ddl292_frozen.py``), because
``test_schema_v10_tables.py`` builds its expected rows from the MANIFEST and so cannot see DDL drift.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from RevenueChaser.core import rawstore

_spec = importlib.util.spec_from_file_location("ddl292_frozen", Path(__file__).with_name("ddl292_frozen.py"))
ddl292_frozen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ddl292_frozen)

HISTORY_MODES = {"event", "session_snapshot", "change_history", "latest_cache", "operational"}

# The 69 tables that existed before G1, with the mode agreed in Part A (rows 1-69).
LEGACY_MODES = {
    "event": (
        "edgar_financials",
        "edgar_documents",
        "edgar_facts",
        "comov_runs",
        "comov_nodes",
        "comov_members",
        "comov_lag",
        "migration_manifest",
        "ticker_themes",
        "llm_cache",
        "theme_runs",
        "relatedness_themes",
        "relatedness_theme_tags",
        "relatedness_theme_members",
        "relatedness_theme_etfs",
        "leaps_flow_prints",
        "sec13f_submissions",
        "sec13f_holdings",
        "sec13f_ingested_zips",
        "sec13f_edgar_backfill",
        "identity_runs",
        "job_runs",
        "price_bars_daily",
        "orphan_lookups",       # 06 §4 (schema 11): event, keyed (ticker, session_date)
        "discord_messages",
        "discord_export_runs",
    ),
    "session_snapshot": (
        "ticker_payload_cache",
        "swing_info",
        "sec13f_enrichment",
        "rotation_snapshots",
        "leaps_daily_scores",
        "forecast_ledger",
    ),
    "change_history": (
        "equity_universe",
        "sec_company_tickers",
        "sec_company_sic",
        "watchlist",
        "sec13f_security_meta",
        "cusip_ticker",
        "sec13f_ticker_names",
        "index_constituents",
        "security_master",
        "identity_conflicts",
        "identity_source_cache",
        "board_settings",
        "discord_channels",
        "catalyst_events",      # 06 §4 (schema 11): change_history; its writers upsert, the store records the change
        "orphan_designations",
        "etf_holdings",         # #303 (D25, design C): was session_snapshot; the store records each change
    ),
    "latest_cache": (
        "leaps_cache",
        "rotation_holdings",
        "equity_universe_provenance",
        "chain_filings",
        "chain_mentions",
        "chain_meta",
        "sec13f_totals",
        "store_meta",
        "run_registry",
        "fundamentals_scores",
        "fundamentals_gates",
        "swing_scores",
        "swing_gates",
        "relatedness_profiles",
        "relatedness_attributes",
        "relatedness_scores",
        "swing_ohlc",
        "ticker_scores",
        "sec13f_cusip_names",
        "oipd_chain",
        "oipd_meta",
    ),
}
LEGACY = {name: mode for mode, names in LEGACY_MODES.items() for name in names}


def test_every_entry_has_one_of_the_five_modes():
    bad = {n: s.get("history_mode") for n, s in rawstore.MANIFEST.items()
           if s.get("history_mode") not in HISTORY_MODES}
    assert not bad, f"entries without a valid history_mode: {bad}"


def test_mode_list_in_rawstore_matches_the_five_values():
    assert set(rawstore.HISTORY_MODES) == HISTORY_MODES


# The 25 tables schema 11 adds (history redesign C): 8 change tables in raw.duckdb, result_runs and 16 run-keyed row
# tables in history.duckdb. Pinned by name, so a table added or dropped by accident fails here.
V11_CHANGE_TABLES = tuple(f"{t}_changes" for t in (
    "equity_universe", "security_master", "identity_source_cache", "index_constituents", "watchlist",
    "board_settings", "catalyst_events", "orphan_designations"))
V11_HISTORY_TABLES = (
    "result_runs", "ag_scores_history", "swing_scores_history", "swing_gates_history", "fundamentals_scores_history",
    "fundamentals_gates_history", "relatedness_scores_history", "value_chain_edges_history", "oipd_chain_history",
    "earnings_cards_history", "leaps_footprint_history", "oipd_fit_history", "rotation_holdings_history",
    "sector_tree_history", "money_flow_history", "sec13f_metrics_history", "financials_report_history")
V11 = V11_CHANGE_TABLES + V11_HISTORY_TABLES
# Change tables added after schema 11 without a bump (design C: one CHANGES line is an addition, report §14.3).
LATER_CHANGE_TABLES = ("etf_holdings_changes",)    # #303
# History tables added after schema 11 without a bump: a new run-keyed table is an addition (avo, #374 card).
LATER_HISTORY_TABLES = ("rotation_snapshots_history",)
# Pre-existing append-only table flagged history later (#286): the guard rides the flag, no schema change.
FORECAST_LEDGER = ("forecast_ledger",)
# Added with prices.duckdb (#156, avo 2026-10-09), no schema bump: the price watermarks beside the bars.
PRICES_DB = ("price_checks",)
# Derived tables added after schema 11 without a bump (an additive MANIFEST table, #380 S0): rebuilt from the store.
LATER_DERIVED_TABLES = ("forecast_outcomes",)


def test_legacy_table_list_is_the_69_pre_g1_tables():
    assert len(LEGACY) == 69
    assert len(V11) == 25
    assert set(LEGACY) | set(ddl292_frozen.ENTRIES) | set(V11) | set(LATER_CHANGE_TABLES) | set(PRICES_DB) \
        | set(LATER_HISTORY_TABLES) | set(LATER_DERIVED_TABLES) == set(rawstore.MANIFEST)
    assert not set(LATER_DERIVED_TABLES) & (set(LEGACY) | set(ddl292_frozen.ENTRIES) | set(V11) | set(PRICES_DB))
    assert all(rawstore.MANIFEST[t]["history_mode"] == "latest_cache" for t in LATER_DERIVED_TABLES)
    assert not set(PRICES_DB) & (set(LEGACY) | set(ddl292_frozen.ENTRIES) | set(V11))
    assert rawstore.MANIFEST["price_checks"]["history_mode"] == "operational"
    assert not set(LEGACY) & set(ddl292_frozen.ENTRIES)
    assert not set(V11) & (set(LEGACY) | set(ddl292_frozen.ENTRIES))
    assert not set(LATER_CHANGE_TABLES) & (set(LEGACY) | set(ddl292_frozen.ENTRIES) | set(V11))


@pytest.mark.parametrize("name", sorted(LEGACY))
def test_legacy_table_mode(name):
    assert rawstore.MANIFEST[name]["history_mode"] == LEGACY[name]


def test_legacy_mode_tally():
    tally = {m: len(v) for m, v in LEGACY_MODES.items()}
    assert tally == {"event": 26, "session_snapshot": 6, "change_history": 16, "latest_cache": 21}   # #303 moved one


def test_only_the_history_tables_are_flagged_history():
    """``history`` (the append-only guard, #286) is on the 25 schema-11 tables, the later change and history tables,
    ``forecast_ledger`` (#286) and on nothing else."""
    assert sorted(n for n, s in rawstore.MANIFEST.items() if s.get("history")) == \
        sorted(V11 + LATER_CHANGE_TABLES + LATER_HISTORY_TABLES + FORECAST_LEDGER)


def test_frozen_copy_has_22_entries():
    assert len(ddl292_frozen.ENTRIES) == 22


@pytest.mark.parametrize("name", sorted(ddl292_frozen.ENTRIES))
def test_g1_entries_match_frozen_ddl(name):
    """The whole entry (key, columns in order, compress, kind, write, history_mode, db, sources) is the reviewed one."""
    assert rawstore.MANIFEST[name] == ddl292_frozen.ENTRIES[name]
