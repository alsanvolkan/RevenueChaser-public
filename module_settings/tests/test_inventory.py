"""Settings inventory: counts, last dates and holes, computed store-only."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.core.identity import refresh as identity_refresh
from RevenueChaser.module_settings import inventory


def _seed():
    rawstore.write_rows("equity_universe", [
        {"ticker": "AAPL", "market_cap": 3.0e9, "name": "Apple"},
        {"ticker": "MSFT", "market_cap": 3.0e9, "name": "Microsoft"},
        {"ticker": "NVDA", "market_cap": None, "name": "Nvidia"},
    ])
    rawstore.write_rows("chain_filings", [
        {"ticker": "AAPL", "cik": "1", "form": "10-K", "filed": "2026-01-15"},
    ])
    rawstore.write_rows("swing_ohlc", [
        {"ticker": "AAPL", "ts": "2026-02-01", "close": 1.0},
    ])


def _rows(payload):
    return {r["key"]: r for r in payload["rows"]}


def test_collect_reports_counts_last_dates_and_holes(rawstore_tmp_db):
    _seed()
    payload = inventory.collect()
    assert isinstance(payload["generated_at"], str)
    rows = _rows(payload)

    universe = rows["universe"]
    assert universe["rows"] == 3
    assert universe["last_date"] == "—"
    assert universe["holes"] == 1  # NVDA has no market cap
    assert universe["fetch"] == {"route": "/run/universe-stats", "label": "Fetch caps"}
    assert universe["fetch_alt"] == {"route": "/run/universe", "label": "Fetch universe"}

    corpus = rows["corpus"]
    assert corpus["rows"] == 1
    assert corpus["last_date"] == "2026-01-15"
    assert corpus["holes"] == 1  # MSFT is classified but has no filing
    assert corpus["fetch"] == {"route": "/run/edgar-sync", "label": "Fetch 10-K sync"}

    ohlc = rows["ohlc"]
    assert ohlc["rows"] == 1
    assert ohlc["last_date"] == "2026-02-01"
    assert ohlc["holes"] == 1  # MSFT is classified but has no bars


def test_universe_row_fetches_caps_not_full_universe(rawstore_tmp_db):
    _seed()
    universe = _rows(inventory.collect())["universe"]
    assert universe["fetch"] == {"route": "/run/universe-stats", "label": "Fetch caps"}
    assert universe["fetch_alt"] == {"route": "/run/universe", "label": "Fetch universe"}


def test_non_fetch_rows_have_no_action(rawstore_tmp_db):
    _seed()
    rows = _rows(inventory.collect())
    assert rows["universe_provenance"]["fetch"] is None
    assert "sic_spine" not in rows, "the identity row replaced the old SIC / GICS spine row"
    assert rows["index"]["fetch"] is None


def test_identity_row_has_the_fetch_identity_button(rawstore_tmp_db):
    _seed()
    identity = _rows(inventory.collect())["identity"]
    assert identity["label"] == "Identity spine"
    assert identity["fetch"] == {"route": "/run/identity", "label": "Fetch identity"}
    assert identity["tables"] == ["security_master", "identity_conflicts", "identity_source_cache", "identity_runs"]


def test_absent_table_is_dash_not_exception(rawstore_tmp_db):
    _seed()
    rawstore.execute("DROP TABLE swing_ohlc")
    rows = _rows(inventory.collect())  # must not raise
    assert rows["ohlc"]["rows"] is None
    assert rows["ohlc"]["last_date"] == "—"
    assert rows["ohlc"]["holes"] is None


def test_universe_note_reports_provenance_keys(rawstore_tmp_db):
    _seed()
    rawstore.write_rows("equity_universe_provenance", [
        {"key": "__meta__", "generated_at": "2026-09-24T12:00:00Z", "refreshed_by": "test"},
    ])
    rows = _rows(inventory.collect())
    note = rows["universe"]["note"]
    assert "__meta__" in note
    assert "__stats__" in note
    assert "__fetch__" in note       # #146: the last pull's outcome is a third provenance key


def test_collect_makes_no_provider_calls(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.core.providers import sec_edgar, yfinance as yf_provider
    import yfinance

    def boom(*args, **kwargs):
        raise AssertionError("inventory.collect() must not touch the network")

    monkeypatch.setattr(sec_edgar, "get_json", boom)
    monkeypatch.setattr(sec_edgar, "get_text", boom)
    monkeypatch.setattr(sec_edgar, "_cik_map", boom)
    monkeypatch.setattr(yf_provider, "get_financials", boom)
    monkeypatch.setattr(yfinance, "Ticker", boom, raising=False)
    monkeypatch.setattr(yfinance, "screen", boom, raising=False)

    payload = inventory.collect()
    assert payload["rows"]


def _seed_detail():
    rawstore.write_rows("equity_universe", [
        {"ticker": "MU", "market_cap": None, "name": "Micron Technology",
         "price": 110.0, "sector": "Technology", "industry": "Semiconductors"},
        {"ticker": "AAPL", "market_cap": 3.0e12, "name": "Apple Inc."},
    ])
    rawstore.write_rows("sec_company_tickers", [
        {"ticker": "MU", "cik": "0000723125", "name": "Micron Technology"},
    ])
    rawstore.write_rows("sec_company_sic", [
        {"ticker": "MU", "cik": "0000723125", "sic": "3674",
         "sic_description": "Semiconductors & Related Devices"},
    ])
    rawstore.write_rows("chain_filings", [
        {"ticker": "MU", "cik": "0000723125", "form": "10-K",
         "filed": "2026-02-01", "accession": "acc-mu", "quality": "ok",
         "fetched_at": 1.0},
    ])
    rawstore.write_rows("swing_ohlc", [
        {"ticker": "MU", "ts": "2026-02-01", "close": 1.0},
        {"ticker": "MU", "ts": "2026-02-02", "close": 1.1},
    ])


def test_collect_embeds_ticker_names_for_the_drill(rawstore_tmp_db):
    _seed_detail()
    tickers = inventory.collect()["tickers"]
    assert {"ticker": "MU", "name": "Micron Technology"} in tickers
    assert {"ticker": "AAPL", "name": "Apple Inc."} in tickers


def test_ticker_detail_reports_null_cap_as_none_and_out_of_universe(rawstore_tmp_db):
    _seed_detail()
    detail = inventory.ticker_detail("MU")
    assert detail["market_cap"] is None
    assert detail["name"] == "Micron Technology"
    assert detail["presence"]["in_classification_universe"] is False
    # A stored cap above $300M is in the classification universe.
    assert inventory.ticker_detail("AAPL")["presence"]["in_classification_universe"] is True


def test_ticker_detail_reports_the_sector_and_the_industry_text(rawstore_tmp_db):
    """#266: the drill's classification is the two UW levels; there is no industry group or sub-industry any more."""
    _seed_detail()
    detail = inventory.ticker_detail("MU")
    assert detail["sector"] == "Technology"
    assert detail["industry"] == "Semiconductors"
    assert "industry_group" not in detail and "sub_industry" not in detail
    # a row with no classification stays None, never coerced
    aapl = inventory.ticker_detail("AAPL")
    assert aapl["sector"] is None and aapl["industry"] is None


