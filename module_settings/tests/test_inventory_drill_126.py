"""The per-ticker drill's dataset presence, identifiers and board fit (S10 of the Data Health
plan, #126; G7 of the GUI brief, #124).

Five datasets decide whether a board can score a name: D01 bars, the quote, vendor
statements, EDGAR facts and ownership. The drill reports each one's count and newest
session/date, the identifiers the identity spine holds, and — since G7 — which registered
boards can score this name from the tables the drill already measured. Store only, one query
per table, and a table or column this store does not carry reads as absent — never an
exception.
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_settings import inventory


def _bars(ticker, *days):
    return [{"ticker": ticker, "session_date": date.fromisoformat(day), "close": 1.0,
             "source": "uw", "volume": 1.0} for day in days]


def _seed_every_dataset():
    """One ticker with a row in all five scoring datasets and in ``security_master``."""
    rawstore.write_rows("equity_universe", [
        {"ticker": "MU", "market_cap": 1.0e9, "name": "Micron Technology"}])
    rawstore.write_rows("security_master", [
        {"ticker": "MU", "cik": "0000723125", "figi": "BBG000BLNNH6"}])
    rawstore.write_rows("price_bars_daily", _bars("MU", "2026-01-30", "2026-02-02"))
    rawstore.write_rows("quote_snapshot", [
        {"ticker": "MU", "session_date": date(2026, 10, 20), "source": "yfinance",
         "market_cap": 1.0e9, "price": 100.0, "run_at": 1.0}])
    rawstore.write_rows("vendor_financials", [
        {"ticker": "MU", "source": "yfinance", "statement": "income", "report_type": "annual",
         "period_end": date(2026, 6, 4), "status": "present", "run_at": 1.0}])
    rawstore.write_rows("edgar_financials", [
        {"ticker": "MU", "annual": [
            {"fiscal_year": "2025", "revenue": 1.0, "period_ends": {"revenue": "2025-08-28"}},
            {"fiscal_year": "2026", "revenue": 2.0, "period_ends": {"revenue": "2026-08-27"}}],
         "quarterly": [], "meta": {"source": "sec_edgar"}, "fetched_at": 1.0}])
    rawstore.write_rows("ownership_snapshot", [
        {"ticker": "MU", "session_date": date(2026, 10, 21), "source": "yfinance",
         "institutional_pct": 0.8, "run_at": 1.0}])


def test_the_five_scoring_datasets_report_a_count_and_the_newest_date(rawstore_tmp_db):
    _seed_every_dataset()
    presence = inventory.ticker_detail("MU")["presence"]

    assert presence["price_bars_daily"] == {"bars": 2, "newest": "2026-02-02"}
    assert presence["quote_snapshot"] == {"rows": 1, "newest": "2026-10-20", "source": "yfinance"}
    assert presence["vendor_financials"] == {"rows": 1, "newest": "2026-06-04"}
    assert presence["edgar_financials"] == {"periods": 2, "newest": "2026-08-27"}
    assert presence["ownership_snapshot"] == {"rows": 1, "newest": "2026-10-21", "source": "yfinance"}


def test_a_ticker_with_only_d01_bars_shows_the_other_four_absent(rawstore_tmp_db):
    rawstore.write_rows("price_bars_daily", _bars("NVDA", "2026-02-02"))
    presence = inventory.ticker_detail("NVDA")["presence"]

    assert presence["price_bars_daily"] == {"bars": 1, "newest": "2026-02-02"}
    assert presence["quote_snapshot"] == {"rows": 0, "newest": None, "source": None}
    assert presence["vendor_financials"] == {"rows": 0, "newest": None}
    assert presence["edgar_financials"] == {"periods": 0, "newest": None}
    assert presence["ownership_snapshot"] == {"rows": 0, "newest": None, "source": None}


def test_a_missing_presence_table_is_no_data_not_an_exception(rawstore_tmp_db):
    """A store without the table (or an old one) must render "absent", never raise."""
    _seed_every_dataset()
    for table in ("quote_snapshot", "vendor_financials", "ownership_snapshot", "security_master"):
        rawstore.execute(f"DROP TABLE {table}")
    rawstore.execute("DROP TABLE edgar_financials", db="edgar")

    detail = inventory.ticker_detail("MU")

    assert detail is not None
    presence = detail["presence"]
    assert presence["price_bars_daily"] == {"bars": 2, "newest": "2026-02-02"}
    assert presence["quote_snapshot"] == {"rows": 0, "newest": None, "source": None}
    assert presence["vendor_financials"] == {"rows": 0, "newest": None}
    assert presence["edgar_financials"] == {"periods": 0, "newest": None}
    assert presence["ownership_snapshot"] == {"rows": 0, "newest": None, "source": None}


def test_the_drill_carries_the_identifiers_the_master_holds(rawstore_tmp_db):
    _seed_every_dataset()
    detail = inventory.ticker_detail("MU")

    assert detail["ticker"] == "MU"
    assert detail["cik"] == "0000723125"
    assert detail["figi"] == "BBG000BLNNH6"
    # The columns exist (#127) but nothing is applied for this ticker: no value, not n/a.
    assert detail["cusip"] is None
    assert detail["isin"] is None


def test_the_identifiers_say_na_when_the_columns_are_not_in_this_store(rawstore_tmp_db, monkeypatch):
    """An older store without the columns: n/a, never an exception."""
    _seed_every_dataset()
    monkeypatch.setattr(inventory, "_has_column",
                        lambda table, column: False if column in ("cusip", "isin") else
                        column in inventory._existing_columns(table))
    detail = inventory.ticker_detail("MU")
    assert detail["cusip"] == "n/a" and detail["isin"] == "n/a"


def test_the_identifiers_show_as_soon_as_the_columns_exist(rawstore_tmp_db):
    """CUSIP / ISIN values written on the master show in the drill."""
    _seed_every_dataset()
    rawstore.execute("UPDATE security_master SET cusip = ?, isin = ? WHERE ticker = ?",
                     ["594918104", "US5949181045", "MU"])

    detail = inventory.ticker_detail("MU")

    assert detail["cusip"] == "594918104"
    assert detail["isin"] == "US5949181045"


def test_the_master_cik_beats_the_fallback_but_a_missing_one_still_falls_back(rawstore_tmp_db):
    rawstore.write_rows("equity_universe", [{"ticker": "MU", "name": "Micron"}])
    rawstore.write_rows("security_master", [{"ticker": "MU", "figi": "BBG000BLNNH6"}])
    rawstore.write_rows("chain_filings", [
        {"ticker": "MU", "cik": "0000723125", "form": "10-K", "filed": "2026-02-01"}])

    assert inventory.ticker_detail("MU")["cik"] == "0000723125"

    rawstore.execute("UPDATE security_master SET cik = '0000000001' WHERE ticker = 'MU'")
    assert inventory.ticker_detail("MU")["cik"] == "0000000001"


def test_a_ticker_the_drill_cannot_find_is_still_none(rawstore_tmp_db):
    _seed_every_dataset()
    assert inventory.ticker_detail("ZZZZ") is None


def test_a_ticker_with_only_a_stored_quote_is_found(rawstore_tmp_db):
    """The drill reads the scoring datasets, so a name present only in one of them is not "unknown"."""
    rawstore.write_rows("quote_snapshot", [
        {"ticker": "ONLYQ", "session_date": date(2026, 10, 20), "source": "uw",
         "market_cap": 1.0e9, "run_at": 1.0}])

    detail = inventory.ticker_detail("ONLYQ")

    assert detail is not None
    assert detail["presence"]["quote_snapshot"] == {"rows": 1, "newest": "2026-10-20", "source": "uw"}


def test_the_edgar_newest_period_is_the_newest_annual_record_not_its_position(rawstore_tmp_db):
    """The rows were written oldest first; a row stored out of order still reports the newest end."""
    rawstore.write_rows("edgar_financials", [
        {"ticker": "OUT", "annual": [
            {"fiscal_year": "2026", "revenue": 2.0, "period_ends": {"revenue": "2026-08-27"}},
            {"fiscal_year": "2024", "revenue": 0.5, "period_ends": {"revenue": "2024-08-29"}}],
         "quarterly": [], "meta": {"source": "sec_edgar"}, "fetched_at": 1.0}])

    assert inventory.ticker_detail("OUT")["presence"]["edgar_financials"] == {
        "periods": 2, "newest": "2026-08-27"}


# ── G7 of the GUI brief (#124): which boards can score this name ────────────────────────────────
# A board lists the store tables it reads (``module_settings.boards.BOARD_INPUTS``). A board can
# score the name when it has a row in every table the board reads that this drill already
# measures. A board reading a table the drill does not measure says "not checked", never a guess.

def _boards_of(ticker: str) -> dict:
    return {b["key"]: b for b in inventory.ticker_detail(ticker)["boards"]}


def test_every_registered_board_answers_in_nav_order(rawstore_tmp_db):
    from RevenueChaser.module_settings import boards

    _seed_every_dataset()
    got = inventory.ticker_detail("MU")["boards"]

    assert [b["key"] for b in got] == [e["key"] for e in boards.BOARD_INPUTS]
    assert [b["label"] for b in got] == [e["label"] for e in boards.BOARD_INPUTS]


def test_a_board_whose_tables_are_all_measured_and_stored_can_score_the_name(rawstore_tmp_db):
    _seed_every_dataset()
    fit = _boards_of("MU")

    # Swing reads equity_universe and price_bars_daily; Fundamentals adds the quote and EDGAR
    # facts; Asymmetric Growth swaps the quote for ownership. MU has every one of them.
    for key in ("module_swing_potential", "module_fundamentals", "module_asymmetric_growth"):
        assert fit[key]["state"] == "can", key
        assert fit[key]["missing"] == []


def test_co_movement_scores_a_name_with_daily_bars_and_no_legacy_cache_row(rawstore_tmp_db):
    """Co-movement reads the daily price bars since #379 (``module_comovement/ohlc.py``), not
    ``swing_ohlc``: the seed has bars and no legacy-cache row, and the board can score the name."""
    _seed_every_dataset()
    assert rawstore.count("swing_ohlc") == 0
    fit = _boards_of("MU")

    assert fit["module_comovement"]["state"] == "can"
    assert fit["module_comovement"]["missing"] == []


def test_a_measured_table_with_no_row_makes_the_board_unable_and_names_what_is_missing(rawstore_tmp_db):
    """A universe name with no daily bar: Co-movement cannot score it and names the bars."""
    _seed_every_dataset()
    rawstore.execute("DELETE FROM price_bars_daily", db=rawstore.MANIFEST["price_bars_daily"].get("db", "raw"))
    fit = _boards_of("MU")

    assert fit["module_comovement"]["state"] == "cannot"
    assert fit["module_comovement"]["missing"] == ["daily price bars"]


def test_a_name_with_only_d01_bars_cannot_be_scored_by_the_boards_this_drill_measures(rawstore_tmp_db):
    rawstore.write_rows("price_bars_daily", _bars("NVDA", "2026-02-02"))
    fit = _boards_of("NVDA")

    assert fit["module_swing_potential"]["state"] == "cannot"
    assert fit["module_swing_potential"]["missing"] == ["equity universe membership"]
    assert fit["module_fundamentals"]["state"] == "cannot"
    assert fit["module_fundamentals"]["missing"] == ["equity universe membership", "quote",
                                                     "EDGAR facts"]
    assert fit["module_asymmetric_growth"]["missing"] == ["equity universe membership",
                                                          "EDGAR facts", "ownership"]


def test_a_board_reading_a_table_the_drill_does_not_measure_says_not_checked(rawstore_tmp_db):
    """Earnings Radar reads eight store tables; this drill measures one of them. Never a guess."""
    _seed_every_dataset()
    fit = _boards_of("MU")

    assert fit["module_earnings_radar"]["state"] == "not_checked"
    assert fit["module_earnings_radar"]["missing"] == []
    assert fit["module_sec_13f"]["state"] == "not_checked"
    assert fit["module_leaps"]["state"] == "not_checked"


def test_a_board_that_reads_no_store_table_says_not_checked(rawstore_tmp_db):
    _seed_every_dataset()
    assert _boards_of("MU")["module_uw_interaction"]["state"] == "not_checked"


def test_universe_membership_is_the_boards_own_cut_not_merely_a_row(rawstore_tmp_db):
    """A row below the $300M floor is in ``equity_universe`` and outside the list boards scan."""
    rawstore.write_rows("equity_universe", [{"ticker": "TINY", "market_cap": 1.0e6}])
    rawstore.write_rows("price_bars_daily", _bars("TINY", "2026-02-02"))

    fit = _boards_of("TINY")

    assert fit["module_swing_potential"] == {
        "key": "module_swing_potential", "label": "Swing", "state": "cannot",
        "missing": ["equity universe membership"]}


def test_the_board_list_costs_no_store_read(rawstore_tmp_db, monkeypatch):
    """G7: one pass over the presence the drill already read — no query per board."""
    _seed_every_dataset()
    detail = inventory.ticker_detail("MU")

    monkeypatch.setattr(rawstore, "query",
                        lambda *a, **k: pytest.fail("the board list must not read the store"))

    assert inventory._board_fit(detail["presence"]) == detail["boards"]

# ── #143: a failed presence query reads "could not read", never "missing" (the G3 rule) ─────────

def _fail_reads_of(monkeypatch, *tables):
    """Every per-name read of ``tables`` raises, as a locked or broken store file would."""
    real = rawstore.query

    def query(sql, *a, **k):
        if any(f"FROM {t} WHERE ticker" in sql for t in tables):
            raise RuntimeError("locked")
        return real(sql, *a, **k)

    monkeypatch.setattr(rawstore, "query", query)


def test_a_failed_bars_read_is_unreadable_not_missing_and_the_board_says_so(rawstore_tmp_db, monkeypatch):
    _seed_every_dataset()
    _fail_reads_of(monkeypatch, "price_bars_daily")
    detail = inventory.ticker_detail("MU")
    fit = {b["key"]: b for b in detail["boards"]}

    assert detail["presence"]["price_bars_daily"]["error"] is True
    assert fit["module_swing_potential"]["state"] == "unreadable"
    assert fit["module_swing_potential"]["missing"] == []
    assert fit["module_swing_potential"]["unreadable"] == ["daily price bars"]
    assert any(e["table"] == "price_bars_daily" for e in detail["errors"])
    # Every board that reads the bars says so; one that does not read them keeps its own state.
    assert fit["module_comovement"]["state"] == "unreadable"
    assert fit["module_sec_13f"]["state"] == "not_checked"


def test_a_failed_row_check_is_unknown_not_absent(rawstore_tmp_db, monkeypatch):
    _seed_every_dataset()
    rawstore.write_rows("oipd_meta", [{"ticker": "MU", "as_of": "2026-10-20"}])
    _fail_reads_of(monkeypatch, "oipd_meta")
    detail = inventory.ticker_detail("MU")

    assert detail["presence"]["oipd_meta"] is None
    fit = {b["key"]: b for b in detail["boards"]}
    assert fit["module_oipd"]["state"] == "unreadable"
    assert fit["module_oipd"]["unreadable"] == ["Implied Probability chain"]


def test_a_name_whose_reads_all_failed_is_not_reported_unknown(rawstore_tmp_db, monkeypatch):
    """Failed reads are not "absent from every dataset": the route must not answer 404."""
    rawstore.write_rows("price_bars_daily", _bars("NVDA", "2026-02-02"))
    _fail_reads_of(monkeypatch, "price_bars_daily")
    detail = inventory.ticker_detail("NVDA")

    assert detail is not None
    assert detail["presence"]["price_bars_daily"]["error"] is True


def test_a_clean_drill_reports_no_errors(rawstore_tmp_db):
    _seed_every_dataset()
    assert inventory.ticker_detail("MU")["errors"] == []


def test_a_failed_table_list_read_makes_that_files_tables_unreadable_not_absent(rawstore_tmp_db, monkeypatch):
    """A locked store file fails on its table list first: every table in it is unknown, not empty."""
    _seed_every_dataset()
    prices_db = rawstore.MANIFEST["price_bars_daily"].get("db", "raw")
    real = rawstore.query

    def query(sql, *a, **k):
        if "information_schema.tables" in sql and k.get("db") == prices_db:
            raise RuntimeError("locked")
        return real(sql, *a, **k)

    monkeypatch.setattr(rawstore, "query", query)
    detail = inventory.ticker_detail("MU")
    fit = {b["key"]: b for b in detail["boards"]}

    assert detail["presence"]["price_bars_daily"]["error"] is True
    assert fit["module_swing_potential"]["state"] == "unreadable"
    assert fit["module_swing_potential"]["unreadable"] == ["daily price bars"]
    assert any(e["table"] == f"db:{prices_db}" for e in detail["errors"])
    # Tables in the other files still answer.
    assert detail["presence"]["quote_snapshot"]["rows"] == 1
    assert "error" not in detail["presence"]["quote_snapshot"]


def test_a_failed_table_list_read_makes_a_row_check_unknown(rawstore_tmp_db, monkeypatch):
    _seed_every_dataset()
    raw_db = rawstore.MANIFEST["oipd_meta"].get("db", "raw")
    real = rawstore.query

    def query(sql, *a, **k):
        if "information_schema.tables" in sql and k.get("db", "raw") == raw_db:
            raise RuntimeError("locked")
        return real(sql, *a, **k)

    monkeypatch.setattr(rawstore, "query", query)
    assert inventory.ticker_detail("MU")["presence"]["oipd_meta"] is None


def test_a_missing_readable_table_says_cannot_even_when_another_read_failed(rawstore_tmp_db, monkeypatch):
    """No bar for the name is known; the failed quote read must not hide it behind "unknown"."""
    _seed_every_dataset()
    rawstore.execute("DELETE FROM price_bars_daily", db=rawstore.MANIFEST["price_bars_daily"].get("db", "raw"))
    from RevenueChaser.core import quotes
    monkeypatch.setattr(quotes, "latest_caps", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked")))
    fit = {b["key"]: b for b in inventory.ticker_detail("MU")["boards"]}

    assert fit["module_fundamentals"]["state"] == "cannot"
    assert fit["module_fundamentals"]["missing"] == ["daily price bars"]


def test_a_failed_row_check_is_recorded_once(rawstore_tmp_db, monkeypatch):
    """A name in no other dataset reaches the row checks twice (the unknown-name test, then the
    presence): one read, so one failure on the list."""
    _seed_every_dataset()
    _fail_reads_of(monkeypatch, "oipd_meta")
    errors = inventory.ticker_detail("ZZZZ")["errors"]
    assert [e["table"] for e in errors].count("oipd_meta") == 1



def test_a_failed_read_of_a_table_the_unknown_name_check_does_not_read_keeps_the_404(rawstore_tmp_db, monkeypatch):
    """#205 review: any recorded failure blocked the 404, so a failure elsewhere in the call turned a name in no
    dataset into an empty drill. Only a failed read of a table the check itself reads makes the name unknown."""
    _seed_every_dataset()
    real = inventory._sec_file_row

    def sec_file_row(ticker):
        inventory._record("index_constituents", "_index_floor", RuntimeError("locked"))
        return real(ticker)

    monkeypatch.setattr(inventory, "_sec_file_row", sec_file_row)
    assert inventory.ticker_detail("ZZZZ") is None
    assert inventory.ticker_detail("MU") is not None


def test_a_failed_table_list_of_a_file_the_check_reads_makes_an_unknown_name_unknown(rawstore_tmp_db, monkeypatch):
    """The other half of the rule: a file whose table list failed reads every one of its tables as missing, so a
    name in no other dataset is unknown, not a 404."""
    _seed_every_dataset()
    raw_db = rawstore.MANIFEST["oipd_meta"].get("db", "raw")
    real = rawstore.query

    def query(sql, *a, **k):
        if "information_schema.tables" in sql and k.get("db", "raw") == raw_db:
            raise RuntimeError("locked")
        return real(sql, *a, **k)

    monkeypatch.setattr(rawstore, "query", query)
    assert inventory.ticker_detail("ZZZZ") is not None
