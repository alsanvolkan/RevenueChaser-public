"""Schema 7 -> 8 (#164): the stack adds tables and columns, nothing is rewritten.

The v7 store is built from a v7 MANIFEST (without the v8 tables and columns) and filled
through ``write_rows``; then the real MANIFEST is restored and ``ensure_all_tables``
runs again, the way the dashboard's first store call does after the merge.
"""
from __future__ import annotations

import datetime as _dt

import duckdb
import pytest

from RevenueChaser.core import rawstore

V8_RAW_TABLES = ("job_runs", "price_bars_daily", "catalyst_events", "orphan_designations",
                 "orphan_lookups", "forecast_ledger")
V8_EDGAR_COLUMNS = {"edgar_financials": ("fetched_at",)}
_ANNUAL = [{"fy": 2025, "revenue": 1.5e9}, {"fy": 2024, "revenue": 1.2e9}]
_META = {"cik": "0000320193", "name": "Apple Inc."}


def _forget_reconciliation():
    # A second ensure in the same process is a no-op unless the memos are reset.
    rawstore._TABLE_READY.clear()
    rawstore._ENSURED.clear()


def _snapshot(path):
    """{table: sorted [(column, type)]} of one store file, read-only."""
    con = duckdb.connect(str(path), read_only=True)
    try:
        rows = con.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position").fetchall()
    finally:
        con.close()
    out: dict[str, list] = {}
    for t, c, ty in rows:
        out.setdefault(t, []).append((c, ty))
    return out


def _columns(path, table):
    return {c for c, _ in _snapshot(path).get(table, [])}


@pytest.fixture
def v7_store(isolate_rawstore):
    """A populated v7 store (raw + edgar + discord files); yields the content to compare."""
    mp = pytest.MonkeyPatch()
    try:  # undo even when a v8 name is missing, or the MANIFEST leaks into later tests
        for t in V8_RAW_TABLES:
            mp.delitem(rawstore.MANIFEST, t)
        for t, cols in V8_EDGAR_COLUMNS.items():
            spec = rawstore.MANIFEST[t]
            mp.setitem(rawstore.MANIFEST, t, {
                **spec, "columns": [c for c in spec["columns"] if c.split()[0] not in cols]})
        _forget_reconciliation()
        rawstore.ensure_all_tables()
        rawstore.write_rows("board_settings", [{"board": "swing", "key": "k", "value": 1.5,
                                                "updated_at": "2026-10-01"}])
        rawstore.write_rows("edgar_financials", [{"ticker": "AAPL", "annual": _ANNUAL,
                                                  "quarterly": [], "meta": _META}])
    finally:
        mp.undo()
    assert "job_runs" not in _snapshot(rawstore.DB_PATH)
    assert "fetched_at" not in _columns(rawstore.edgar_db_path(), "edgar_financials")
    yield {
        "board_settings": rawstore.get("board_settings", ("swing", "k")),
        "edgar_financials": rawstore.get("edgar_financials", "AAPL"),
    }


def _migrate():
    _forget_reconciliation()
    rawstore.ensure_all_tables()


def test_schema_version_label_moved_on_to_11_with_the_history_tables():
    """The v8 tables below still migrate; the label itself is 11 since the history redesign C (25 tables)."""
    assert rawstore.schema_version() == "11"


def test_v7_store_gains_job_runs_and_fetched_at_and_keeps_every_row(v7_store):
    _migrate()
    assert {"run_id", "job_id", "module", "lane", "trigger", "source", "pid", "registry_name",
            "params", "queued_at", "started_at", "finished_at", "status", "error", "message",
            "counts"} <= _columns(rawstore.DB_PATH, "job_runs")
    assert "fetched_at" in _columns(rawstore.edgar_db_path(), "edgar_financials")
    assert {"ticker", "session_date", "open", "high", "low", "close", "adj_close", "volume",
            "split_ratio", "source", "fetched_at"} <= _columns(rawstore.prices_db_path(), "price_bars_daily")
    for t in ("catalyst_events", "orphan_designations", "orphan_lookups", "forecast_ledger"):
        assert _columns(rawstore.DB_PATH, t), t

    assert rawstore.count("board_settings") == 1
    assert rawstore.count("edgar_financials") == 1
    assert rawstore.get("board_settings", ("swing", "k")) == v7_store["board_settings"]
    edgar = rawstore.get("edgar_financials", "AAPL")
    assert edgar["annual"] == _ANNUAL and edgar["meta"] == _META  # decompressed, intact
    assert edgar["fetched_at"] is None
    for name, was in v7_store["edgar_financials"].items():
        assert edgar[name] == was