def test_ticker_detail_reads_a_stored_pre_rename_sector_as_its_morningstar_name(rawstore_tmp_db):
    """A row `/run/identity` has not re-applied yet still holds a GICS spelling: the drill shows the UW name."""
    _seed_detail()
    rawstore.write_rows("equity_universe", [
        {"ticker": "OLD", "market_cap": 1e9, "name": "Old Row", "sector": "Information Technology",
         "industry": "Semiconductors"},
        {"ticker": "LEG", "market_cap": 1e9, "name": "Legacy Row", "sector": "Other"}])
    assert inventory.ticker_detail("OLD")["sector"] == "Technology"
    assert inventory.ticker_detail("LEG")["sector"] == "Unclassified"


def test_ticker_detail_reads_filing_from_the_edgar_db(rawstore_tmp_db):
    _seed_detail()
    detail = inventory.ticker_detail("MU")
    assert detail["cik"] == "0000723125"
    assert detail["sic"] == "3674"
    assert detail["sic_description"] == "Semiconductors & Related Devices"
    assert detail["filing"] == {"form": "10-K", "filed": "2026-02-01",
                                "accession": "acc-mu", "quality": "ok"}


def test_ticker_detail_reports_dataset_presence_from_the_store(rawstore_tmp_db):
    _seed_detail()
    presence = inventory.ticker_detail("MU")["presence"]
    assert presence["swing_ohlc"] == {"bars": 2, "newest": "2026-02-02"}
    assert presence["edgar_financials"] is False
    assert presence["oipd_meta"] is False
    assert presence["relatedness_profiles"] is False


def test_ticker_detail_unknown_is_none(rawstore_tmp_db):
    _seed_detail()
    assert inventory.ticker_detail("ZZZZ") is None
    assert inventory.ticker_detail("") is None


def test_ticker_detail_makes_no_provider_calls(rawstore_tmp_db, monkeypatch):
    _seed_detail()
    from RevenueChaser.core.providers import sec_edgar, yfinance as yf_provider
    import yfinance

    def boom(*args, **kwargs):
        raise AssertionError("ticker_detail must not touch the network")

    monkeypatch.setattr(sec_edgar, "get_json", boom)
    monkeypatch.setattr(sec_edgar, "get_text", boom)
    monkeypatch.setattr(sec_edgar, "_cik_map", boom)
    monkeypatch.setattr(yf_provider, "get_financials", boom)
    monkeypatch.setattr(yfinance, "Ticker", boom, raising=False)
    monkeypatch.setattr(yfinance, "screen", boom, raising=False)

    assert inventory.ticker_detail("MU") is not None


def test_missing_counts_a_ticker_once_however_many_rows_it_has(rawstore_tmp_db):
    """OHLC has many bars per ticker; the hole count must still be per ticker."""
    rawstore.write_rows("equity_universe", [
        {"ticker": t, "market_cap": 3.0e9, "name": t} for t in ("AAPL", "MSFT", "NVDA")])
    rawstore.write_rows("swing_ohlc", [
        {"ticker": "AAPL", "ts": f"2026-02-0{d}", "close": 1.0} for d in range(1, 6)
    ] + [{"ticker": "MSFT", "ts": "2026-02-01", "close": 1.0}])
    assert _rows(inventory.collect())["ohlc"]["holes"] == 1  # only NVDA has no bars


def test_a_table_created_after_one_collect_shows_on_the_next(isolate_rawstore):
    """No cache may survive a Refresh: the table list is re-read each time."""
    rawstore.write_rows("equity_universe", [
        {"ticker": "AAPL", "market_cap": 3.0e9, "name": "Apple"}])
    first = _rows(inventory.collect())
    assert first["ohlc"]["rows"] is None and first["ohlc"]["holes"] is None
    rawstore.write_rows("swing_ohlc", [{"ticker": "AAPL", "ts": "2026-02-01", "close": 1.0}])
    second = _rows(inventory.collect())
    assert second["ohlc"]["rows"] == 1 and second["ohlc"]["holes"] == 0


# ── F4: a failed query is an error, a missing table is not ───────────────────
def test_a_failing_query_is_recorded_not_swallowed(rawstore_tmp_db, monkeypatch):
    _seed()

    def boom(name):
        raise RuntimeError("disk on fire <b>")

    monkeypatch.setattr(rawstore, "count", boom)
    payload = inventory.collect()
    errs = payload["errors"]
    assert errs, "a query failure must surface in payload['errors']"
    hit = [e for e in errs if e["table"] == "equity_universe"]
    assert hit and hit[0]["where"] == "_count"
    assert "disk on fire" in hit[0]["error"]
    # still never raises, and the cell stays a dash rather than a fake number
    assert _rows(payload)["universe"]["rows"] is None


def test_a_missing_table_is_not_an_error(rawstore_tmp_db):
    _seed()
    rawstore.execute("DROP TABLE swing_ohlc")
    payload = inventory.collect()
    assert payload["errors"] == []
    assert _rows(payload)["ohlc"]["rows"] is None


def test_errors_do_not_leak_between_calls(rawstore_tmp_db, monkeypatch):
    _seed()
    monkeypatch.setattr(rawstore, "count", lambda n: (_ for _ in ()).throw(RuntimeError("x")))
    assert inventory.collect()["errors"]
    monkeypatch.undo()
    assert inventory.collect()["errors"] == []