def test_job_runs_exists_only_in_the_raw_file(v7_store):
    _migrate()
    assert "job_runs" in _snapshot(rawstore.DB_PATH)
    assert "job_runs" not in _snapshot(rawstore.edgar_db_path())
    assert "job_runs" not in _snapshot(rawstore.discord_db_path())


def test_a_second_ensure_changes_nothing(v7_store):
    _migrate()
    files = (rawstore.DB_PATH, rawstore.edgar_db_path(), rawstore.discord_db_path())
    before = [_snapshot(p) for p in files]
    counts = (rawstore.count("board_settings"), rawstore.count("edgar_financials"))
    _migrate()
    assert [_snapshot(p) for p in files] == before
    assert (rawstore.count("board_settings"), rawstore.count("edgar_financials")) == counts


def test_job_runs_upsert_replaces_the_row_for_a_run_id(rawstore_tmp_db):
    base = {"run_id": "r1", "job_id": "j1", "module": "swing", "lane": "short",
            "trigger": "manual", "source": "dashboard", "pid": 1, "registry_name": None,
            "params": {"force": True}, "queued_at": 0.5, "started_at": 1.0,
            "finished_at": None, "status": "running", "error": None, "message": None,
            "counts": {"rows": 0}}
    rawstore.write_rows("job_runs", [base])
    rawstore.write_rows("job_runs", [{**base, "finished_at": 2.0, "status": "done",
                                      "counts": {"rows": 5}}])
    assert rawstore.count("job_runs") == 1
    row = rawstore.get("job_runs", "r1")
    assert row["status"] == "done" and row["finished_at"] == 2.0
    assert row["params"] == {"force": True} and row["queued_at"] == 0.5


# Column types the v8 tables must keep: a JSON column read back as TEXT (or a DATE as TEXT)
# changes what get() returns and how writers must serialise.
V8_COLUMN_TYPES = {
    "job_runs": {"params": "JSON", "counts": "JSON", "queued_at": "DOUBLE", "pid": "BIGINT"},
    "price_bars_daily": {"session_date": "DATE", "split_ratio": "DOUBLE", "fetched_at": "DOUBLE"},
    "catalyst_events": {"event_date": "DATE", "fetched_at": "DOUBLE"},
    "orphan_designations": {"date_designated": "DATE", "marketing_approval_date": "DATE",
                            "approvals": "JSON"},
    "orphan_lookups": {"candidates": "JSON", "n_rows": "INTEGER"},
    "forecast_ledger": {"forecast_date": "DATE", "target_date": "DATE", "probs": "JSON",
                        "rank": "INTEGER", "score": "DOUBLE"},
}


def test_v8_tables_keep_their_column_types(v7_store):
    _migrate()
    snap = {**_snapshot(rawstore.DB_PATH), **_snapshot(rawstore.prices_db_path())}   # price_bars_daily: #156
    for table, expected in V8_COLUMN_TYPES.items():
        have = dict(snap[table])
        for col, want in expected.items():
            assert have[col] == want, f"{table}.{col}: {have[col]} != {want}"
    edgar = dict(_snapshot(rawstore.edgar_db_path())["edgar_financials"])
    assert edgar["fetched_at"] == "DOUBLE"


# ── price_bars_daily (#164b) ─────────────────────────────────────────────────
def _bar(ticker, d, close, **kw):
    row = {"ticker": ticker, "session_date": d, "open": close, "high": close, "low": close,
           "close": close, "adj_close": close, "volume": 100.0, "split_ratio": None,
           "source": "yfinance", "fetched_at": 1.0}
    row.update(kw)
    return row


def test_price_bars_append_keeps_the_rest_of_a_tickers_history(rawstore_tmp_db):
    # 300 rows go through the bulk write path (text cast to DATE).
    start = _dt.date(2026, 1, 1)
    history = [_bar("T", (start + _dt.timedelta(days=i)).isoformat(), float(i)) for i in range(300)]
    rawstore.write_rows("price_bars_daily", history)
    batch = [_bar("T", (start + _dt.timedelta(days=i)).isoformat(), 999.0, fetched_at=2.0)
             for i in range(295, 300)]
    rawstore.write_rows("price_bars_daily", batch)
    assert rawstore.count("price_bars_daily") == 300
    row = rawstore.get("price_bars_daily", ("T", (start + _dt.timedelta(days=299)).isoformat()))
    assert row["close"] == 299.0 and row["fetched_at"] == 1.0      # append (schema 11): the first write of a key wins
    assert row["session_date"] == start + _dt.timedelta(days=299)


def test_price_bars_session_date_reads_back_as_a_date_and_split_ratio_is_nullable(rawstore_tmp_db):
    rawstore.write_rows("price_bars_daily", [
        _bar("T", "2026-09-30", 10.0),
        _bar("S", "2026-09-30", 5.0, split_ratio=2.0)])
    row = rawstore.get("price_bars_daily", ("T", "2026-09-30"))
    assert row["session_date"] == _dt.date(2026, 9, 30)
    assert row["split_ratio"] is None
    assert rawstore.get("price_bars_daily", ("S", "2026-09-30"))["split_ratio"] == 2.0


# ── catalyst_events, orphan_designations, orphan_lookups (#164c) ─────────────
def _event(**kw):
    row = {"ticker": "AMGN", "event_date": "2026-11-15", "date_precision": "day",
           "kind": "trial_readout", "title": "Phase 3 topline", "source": "ctgov",
           "source_event_id": "NCT01234567:primary_completion",
           "source_url": "https://clinicaltrials.gov/study/NCT01234567",
           "confidence": "sponsor_entered", "nct_id": "NCT01234567", "fetched_at": 1.0}
    row.update(kw)
    return row


def test_catalyst_event_date_slip_leaves_one_row_with_the_new_date(rawstore_tmp_db):
    rawstore.write_rows("catalyst_events", [_event()])
    rawstore.write_rows("catalyst_events", [_event(event_date="2027-02-01", fetched_at=2.0)])
    assert rawstore.count("catalyst_events") == 1
    row = rawstore.get("catalyst_events", ("ctgov", "NCT01234567:primary_completion"))
    assert row["event_date"] == _dt.date(2027, 2, 1)


def _designation(**kw):
    row = {"ticker": "AMGN", "generic_name": "blinatumomab",
           "designation": "Treatment of acute lymphoblastic leukemia",
           "date_designated": "2008-11-20", "designation_status": "Designated",
           "approval_status": None, "marketing_approval_date": "2014-12-03",
           "trade_name": "Blincyto",
           "approvals": [{"marketing_approval_date": "2014-12-03", "trade_name": "Blincyto"},
                         {"marketing_approval_date": "2017-07-11", "trade_name": "Blincyto"}],
           "sponsor": "Amgen Inc.", "fetched_at": 1.0}
    row.update(kw)
    return row


_DESIGNATION_KEY = ("AMGN", "blinatumomab", "Treatment of acute lymphoblastic leukemia", "2008-11-20")


def test_orphan_designation_upsert_is_idempotent(rawstore_tmp_db):
    rawstore.write_rows("orphan_designations", [_designation()])
    rawstore.write_rows("orphan_designations", [_designation(fetched_at=2.0)])
    assert rawstore.count("orphan_designations") == 1
    assert rawstore.get("orphan_designations", _DESIGNATION_KEY)["fetched_at"] == 2.0


def test_orphan_designation_with_approvals_round_trips(rawstore_tmp_db):
    rawstore.write_rows("orphan_designations", [_designation()])
    row = rawstore.get("orphan_designations", _DESIGNATION_KEY)
    assert row["approval_status"] is None
    assert row["marketing_approval_date"] == _dt.date(2014, 12, 3)
    assert row["date_designated"] == _dt.date(2008, 11, 20)
    assert row["trade_name"] == "Blincyto"
    assert row["approvals"] == _designation()["approvals"]


def test_a_null_date_designated_makes_the_whole_batch_raise(rawstore_tmp_db):
    # Pins the failure mode the #190 writer must guard: it skips and reports such rows.
    batch = [_designation(), _designation(generic_name="apremilast", date_designated=None)]
    with pytest.raises(duckdb.Error):
        rawstore.write_rows("orphan_designations", batch)
    assert rawstore.count("orphan_designations") == 0


def _lookup(ticker, match, **kw):
    row = {"ticker": ticker, "session_date": "2026-10-06", "sec_name": f"{ticker} Inc.", "query": ticker, "match": match,
           "provider_status": "ok", "n_rows": 0, "candidates": [], "error": None,
           "fetched_at": 1.0}
    row.update(kw)
    return row