def test_collect_includes_board_cards(rawstore_tmp_db):
    _seed()
    cards = inventory.collect()["boards"]
    assert [c["key"] for c in cards] == ["module_comovement", "module_swing_potential"]   # #384 SW-S4


# ── every (table, column) the inventory queries exists in the MANIFEST ───────
def test_queried_columns_exist_in_the_manifest():
    assert inventory.QUERIED_COLUMNS
    for table, column in inventory.QUERIED_COLUMNS:
        assert table in rawstore.MANIFEST, f"{table} is not in MANIFEST"
        cols = rawstore._columns_of(rawstore.MANIFEST[table])
        assert column in cols, f"{table}.{column} is not a MANIFEST column"


def test_queried_columns_list_covers_every_pair_in_the_source():
    import inspect
    import re

    src = inspect.getsource(inventory)
    literal = set(re.findall(r'\("(\w+)",\s*"(\w+)"\)', src))
    literal = {p for p in literal if p[0] in rawstore.MANIFEST}
    missing = literal - set(inventory.QUERIED_COLUMNS)
    assert not missing, f"queried but not pinned in QUERIED_COLUMNS: {sorted(missing)}"


# ── the one universe: the security-type filter reaches the Settings board (#87) ──
def _seed_typed():
    rawstore.write_rows("equity_universe", [
        {"ticker": "AAPL", "market_cap": 3.0e12, "name": "Apple"},
        {"ticker": "FUND", "market_cap": 2.0e12, "name": "A Closed-End Fund"},
        {"ticker": "NOROW", "market_cap": 5.0e9, "name": "Untyped Co"},
    ])
    rawstore.ensure_table("security_master")
    rawstore.write_rows("security_master", [
        {"ticker": "AAPL", "security_type": "common"},
        {"ticker": "FUND", "security_type": "cef"},
    ])


def test_ticker_detail_flag_is_membership_in_the_classification_universe(rawstore_tmp_db):
    from RevenueChaser.core import universe
    _seed_typed()
    assert universe.classification_universe() == ["AAPL", "NOROW"]
    flags = {t: inventory.ticker_detail(t)["presence"]["in_classification_universe"]
             for t in ("AAPL", "FUND", "NOROW")}
    # FUND clears the cap floor but is a fund: not in the universe the boards read
    assert flags == {"AAPL": True, "FUND": False, "NOROW": True}


def test_coverage_counts_follow_the_typed_universe(rawstore_tmp_db):
    _seed_typed()
    rows = _rows(inventory.collect())
    # holes are counted over the operating universe only: AAPL and NOROW, not FUND
    assert rows["corpus"]["holes"] == 2
    assert rows["ohlc"]["holes"] == 2
    assert rows["universe"]["rows"] == 3          # the table itself is untouched


# ── identity health (issue #89): the Identity spine row and ``payload["identity"]`` ───────────
FIXED = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)      # tests never touch today's date
RUN1, RUN2, RUN3 = "2026-09-20T00:00:00Z", "2026-09-30T00:00:00Z", "2026-10-01T00:00:00Z"
SOURCES_OK = {"spdr": {"status": "ok", "selected": True, "rows": 480, "fetched": 0, "error": None},
              "nasdaq": {"status": "ok", "selected": True, "rows": 5000, "fetched": 0, "error": None}}
SOURCES_NO_SPDR = {"nasdaq": {"status": "ok", "selected": True, "rows": 5000, "fetched": 0, "error": None},
                   "spdr": {"status": "skipped", "selected": False, "rows": 0, "fetched": 0, "error": None}}

# (ticker, cap, security_type, sector_source). GONE is not in the universe, NOCAP has a NULL cap.
MASTER = [
    ("AAPL", 3.0e12, "common", "sp1500"), ("MSFT", 2.0e12, "common", "provider_uw"),
    ("ORCL", 1.0e12, "common", "provider_uw"), ("NVDA", 9.0e11, "common", "provider_yf"),
    ("BANKX", 5.0e9, "bank_nonsec", "sic_explicit"), ("PDI", 2.0e9, "cef", "type_rule"),
    ("FLAG", 1.0e9, "etp", "type_rule(etp: no GICS)"), ("ACQ", 4.0e8, "spac", "none"),
    ("SMALL", 1.0e8, "common", "none"), ("NOCAP", None, "common", None),
    ("MANU", 5.0e8, "common", "manual"), ("UNK", 1.0e9, None, "none"), ("GONE", "absent", "common", "provider_uw"),
]


def _run_row(run_id, started, status, sources, counts, finished=None):
    return {"run_id": run_id, "started_at": started, "finished_at": finished or started.replace("00:00:00", "00:06:00"),
            "status": status, "sources": sources, "counts": counts}


def _seed_identity(spdr_error_after=False, only_runs=None):
    rawstore.write_rows("equity_universe", [
        {"ticker": t, "name": t, "market_cap": cap} for t, cap, _, _ in MASTER if cap != "absent"])
    rawstore.write_rows("security_master", [
        {"ticker": t, "security_type": typ, "sector_source": src, "sector": "x"} for t, _, typ, src in MASTER])
    runs = [_run_row("r1", RUN1, "ok", SOURCES_NO_SPDR, {"tickers": 13}),
            _run_row("r2", RUN2, "ok", SOURCES_OK, {"tickers": 13, "conflicts": 6, "spdr_checked": 3, "spdr_agree": 2})]
    if spdr_error_after:
        runs.append(_run_row("r3", RUN3, "ok", {"spdr": {"status": "error", "selected": True, "rows": 0, "fetched": 0,
                                                       "error": "SPDR xle: HTTP 500"}}, {"tickers": 13}))
    runs = [r for r in runs if only_runs is None or r["run_id"] in only_runs]
    if runs:
        rawstore.write_rows("identity_runs", runs)
    cur = RUN3 if spdr_error_after else RUN2

    def conflict(t, kind, field, a, b, note, last_seen):
        return {"ticker": t, "kind": kind, "field": field, "side_a": a, "side_b": b, "note": note,
                "first_seen": RUN1, "last_seen": last_seen}
    rawstore.write_rows("identity_conflicts", [
        conflict("AAPL", "spdr", "sector", "Technology", "Communication Services", "SPDR XLC holdings", cur),
        conflict("MSFT", "sector", "sector", "Technology", "Financial Services", "uw vs yahoo", cur),
        conflict("MSFT", "cap", "market_cap", "2.0e12", "2.0e11", "10x", cur),
        conflict("ORCL", "sector", "sector", "Technology", "Healthcare", "uw vs yahoo", cur),
        conflict("NOCAP", "type", "security_type", "common", "etp", "check", cur),
        conflict("*", "source", "uw_snapshot", "previous snapshot", "new snapshot", "gated", cur),
        conflict("GONE", "sector", "sector", "a", "b", "old", RUN1),                 # older: not seen in the latest run
        conflict("SMALL", "cap", "market_cap", "1", "2", "older still", "2026-09-10T00:00:00Z"),
    ])

    def cache_row(source, ticker, fetched_at, status="ok"):
        return {"source": source, "ticker": ticker, "fetched_at": fetched_at, "status": status}
    rawstore.write_rows("identity_source_cache", [
        # sec_submissions: AAPL fresh; MSFT 31 d; ORCL exactly 30 d (= stale); an outsider is the newest row
        cache_row("sec_submissions", "AAPL", "2026-09-25T12:00:00Z"),
        cache_row("sec_submissions", "MSFT", "2026-08-31T00:00:00Z"),
        cache_row("sec_submissions", "ORCL", "2026-09-01T12:00:00Z"),
        cache_row("sec_submissions", "OUTSIDER", "2026-09-30T00:00:00Z"),
        # openfigi: ok rows count against 90 days, a no_match row against 7
        cache_row("openfigi", "AAPL", "2026-09-23T12:00:00Z"),
        cache_row("openfigi", "ORCL", "2026-09-23T12:00:00Z"),
        cache_row("openfigi", "MSFT", "2026-06-23T00:00:00Z"),
        cache_row("openfigi", "SMALL", "2026-09-23T12:00:00Z", "no_match"),
        cache_row("openfigi", "NOCAP", "2026-09-28T12:00:00Z", "no_match"),
        cache_row("openfigi", "OUTSIDER", "2026-01-01T00:00:00Z", "no_match"),
        cache_row("yahoo_sector", "AAPL", "2026-06-01T00:00:00Z"),
        cache_row("yahoo_sector", "MSFT", "2026-09-21T00:00:00Z"),
        cache_row("sec_exchange", "AAPL", "2026-09-30T00:00:00Z"),
        cache_row("sec_exchange", "MSFT", "2026-09-30T00:00:00Z"),
        cache_row("nasdaq", "AAPL", "2026-09-30T00:00:00Z"),
        cache_row("sp1500", "AAPL", "2026-09-30T00:00:00Z"),
        cache_row("uw_sector", "AAPL", "2026-09-30T00:00:00Z"),
    ])


def _identity(**kw):
    return inventory.collect(now=FIXED)["identity"]


def test_collect_without_now_still_works_and_stamps_generated_at_from_now(rawstore_tmp_db):
    _seed()
    assert inventory.collect(now=FIXED)["generated_at"].startswith("2026-10-01T12:00:00")
    assert inventory.collect()["generated_at"]


def test_the_identity_row_counts_the_master_and_reports_the_last_run_and_unclassified_holes(rawstore_tmp_db):
    _seed_identity()
    row = _rows(inventory.collect(now=FIXED))["identity"]
    assert row["rows"] == len(MASTER)
    assert row["last_date"] == "2026-09-30", "the newest identity_runs.finished_at"
    assert row["holes"] == 2, "ACQ and UNK are unclassified at >= $300M; SMALL is below, NOCAP has no cap"
    assert row["hole_label"] == "unclassified ≥$300M"
    assert row["note"] == "last run ok"
    assert row["fetch"] == {"route": "/run/identity", "label": "Fetch identity"}


def test_the_identity_payload_is_empty_without_the_identity_tables_and_never_raises(isolate_rawstore):
    rawstore.write_rows("equity_universe", [{"ticker": "AAPL", "market_cap": 3.0e9, "name": "Apple"}])
    payload = inventory.collect(now=FIXED)
    ident = payload["identity"]
    assert ident["last_run"] is None and ident["floor"] == 300_000_000.0
    assert ident["type_mix"] == [] and ident["source_mix"] == [] and ident["stale"] == []
    assert ident["conflicts"] == {"as_of": None, "by_kind": [], "top": [], "older": {"count": 0, "since": None}}
    assert ident["spdr"] == {"state": "never"}
    assert payload["errors"] == [], "a missing table is not a failure"
    row = _rows(payload)["identity"]
    assert row["rows"] is None and row["holes"] is None and row["last_date"] == "—"
    assert row["note"] == "no identity run yet"


def test_the_floor_is_the_classification_universe_floor(rawstore_tmp_db):
    from RevenueChaser.core.universe import DEFAULT_MC_MIN
    _seed_identity()
    assert _identity()["floor"] == DEFAULT_MC_MIN


def test_last_run_is_the_latest_by_started_at_with_parsed_sources_and_counts(rawstore_tmp_db):
    _seed_identity()
    run = _identity()["last_run"]
    assert run["run_id"] == "r2" and run["status"] == "ok"
    assert run["started_at"] == RUN2 and run["finished_at"] == "2026-09-30T00:06:00Z"
    assert run["sources"]["nasdaq"] == {"status": "ok", "rows": 5000, "fetched": 0, "error": None, "selected": True}
    assert run["counts"]["spdr_checked"] == 3 and run["counts"]["tickers"] == 13


def test_type_mix_counts_all_and_the_floor_flags_excluded_types_and_sorts_by_size(rawstore_tmp_db):
    _seed_identity()
    mix = _identity()["type_mix"]
    assert mix == [
        {"type": "common", "all": 8, "floor": 5, "excluded": False},     # floor: AAPL MSFT ORCL NVDA MANU
        {"type": "bank_nonsec", "all": 1, "floor": 1, "excluded": False},
        {"type": "cef", "all": 1, "floor": 1, "excluded": True},
        {"type": "etp", "all": 1, "floor": 1, "excluded": True},
        {"type": "spac", "all": 1, "floor": 1, "excluded": True},
        {"type": "unknown", "all": 1, "floor": 1, "excluded": False},   # a NULL type reads as "unknown"
    ]