def test_orphan_lookups_keeps_one_row_per_ticker_and_session_and_a_later_lookup_adds_one(rawstore_tmp_db):
    rawstore.write_rows("orphan_lookups", [
        _lookup("AMGN", "matched", n_rows=25),
        _lookup("XYZ", "unmatched", candidates=["XYZ Pharma", "XYZ Bio"]),
        _lookup("NONE", "no_hits"),
        _lookup("ERR", "failed", provider_status=None, error="unrecognised_page"),
    ])
    assert rawstore.count("orphan_lookups") == 4
    assert rawstore.get("orphan_lookups", ("XYZ", "2026-10-06"))["candidates"] == ["XYZ Pharma", "XYZ Bio"]
    assert rawstore.get("orphan_lookups", ("ERR", "2026-10-06"))["error"] == "unrecognised_page"
    # schema 11 (append, key (ticker, session_date)): the same session keeps its first answer ...
    rawstore.write_rows("orphan_lookups", [_lookup("ERR", "matched", n_rows=3, fetched_at=2.0)])
    assert rawstore.count("orphan_lookups") == 4
    assert rawstore.get("orphan_lookups", ("ERR", "2026-10-06"))["match"] == "failed"
    # ... and a later session adds a row, so each day's lookup outcome is kept.
    rawstore.write_rows("orphan_lookups", [_lookup("ERR", "matched", n_rows=3, session_date="2026-10-07", fetched_at=2.0)])
    assert rawstore.count("orphan_lookups") == 5
    row = rawstore.get("orphan_lookups", ("ERR", "2026-10-07"))
    assert row["match"] == "matched" and row["error"] is None


# ── forecast_ledger (#164d): forecast-only, append-only, first write wins ────
def _forecast(**kw):
    row = {"source": "spx_gamma", "forecast_date": "2026-10-01", "subject": "SPX",
           "target_type": "pin_prob", "target_date": "2026-10-02", "frozen_at": 1.0,
           "run_id": None, "model_version": "v1", "rank": None, "score": 0.42,
           "bucket": None, "probs": {"5700": 0.3, "5750": 0.5}, "market_cap": None,
           "sector": None, "shown_price": 5730.5, "stop": None, "target": None}
    row.update(kw)
    return row


_FORECAST_KEY = ("spx_gamma", "2026-10-01", "SPX", "pin_prob", "2026-10-02")


def test_forecast_ledger_keeps_the_first_write_for_a_key(rawstore_tmp_db):
    rawstore.write_rows("forecast_ledger", [_forecast()])
    rawstore.write_rows("forecast_ledger", [_forecast(model_version="v2", score=0.9)])
    assert rawstore.count("forecast_ledger") == 1
    row = rawstore.get("forecast_ledger", _FORECAST_KEY)
    assert row["model_version"] == "v1" and row["score"] == 0.42


def test_forecast_ledger_first_write_wins_within_one_bulk_batch(rawstore_tmp_db):
    # 70 rows take the bulk path; the duplicate of row 0 comes last.
    rows = [_forecast(subject=f"T{i:03d}", target_type="pick", target_date="2026-10-01")
            for i in range(70)]
    rows.append({**rows[0], "score": 99.0})
    rawstore.write_rows("forecast_ledger", rows)
    assert rawstore.count("forecast_ledger") == 70
    first = rawstore.get("forecast_ledger", ("spx_gamma", "2026-10-01", "T000", "pick", "2026-10-01"))
    assert first["score"] == 0.42


def test_forecasts_differing_only_in_target_date_both_persist(rawstore_tmp_db):
    rawstore.write_rows("forecast_ledger", [_forecast(), _forecast(target_date="2026-10-03")])
    assert rawstore.count("forecast_ledger") == 2


def test_forecast_round_trips_probs_and_dates(rawstore_tmp_db):
    rawstore.write_rows("forecast_ledger", [_forecast(stop=5600.0, target=5800.0)])
    row = rawstore.get("forecast_ledger", _FORECAST_KEY)
    assert row["probs"] == {"5700": 0.3, "5750": 0.5}
    assert row["forecast_date"] == _dt.date(2026, 10, 1)
    assert row["target_date"] == _dt.date(2026, 10, 2)
    assert (row["stop"], row["target"]) == (5600.0, 5800.0)


def test_forecast_ledger_has_no_outcome_columns(rawstore_tmp_db):
    # Decision 5-B (2026-10-02): outcomes live in a derived forecast_outcomes table.
    cols = {c.split()[0] for c in rawstore.MANIFEST["forecast_ledger"]["columns"]}
    assert not cols & {"outcome", "brier", "scored_at"}
    assert not _columns(rawstore.DB_PATH, "forecast_ledger") & {"outcome", "brier", "scored_at"}