def test_source_mix_buckets_in_a_fixed_order_and_other_only_when_non_zero(rawstore_tmp_db):
    _seed_identity()
    mix = _identity()["source_mix"]
    assert mix == [
        {"bucket": "official", "all": 1, "floor": 1},         # sp1500: AAPL (the S&P 1500 list, #272)
        {"bucket": "provider", "all": 4, "floor": 3},         # provider_uw x3 (GONE is not in the universe), provider_yf
        {"bucket": "SIC", "all": 1, "floor": 1},              # sic_explicit: BANKX
        {"bucket": "type rule", "all": 2, "floor": 2},        # type_rule and type_rule(etp: no GICS)
        {"bucket": "unclassified", "all": 4, "floor": 2},     # 'none' x3 and NULL
        {"bucket": "other", "all": 1, "floor": 1},            # manual
    ]
    rawstore.execute("UPDATE security_master SET sector_source = 'provider_uw' WHERE sector_source = 'manual'")
    assert [m["bucket"] for m in _identity()["source_mix"]] == [
        "official", "provider", "SIC", "type rule", "unclassified"], "no 'other' row when it is empty"


def test_every_bucket_is_listed_even_when_the_master_has_no_row_for_it(rawstore_tmp_db):
    """A master built before the list and the SIC fallback came back shows them as zero, not as missing."""
    _seed_identity()
    rawstore.execute("UPDATE security_master SET sector_source = 'provider_uw' "
                     "WHERE sector_source IN ('sp1500', 'sic_explicit')")
    mix = {m["bucket"]: m for m in _identity()["source_mix"]}
    assert mix["official"] == {"bucket": "official", "all": 0, "floor": 0}
    assert mix["SIC"] == {"bucket": "SIC", "all": 0, "floor": 0}
    assert mix["provider"] == {"bucket": "provider", "all": 6, "floor": 5}


def test_the_source_mix_values_match_what_the_master_actually_writes():
    """A renamed source value in gics.py must not silently fall into 'other'; the removed one is gone from it."""
    import inspect
    from RevenueChaser.core.identity import gics
    src = inspect.getsource(gics)
    for value in ("sp1500", "provider_uw", "provider_yf", "sic_explicit", "type_rule", "manual"):
        assert value in src
    assert "provider_uw+correction" not in src


def test_conflicts_current_means_last_seen_equals_the_latest_ok_or_partial_run(rawstore_tmp_db):
    _seed_identity()
    c = _identity()["conflicts"]
    assert c["as_of"] == RUN2
    assert c["by_kind"] == [{"kind": "sector", "count": 2}, {"kind": "cap", "count": 1},
                            {"kind": "source", "count": 1}, {"kind": "spdr", "count": 1},
                            {"kind": "type", "count": 1}], "by count, then kind"
    assert c["older"] == {"count": 2, "since": RUN1}, "the newest last_seen among the not-current rows"


def test_an_error_run_does_not_move_the_conflict_as_of_date(rawstore_tmp_db):
    _seed_identity()
    rawstore.write_rows("identity_runs", [_run_row("r9", "2026-10-01T00:00:00Z", "error", {}, {"error": "x"})])
    assert _identity()["conflicts"]["as_of"] == RUN2


def test_top_conflicts_are_ordered_by_market_cap_with_no_cap_last_and_the_star_row_at_the_end(rawstore_tmp_db):
    _seed_identity()
    top = _identity()["conflicts"]["top"]
    assert [(t["ticker"], t["kind"]) for t in top] == [
        ("AAPL", "spdr"), ("MSFT", "cap"), ("MSFT", "sector"), ("ORCL", "sector"), ("NOCAP", "type"), ("*", "source")]
    assert top[0] == {"ticker": "AAPL", "kind": "spdr", "field": "sector", "side_a": "Technology",
                      "side_b": "Communication Services", "note": "SPDR XLC holdings", "last_seen": RUN2,
                      "market_cap": 3.0e12}
    assert top[-1]["market_cap"] is None and top[-2]["market_cap"] is None


def test_top_conflicts_are_capped_at_fifty_but_the_counts_are_not(rawstore_tmp_db):
    _seed_identity()
    rawstore.write_rows("equity_universe", [
        {"ticker": f"F{i:02d}", "name": "f", "market_cap": 1.0e6 * (i + 1)} for i in range(60)])
    rawstore.write_rows("identity_conflicts", [
        {"ticker": f"F{i:02d}", "kind": "sector", "field": "sector", "side_a": "a", "side_b": "b", "note": "",
         "first_seen": RUN1, "last_seen": RUN2} for i in range(60)])
    c = _identity()["conflicts"]
    assert len(c["top"]) == 50
    assert {k["kind"]: k["count"] for k in c["by_kind"]}["sector"] == 62
    caps = [t["market_cap"] for t in c["top"] if t["market_cap"] is not None]
    assert caps == sorted(caps, reverse=True)
    assert "F00" not in {t["ticker"] for t in c["top"]}, "the smallest caps fall off the list"


def test_with_no_ok_run_every_conflict_is_older(rawstore_tmp_db):
    _seed_identity(only_runs=set())
    rawstore.write_rows("identity_runs", [_run_row("r9", RUN3, "error", {}, {})])
    c = _identity()["conflicts"]
    assert c["as_of"] is None and c["by_kind"] == [] and c["top"] == []
    assert c["older"]["count"] == 8 and c["older"]["since"] == RUN2


def test_stale_counts_universe_tickers_only_and_uses_the_refresh_windows(rawstore_tmp_db):
    _seed_identity()
    stale = {s["source"]: s for s in _identity()["stale"]}
    R = identity_refresh
    sec = stale["sec_submissions"]
    assert sec["window"] == f"{R.SEC_DAYS} days"
    assert sec["newest"] == "2026-09-30T00:00:00Z", "newest covers every row, the non-universe ticker included"
    assert (sec["rows"], sec["past_window"]) == (3, 2), "MSFT (31 d) and ORCL (exactly 30 d) are past the window"
    figi = stale["openfigi"]
    assert figi["window"] == f"{R.FIGI_DAYS} days (miss {R.FIGI_MISS_DAYS})"
    assert (figi["rows"], figi["past_window"]) == (5, 2), (
        "MSFT (ok, 100 d) and SMALL (no_match, 8 d) are past; ORCL (ok, 8 d) and NOCAP (miss, 3 d) are not")
    assert figi["newest"] == "2026-09-28T12:00:00Z"
    yahoo = stale["yahoo_sector"]
    assert yahoo["window"] == f"{R.YAHOO_DAYS} days" and (yahoo["rows"], yahoo["past_window"]) == (2, 1)
    fdic = stale["fdic"]
    assert fdic == {"source": "fdic", "newest": None, "rows": 0, "past_window": 0, "window": f"{R.FDIC_DAYS} days"}, (
        "a windowed source that was never cached still gets a row")


def test_stale_uses_the_refresh_staleness_function_and_not_its_own_arithmetic(rawstore_tmp_db, monkeypatch):
    _seed_identity()
    seen = []
    real = identity_refresh._stale
    monkeypatch.setattr(identity_refresh, "_stale", lambda *a: seen.append(a) or real(*a))
    _identity()
    assert seen, "inventory must call refresh._stale so the semantics cannot drift"


def test_every_run_sources_have_no_window_and_no_past_window_count(rawstore_tmp_db):
    _seed_identity()
    stale = {s["source"]: s for s in _identity()["stale"]}
    for name in ("sec_exchange", "nasdaq", "sp1500", "uw_sector"):
        assert stale[name]["window"] == "every run" and stale[name]["past_window"] is None
    assert stale["sec_exchange"]["rows"] == 2 and stale["nasdaq"]["newest"] == "2026-09-30T00:00:00Z"
    assert "spdr" not in stale, "SPDR rows are never cached"


def test_stale_lists_the_known_sources_in_refresh_order(rawstore_tmp_db):
    _seed_identity()
    assert [s["source"] for s in _identity()["stale"]] == [
        "sec_exchange", "sec_submissions", "openfigi", "nasdaq", "sp1500", "uw_sector", "yahoo_sector", "fdic"]


def test_a_cached_source_that_is_no_longer_in_sources_is_hidden(rawstore_tmp_db):
    """A source a later change drops stays in the cache table (nothing deletes rows) but the card does not list it."""
    _seed_identity()
    cache_row = rawstore.query("SELECT * FROM identity_source_cache WHERE source = 'sp1500' LIMIT 1")[0]
    rawstore.write_rows("identity_source_cache", [dict(cache_row, source="retired_source")])
    assert "retired_source" in {r["source"] for r in rawstore.query("SELECT source FROM identity_source_cache")}
    assert "retired_source" not in {s["source"] for s in _identity()["stale"]}


def test_spdr_state_never_when_no_run_selected_it(rawstore_tmp_db):
    _seed_identity(only_runs={"r1"})
    assert _identity()["spdr"] == {"state": "never"}


def test_spdr_state_ok_reports_checked_agree_and_the_run_date(rawstore_tmp_db):
    _seed_identity()
    assert _identity()["spdr"] == {"state": "ok", "checked": 3, "agree": 2, "date": RUN2}


def test_spdr_state_error_carries_the_last_good_check(rawstore_tmp_db):
    _seed_identity(spdr_error_after=True)
    assert _identity()["spdr"] == {"state": "error", "error": "SPDR xle: HTTP 500", "date": RUN3,
                                   "last_good": {"date": RUN2, "checked": 3, "agree": 2}}


def test_spdr_state_error_without_any_good_run_has_no_last_good(rawstore_tmp_db):
    _seed_identity(spdr_error_after=True, only_runs={"r1", "r3"})
    spdr = _identity()["spdr"]
    assert spdr["state"] == "error" and spdr["last_good"] is None and spdr["date"] == RUN3


def test_a_run_that_stopped_before_the_spdr_check_does_not_hide_the_last_check(rawstore_tmp_db):
    """A run that raised early still lists SPDR as selected with its initial ``ok`` but never checked."""
    _seed_identity()
    rawstore.write_rows("identity_runs", [_run_row("r4", RUN3, "error", {
        "spdr": {"status": "ok", "selected": True, "rows": 0, "fetched": 0, "error": None}},
        {"error": "RuntimeError: equity_universe is empty"})])
    assert _identity()["spdr"] == {"state": "ok", "checked": 3, "agree": 2, "date": RUN2}


def test_spdr_error_falls_back_to_the_status_when_the_run_recorded_no_message(rawstore_tmp_db):
    _seed_identity(only_runs={"r1"})
    rawstore.write_rows("identity_runs", [_run_row("r4", RUN3, "ok", {
        "spdr": {"status": "skipped", "selected": True, "rows": 0, "fetched": 0, "error": None}}, {})])
    assert _identity()["spdr"]["error"] == "skipped"


def test_identity_collect_makes_no_provider_calls(rawstore_tmp_db, monkeypatch):
    import httpx
    _seed_identity()

    def boom(*a, **k):
        raise AssertionError("the identity health card is store-only")
    monkeypatch.setattr(httpx, "get", boom)
    monkeypatch.setattr(httpx, "post", boom)
    assert inventory.collect(now=FIXED)["identity"]["last_run"]


def test_the_identity_health_code_does_not_depend_on_the_shared_floor_import(rawstore_tmp_db, monkeypatch):
    """Another branch may drop ``DEFAULT_MC_MIN`` from inventory's shared universe import: the identity-health
    code keeps its own alias, so removing that name must not break it."""
    from RevenueChaser.core.universe import DEFAULT_MC_MIN
    _seed_identity()
    monkeypatch.delattr(inventory, "DEFAULT_MC_MIN")
    payload = inventory.collect(now=FIXED)
    ident = payload["identity"]
    assert ident["floor"] == DEFAULT_MC_MIN and ident["type_mix"] and ident["source_mix"]
    assert _rows(payload)["identity"]["holes"] == 2
    assert payload["errors"] == []


# -- Discord export readiness (#242): ``payload["discord"]``, environment + CLI file only -----------------------
DISCORD_TOKEN = "SENTINEL-TOKEN-1b7e"
DISCORD_GUILD = "SENTINEL-GUILD-9042"


def _discord_env(monkeypatch, tmp_path, version=(2, 48, 0)):
    from RevenueChaser.module_discord import exporter
    exe = tmp_path / "dce"
    exe.write_text("")
    monkeypatch.setenv("RC_DCE_CLI", str(exe))
    monkeypatch.setenv("DISCORD_TOKEN", DISCORD_TOKEN)
    monkeypatch.setenv("RC_DISCORD_GUILD_ID", DISCORD_GUILD)
    monkeypatch.setattr(exporter, "cli_version", lambda: exporter.CliVersion(version, "", ""))


def test_collect_carries_the_discord_readiness(rawstore_tmp_db, monkeypatch, tmp_path):
    _seed()
    _discord_env(monkeypatch, tmp_path)
    discord = inventory.collect()["discord"]
    assert discord["ready"] is True
    assert [c["key"] for c in discord["checks"]] == ["cli", "version", "token", "guild"]


def test_the_readiness_is_not_ready_without_the_environment_and_starts_no_process(rawstore_tmp_db, monkeypatch):
    """The root conftest removes RC_DCE_CLI, DISCORD_TOKEN and RC_DISCORD_GUILD_ID for every test."""
    from RevenueChaser.module_discord import exporter

    def boom(*a, **kw):
        raise AssertionError("collect() must not start the DCE exe")

    monkeypatch.setattr(exporter, "run_cli", boom)
    _seed()
    discord = inventory.collect()["discord"]
    assert discord["ready"] is False
    assert {c["key"]: c["state"] for c in discord["checks"]} == {
        "cli": "bad", "version": "skipped", "token": "bad", "guild": "bad"}


def test_a_failing_readiness_check_is_recorded_and_the_rest_of_the_payload_survives(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.module_discord import readiness
    from RevenueChaser.module_settings import report

    def boom():
        raise RuntimeError("readiness exploded")

    monkeypatch.setattr(readiness, "collect", boom)
    _seed()
    payload = inventory.collect()
    assert payload["discord"] is None
    assert {"table": "discord", "where": "_discord_readiness", "error": "readiness exploded"} in payload["errors"]
    assert _rows(payload)["universe"]["rows"] == 3
    html = report.render_settings_dashboard(payload)
    assert "readiness exploded" in html and 'id="discord-readiness"' not in html


def test_neither_the_token_nor_the_guild_value_reaches_the_payload_or_the_page(rawstore_tmp_db, monkeypatch, tmp_path):
    import json
    from RevenueChaser.module_settings import report
    _seed()
    _discord_env(monkeypatch, tmp_path)
    payload = inventory.collect()
    page = report.render_settings_dashboard(payload)
    for text in (json.dumps(payload, default=str), page):
        assert DISCORD_TOKEN not in text and DISCORD_GUILD not in text
    assert 'id="discord-readiness"' in page
    # a failing check puts its message in errors; that must not carry the values either
    from RevenueChaser.module_discord import readiness

    def boom():
        raise RuntimeError("check failed")

    monkeypatch.setattr(readiness, "collect", boom)
    broken = inventory.collect()
    for text in (json.dumps(broken, default=str), report.render_settings_dashboard(broken)):
        assert DISCORD_TOKEN not in text and DISCORD_GUILD not in text


# ── Daily price bars row (#156) ───────────────────────────────────────────────
def test_price_bars_row_on_an_empty_store(rawstore_tmp_db):
    from RevenueChaser.core import prices
    _seed()
    row = _rows(inventory.collect(now=FIXED))["price_bars"]
    assert row["label"] == "Daily price bars"
    assert row["tables"] == ["price_bars_daily", "price_actions"]
    assert row["rows"] == 0 and row["last_date"] == "—"
    assert "no bars yet" in row["note"]
    assert row["holes"] == len(prices.bar_scope())
    assert row["hole_label"] == "behind latest session" and row["fetch"] is None


def test_price_bars_row_on_a_seeded_store(rawstore_tmp_db):
    from RevenueChaser.core import prices
    _seed()
    rawstore.write_rows("price_bars_daily", [
        {"ticker": "SPY", "session_date": date(2026, 9, 30), "close": 600.0, "source": "uw",
         "volume": 1.0},
        {"ticker": "AAPL", "session_date": date(2026, 9, 30), "close": 10.0, "source": "yfinance",
         "volume": 1.0}])
    rawstore.write_rows("price_actions", [
        {"ticker": "AAPL", "ex_date": date(2026, 9, 30), "action": "dividend", "value": 0.1,
         "session_date": date(2026, 9, 30), "source": "yfinance", "fetched_at": 1.0,
         "run_id": "r", "run_at": 1.0}])
    row = _rows(inventory.collect(now=FIXED))["price_bars"]
    assert row["rows"] == 2 and row["last_date"] == "2026-09-30"
    assert "newest 2026-09-30" in row["note"]
    assert "UW 1 / yfinance 1" in row["note"]
    assert "1 actions in the last 5 sessions" in row["note"]
    assert row["holes"] == len(prices.bar_scope()) - 2


def test_price_bars_note_on_a_calendar_error(rawstore_tmp_db):
    _seed()
    rawstore.write_rows("price_bars_daily", [
        {"ticker": "SPY", "session_date": date(2026, 9, 30), "close": 600.0, "source": "uw"}])
    payload = inventory.collect(now=datetime(2029, 1, 2, 12, 0, tzinfo=timezone.utc))
    assert "calendar error" in _rows(payload)["price_bars"]["note"]
    assert payload["errors"] == []


def test_price_bars_queries_are_pinned():
    pairs = {("price_bars_daily", "ticker"), ("price_bars_daily", "session_date"),
             ("price_bars_daily", "source"), ("price_actions", "ex_date"),
             ("price_checks", "dataset"), ("price_checks", "source"),
             ("price_checks", "checked_at")}
    assert pairs <= set(inventory.QUERIED_COLUMNS)

# ── Quotes and market cap row (#294) ──────────────────────────────────────────
def _quote(ticker, source, cap, day=date(2026, 9, 30)):
    return {"ticker": ticker, "session_date": day, "source": source, "market_cap": cap, "price": 1.0,
            "payload": {"t": ticker}, "fetched_at": 1.0, "run_id": "r", "run_at": 1.0}


def test_quotes_row_on_an_empty_store(rawstore_tmp_db):
    _seed()
    row = _rows(inventory.collect(now=FIXED))["quotes"]
    assert row["label"] == "Quotes and market cap" and row["tables"] == ["quote_snapshot"]
    assert row["rows"] == 0 and row["last_date"] == "—"
    assert row["holes"] == 3 and row["hole_label"] == "no Yahoo cap in newest session"
    assert row["fetch"] == {"route": "/run/universe", "label": "Fetch universe"}
    assert "no quote rows yet" in row["note"]


def test_quotes_row_on_a_seeded_store(rawstore_tmp_db):
    from RevenueChaser.core import quotes
    _seed()
    rawstore.write_rows("quote_snapshot", [
        _quote("AAPL", "yfinance", 100.0), _quote("AAPL", "uw", 110.0),
        _quote("MSFT", "yfinance", 100.0),
        _quote("AAPL", "yfinance", 90.0, day=date(2026, 9, 29))])
    rawstore.write_rows("source_checks", [{"dataset": "quote_snapshot", "source": "yfinance",
                                           "key": quotes.RATE_LIMIT_CHECK[2], "checked_at": FIXED.timestamp(),
                                           "run_id": "r"}])
    row = _rows(inventory.collect(now=FIXED))["quotes"]
    assert row["rows"] == 4 and row["last_date"] == "2026-09-30"
    assert row["holes"] == 1, "NVDA has no Yahoo row in the newest session (MSFT has no UW row: not a hole)"
    assert row["note"] == ("newest 2026-09-30: yfinance 2 / UW 1 of 3 universe; 1 caps more than 5% apart "
                           f"(AAPL); last Yahoo rate-limit stop {FIXED.date().isoformat()}")


def test_quotes_row_survives_a_failing_summary(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.core import quotes
    _seed()

    def boom():
        raise RuntimeError("bad read")
    monkeypatch.setattr(quotes, "health_summary", boom)
    payload = inventory.collect(now=FIXED)
    assert _rows(payload)["quotes"]["note"] == "quote summary unavailable"
    assert payload["errors"][-1]["where"] == "_quotes_row"


def test_quotes_queries_are_pinned():
    pairs = {("quote_snapshot", "ticker"), ("quote_snapshot", "session_date"), ("quote_snapshot", "source"),
             ("quote_snapshot", "market_cap"), ("source_checks", "key")}
    assert pairs <= set(inventory.QUERIED_COLUMNS)


def test_corpus_note_names_recorded_misses_and_no_cik(rawstore_tmp_db):
    import json
    _seed()
    # No CIK map stored: unknown, so the note stays empty (not "2 no CIK").
    assert _rows(inventory.collect())["corpus"]["note"] == ""
    rawstore.write_rows("sec_company_tickers", [{"ticker": "MSFT", "cik": "7", "name": "Microsoft"}])
    rawstore.write_rows("chain_meta", [{"key": "miss:MSFT", "value": json.dumps(
        {"reason": "form_40f", "cik": "7", "checked": datetime.now(timezone.utc).isoformat()})}])
    note = _rows(inventory.collect())["corpus"]["note"]
    assert note == "1 recorded misses (0 no 10-K/20-F, 1 40-F; 0 due for retry) - 1 no CIK"


def test_corpus_note_is_empty_when_the_store_read_fails(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.module_value_chain import engine as vc_engine
    _seed()
    monkeypatch.setattr(vc_engine, "corpus_miss_summary", lambda tickers: None)
    assert _rows(inventory.collect())["corpus"]["note"] == ""


@pytest.mark.parametrize("status,want,not_want", [
    (404, "No stored data for", "Could not read"),
    (400, "No stored data for", "Could not read"),
    (500, "Could not read the store for", "No stored data")])
def test_df98_drill_message_by_status(tmp_path, rawstore_tmp_db, status, want, not_want):
    """DF98 under node: only an unknown or malformed ticker says 'No stored data'; a server failure says the store
    could not be read."""
    from RevenueChaser.core.tests.board_js import run_board_js
    from RevenueChaser.module_settings import report as sr
    html = sr.render_settings_dashboard(inventory.collect())
    tail = """
globalThis.fetch = function(){ return Promise.resolve({status: %d, ok: false, json: function(){ return Promise.resolve({}); }}); };
onDrillPick('AAPL');
setTimeout(function(){ console.log(JSON.stringify(_els['drill-body'].innerHTML)); }, 20);
""" % status
    out = run_board_js(html, tail, tmp_path)
    assert want in out and not_want not in out


# ── Vendor statements and valuation row (#297) ───────────────────────────────
def test_vendor_row_on_an_empty_store(rawstore_tmp_db):
    _seed()
    row = _rows(inventory.collect(now=FIXED))["vendor"]
    assert row["label"] == "Vendor statements & valuation"
    assert row["tables"] == ["vendor_financials", "vendor_valuation"]
    assert row["rows"] == 0 and row["last_date"] == "—" and row["fetch"] is None
    assert "no vendor rows yet" in row["note"]


def test_vendor_row_on_a_seeded_store(rawstore_tmp_db):
    from RevenueChaser.core import vendor_data
    from RevenueChaser.core.tests.test_vendor_data import COMPLETE, _INFO, _uw_fin
    _seed()
    vendor_data.record(statements=vendor_data.statement_rows_uw("BBB", _uw_fin()),
                       valuations=[vendor_data.valuation_row_yfinance("AAA", _INFO)], run_id="r1", run_at=COMPLETE)
    row = _rows(inventory.collect(now=FIXED))["vendor"]
    assert row["rows"] == 3 and row["last_date"] == "2026-10-08"
    assert "valuation 1 tickers on 2026-10-08" in row["note"]
    assert "newest statement change: 3 rows on 2026-10-08" in row["note"]
    assert "last run" not in row["note"]
    assert "UW revenue 100%" in row["note"]


def test_vendor_queries_are_pinned():
    pairs = {("vendor_financials", "fetched_at"), ("vendor_valuation", "session_date")}
    assert pairs <= set(inventory.QUERIED_COLUMNS)
