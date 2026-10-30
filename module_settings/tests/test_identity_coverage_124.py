"""Identity coverage (S7 of the Data Health plan, #124): the funnel, the inputs, the identifiers.

Three objects, counted over the same population:

* the universe funnel — the SEC ticker and exchange file's rows down to
  ``universe.classification_universe()``, every drop by design (and the one read that is not:
  a name with no market cap stored);
* the inputs the surviving names carry (CIK, quote, bars, CUSIP, FIGI, EDGAR facts, annual
  filing, ownership);
* the identifier matrix — each row naming its own denominator, the CUSIP row expanded into
  mapped / asked and answered empty / never asked, and the two "unmatched" counts labelled
  (the store's and the 13F board's).

Store only: the seeded store is a temp DuckDB per store file, no provider is asked.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from RevenueChaser.core import funnel as funnel_mod
from RevenueChaser.core import rawstore
from RevenueChaser.core import universe
from RevenueChaser.module_settings import inventory

# No test touches today's date: this clock is a Thursday, 08:00 in New York, so the latest
# complete session is the Wednesday before it.
FIXED = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
SESSION = "2026-09-30"


def _seed() -> None:
    """A store with one of every funnel outcome and one of every input present for some names.

    The funnel, in order: 8 present rows in the SEC file; 2 outside Nasdaq / NYSE / Cboe;
    1 listed name the equity universe does not hold; 1 below the old $300M floor; 1 row with no
    market cap; 1 exchange-traded fund. With no floor and every type kept (PR 2) the funnel keeps AAPL, MU,
    SMALL, NOCAP and FUND — exactly ``classification_universe()``; FUND is a type note, not a drop.
    """
    rawstore.write_rows("equity_universe", [
        {"ticker": "AAPL", "market_cap": 3.0e12, "name": "Apple"},
        {"ticker": "MU", "market_cap": 1.0e11, "name": "Micron Technology"},
        {"ticker": "SMALL", "market_cap": 2.0e8, "name": "Too Small"},
        {"ticker": "NOCAP", "market_cap": None, "name": "No Cap Stored"},
        {"ticker": "FUND", "market_cap": 5.0e9, "name": "An Exchange-Traded Fund"},
    ])
    rawstore.write_rows("security_master", [
        {"ticker": "AAPL", "security_type": "operating", "cusip": "037833100",
         "isin": "US0378331005", "figi": "BBG000B9XRY4"},
        {"ticker": "MU", "security_type": "operating", "cusip": "595112103",
         "isin": "US5951121036", "figi": None},
        {"ticker": "SMALL", "security_type": "operating"},
        {"ticker": "NOCAP", "security_type": "operating"},
        {"ticker": "FUND", "security_type": "etp"},
    ])
    rawstore.write_rows("sec_exchange_file", [
        {"ticker": "AAPL", "cik": "0000320193", "name": "Apple", "exchange": "Nasdaq",
         "status": "present", "run_at": 2.0},
        {"ticker": "MU", "cik": "0000723125", "name": "Micron Technology", "exchange": "Nasdaq",
         "status": "present", "run_at": 2.0},
        {"ticker": "SMALL", "cik": "0000000003", "name": "Too Small", "exchange": "NYSE",
         "status": "present", "run_at": 2.0},
        {"ticker": "NOCAP", "cik": "0000000004", "name": "No Cap Stored", "exchange": "NYSE",
         "status": "present", "run_at": 2.0},
        {"ticker": "FUND", "cik": "0000000005", "name": "A Fund", "exchange": "NYSE",
         "status": "present", "run_at": 2.0},
        {"ticker": "LISTEDONLY", "cik": "0000000006", "name": "Listed Only", "exchange": "NYSE",
         "status": "present", "run_at": 2.0},
        {"ticker": "OTC1", "cik": "0000000007", "name": "Over The Counter", "exchange": "OTC",
         "status": "present", "run_at": 2.0},
        {"ticker": "NOWHERE", "cik": "0000000008", "name": "No Venue", "exchange": "",
         "status": "present", "run_at": 2.0},
        # gone from the file: the newest row is 'absent', so it is not a present row at all
        {"ticker": "GONE", "cik": "0000000009", "name": "Gone", "exchange": "Nasdaq",
         "status": "absent", "run_at": 2.0},
    ])
    rawstore.write_rows("quote_snapshot", [
        {"ticker": "AAPL", "session_date": SESSION, "source": "yfinance", "market_cap": 3.0e12},
        {"ticker": "MU", "session_date": SESSION, "source": "uw", "market_cap": 1.0e11},
    ])
    # AAPL has a bar on the latest complete session; MU's newest bar is a day behind it.
    rawstore.write_rows("price_bars_daily", [
        {"ticker": "AAPL", "session_date": SESSION, "close": 1.0, "source": "yfinance"},
        {"ticker": "MU", "session_date": "2026-09-29", "close": 1.0, "source": "yfinance"},
    ])
    rawstore.write_rows("chain_filings", [
        {"ticker": "MU", "cik": "0000723125", "form": "10-K", "filed": "2026-01-15"},
    ])
    rawstore.write_rows("edgar_financials", [
        {"ticker": "AAPL", "annual": [], "quarterly": [], "fetched_at": 1.0},
    ])
    rawstore.write_rows("ownership_snapshot", [
        {"ticker": "AAPL", "session_date": SESSION, "source": "uw", "institutional_pct": 60.0},
    ])


def _seed_cusip_map() -> None:
    """Two CUSIPs the store has asked about (one mapped, one empty) and one the filings hold unmapped."""
    rawstore.write_rows("identity_cusip", [
        {"cusip": "037833100", "status": "present", "ticker": "AAPL", "source": "openfigi",
         "run_at": 2.0},
        {"cusip": "111111111", "status": "absent", "ticker": None, "source": "openfigi",
         "run_at": 2.0},
    ])
    rawstore.write_rows("cusip_ticker", [
        {"cusip": "037833100", "entry": {"ticker": "AAPL", "source": "openfigi"}},
    ])
    rawstore.write_rows("sec13f_submissions", [
        {"accession": "acc-1", "cik": "0000000001", "period": "2026-06-30",
         "filing_date": "2026-08-10", "type": "13F-HR", "report_type": "13F-HR"},
    ])
    rawstore.write_rows("sec13f_holdings", [
        {"accession": "acc-1", "line_no": 1, "cusip": "037833100", "cik": "0000000001",
         "shares": 10.0, "value": 1000.0, "period": "2026-06-30", "sshprnamttype": "SH"},
        {"accession": "acc-1", "line_no": 2, "cusip": "999999999", "cik": "0000000001",
         "shares": 5.0, "value": 500.0, "period": "2026-06-30", "sshprnamttype": "SH"},
    ])


def _coverage() -> dict:
    return inventory.identity_coverage(universe.classification_universe(), FIXED)


def _steps(cov: dict) -> dict[str, dict]:
    return {s["key"]: s for s in cov["funnel"]}


def _rows(cov: dict, part: str) -> dict[str, dict]:
    return {r["key"]: r for r in cov[part]}


# ── the funnel ─────────────────────────────────────────────────────────────
def test_the_funnel_result_is_the_classification_universe(rawstore_tmp_db):
    """Plan CI test 4: ``start - sum(drops)`` equals the result, and the result is what the boards read."""
    _seed()
    cov = _coverage()
    steps = cov["funnel"]
    assert funnel_mod.validate(steps) == [], "the funnel must add up: no name lost off the books"
    kept = universe.classification_universe()
    # No market-cap floor (avo, 29 Oct 2026): SMALL and the NULL-cap name are universe names too; every security
    # type is (PR 2, avo 30 Oct 2026), so the fund is as well.
    assert sorted(kept) == ["AAPL", "FUND", "MU", "NOCAP", "SMALL"]
    assert steps[-1]["kind"] == "result" and steps[-1]["count"] == len(kept) == 5
    assert steps[0]["kind"] == "start" and steps[0]["count"] == 8, "8 present rows in the SEC file"
    drops = sum(s["count"] for s in steps if s["kind"] == "drop")
    assert steps[0]["count"] - drops == steps[-1]["count"]
    assert cov["names_kept"] == len(kept)


def test_every_drop_is_named_in_plain_words_and_carries_its_count(rawstore_tmp_db):
    _seed()
    steps = _steps(_coverage())
    assert steps["venues"]["count"] == 2, "OTC and no-venue rows leave at the venue step"
    assert "Nasdaq" in steps["venues"]["label"] and "Cboe" in steps["venues"]["label"]
    assert steps["universe"]["count"] == 1, "LISTEDONLY is not in the equity universe"
    assert steps["floor"]["count"] == 0, "no floor is set, so nothing is below it"
    assert "no floor is set" in steps["floor"]["label"]
    assert steps["no_cap"]["count"] == 0 and steps["no_cap"]["by_design"] is False, \
        "at floor 0 a missing cap keeps the name, so the row drops nobody"
    assert steps["funds_etfs"]["count"] == 1, "the exchange-traded fund is counted in the kept names' type mix"
    assert steps["funds_etfs"]["kind"] == "note", "PR 2: a type is data, never a drop"
    assert steps["funds_etfs"]["label"] == "Kept, of which: funds and exchange-traded funds"
    assert steps["universe"]["by_design"] is False, \
        "a listed name the universe does not hold is the universe being behind, not a rule"
    assert "spac" not in steps["spacs"]["label"].lower(), "plain words, no code short forms"
    for key in ("venues", "floor"):
        assert steps[key]["by_design"] is True, key
        assert "spac" not in steps[key]["label"].lower(), "plain words, no code short forms"


def test_a_kept_name_the_sec_file_does_not_list_is_said_not_counted(rawstore_tmp_db):
    """A name the classification rule keeps that the file does not list is a note, never a dropped name."""
    _seed()
    rawstore.write_rows("equity_universe", [
        {"ticker": "AAPL", "market_cap": 3.0e12, "name": "Apple"},
        {"ticker": "MU", "market_cap": 1.0e11, "name": "Micron Technology"},
        {"ticker": "SMALL", "market_cap": 2.0e8, "name": "Too Small"},
        {"ticker": "NOCAP", "market_cap": None, "name": "No Cap Stored"},
        {"ticker": "FUND", "market_cap": 5.0e9, "name": "An Exchange-Traded Fund"},
        {"ticker": "LEFTOVER", "market_cap": 8.0e9, "name": "Kept But Unlisted"},
    ])
    cov = _coverage()
    steps = _steps(cov)
    assert steps["unreached"]["kind"] == "note" and steps["unreached"]["count"] == 1
    assert steps["kept"]["count"] == 5, "the chain's own result, not the classification count"
    assert cov["names_kept"] == 6
    assert funnel_mod.validate(cov["funnel"]) == []


def test_an_unreadable_sec_file_has_no_funnel_and_says_so(rawstore_tmp_db, monkeypatch):
    _seed()
    monkeypatch.setattr(inventory, "_present_file_rows", lambda: None)
    cov = _coverage()
    assert cov["funnel"] == []
    assert "could not be read" in cov["funnel_note"]


def test_an_empty_sec_file_is_not_a_funnel_of_zero(rawstore_tmp_db):
    """Nothing stored is not "nothing survives": the funnel is not drawn at all."""
    _seed()
    rawstore.execute("DELETE FROM sec_exchange_file")
    cov = _coverage()
    assert cov["funnel"] == []
    assert "not stored yet" in cov["funnel_note"]


# ── the inputs ─────────────────────────────────────────────────────────────
def test_the_inputs_are_the_eight_the_plan_names_in_order(rawstore_tmp_db):
    _seed()
    cov = _coverage()
    assert [r["key"] for r in cov["inputs"]] == [
        "cik", "quotes", "bars", "cusip", "figi", "facts", "annual", "ownership"]


def test_every_input_row_names_its_own_denominator(rawstore_tmp_db):
    _seed()
    for row in _coverage()["inputs"]:
        assert row["denominator"] == "5 names the funnel keeps", row["key"]
        assert row["expected"] == 5, row["key"]
        assert row["label"] and row["label"][0].isupper(), row["key"]


def test_the_inputs_count_the_names_that_carry_each_one(rawstore_tmp_db):
    _seed()
    rows = _rows(_coverage(), "inputs")
    assert (rows["cik"]["covered"], rows["cik"]["state"]) == (5, "")      # FUND files with the SEC too
    assert rows["quotes"]["covered"] == 2, "both names have a quote on the newest session"
    assert rows["bars"]["covered"] == 1, "MU's newest bar is behind the latest complete session"
    assert rows["cusip"]["covered"] == 2 and rows["figi"]["covered"] == 1
    assert rows["facts"]["covered"] == 1 and rows["annual"]["covered"] == 1
    assert rows["ownership"]["covered"] == 1


def test_a_quote_the_store_never_stored_is_a_zero_not_an_error(rawstore_tmp_db):
    _seed()
    rawstore.execute("DELETE FROM quote_snapshot")
    row = _rows(_coverage(), "inputs")["quotes"]
    assert (row["covered"], row["state"]) == (0, ""), "an empty store covers nobody: that is a real zero"


def test_a_failed_read_says_unreadable_never_zero(rawstore_tmp_db, monkeypatch):
    """A read that raises is not "no data": the row carries the unreadable state, so the page says so."""
    _seed()
    from RevenueChaser.core import quotes
    monkeypatch.setattr(quotes, "health_summary", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked")))
    row = _rows(_coverage(), "inputs")["quotes"]
    assert row["covered"] is None and row["state"] == "unreadable"


def test_an_input_table_the_store_lacks_counts_zero(rawstore_tmp_db):
    """A table this store does not carry stores nothing, so it covers no names — not an error."""
    _seed()
    rawstore.execute("DROP TABLE ownership_snapshot")
    rawstore._TABLE_READY.clear()
    row = _rows(_coverage(), "inputs")["ownership"]
    assert (row["covered"], row["state"]) == (0, "")


# ── the identifier matrix ──────────────────────────────────────────────────
def test_the_isin_row_is_present_at_its_true_count_including_zero(rawstore_tmp_db):
    _seed()
    row = _rows(_coverage(), "identifiers")["isin"]
    assert (row["covered"], row["expected"], row["state"]) == (2, 2, "")
    rawstore.execute("UPDATE security_master SET isin = NULL")
    row = _rows(_coverage(), "identifiers")["isin"]
    assert (row["covered"], row["expected"], row["state"]) == (0, 2, ""), \
        "zero ISINs is a real count, not a missing row"
    assert "CUSIP" in row["denominator"], "an ISIN is derived from a CUSIP: that is its denominator"


def test_the_cusip_row_expands_into_mapped_asked_empty_and_never_asked(rawstore_tmp_db):
    _seed()
    _seed_cusip_map()
    rows = _rows(_coverage(), "identifiers")
    assert rows["cusip_mapped"]["covered"] == 1, "one stored answer names a ticker"
    assert rows["cusip_mapped"]["expected"] == 2, "of the 2 CUSIPs the store has asked about"
    assert rows["cusip_empty"]["covered"] == 1, "asked and answered empty"
    assert rows["cusip_never_asked"]["covered"] == 1, "held by a filing, never asked"
    assert rows["cusip_never_asked"]["expected"] == 3, "asked plus waiting"


def test_the_two_unmatched_counts_are_labelled_apart(rawstore_tmp_db):
    """The store's own count and the 13F board's post-filter count are different numbers: both are named."""
    _seed()
    _seed_cusip_map()
    rows = _rows(_coverage(), "identifiers")
    assert rows["cusip_empty"]["covered"] == 1
    assert "store" in rows["cusip_empty"]["label"].lower()
    board = rows["sec13f_left_out"]
    assert board["covered"] == 1, "the newest period holds one CUSIP with no ticker"
    assert "13F" in board["label"]
    assert board["denominator"] == "not measured", "the period's CUSIP total is not stored"


def test_no_cusip_map_stored_is_zero_asked_not_an_error(rawstore_tmp_db):
    _seed()
    rawstore.execute("DELETE FROM identity_cusip")
    rows = _rows(_coverage(), "identifiers")
    assert rows["cusip_mapped"]["covered"] == 0 and rows["cusip_mapped"]["expected"] == 0
    assert rows["cusip_never_asked"]["covered"] == 0


def test_a_failed_cusip_read_says_unreadable(rawstore_tmp_db, monkeypatch):
    _seed()
    from RevenueChaser.core import identity_cusip
    monkeypatch.setattr(identity_cusip, "latest",
                        lambda: (_ for _ in ()).throw(RuntimeError("locked")))
    rows = _rows(_coverage(), "identifiers")
    assert rows["cusip_mapped"]["covered"] is None and rows["cusip_mapped"]["state"] == "unreadable"


def test_a_failed_map_queue_read_never_counts_the_waiting_cusips_as_zero(rawstore_tmp_db, monkeypatch):
    """The map answered; the 13F queue under it could not be read: that row is unreadable, not 0.

    The 13F board's own count is a different read and still comes through.
    """
    _seed()
    _seed_cusip_map()
    from RevenueChaser.module_sec_13f import engine as sec_13f
    monkeypatch.setattr(sec_13f, "map_candidates",
                        lambda: (_ for _ in ()).throw(RuntimeError("locked")))
    rows = _rows(_coverage(), "identifiers")
    assert rows["cusip_never_asked"]["covered"] is None
    assert rows["cusip_never_asked"]["state"] == "unreadable"
    assert rows["sec13f_left_out"]["covered"] == 1, "a different read still answers"


def test_a_failed_13f_summary_read_says_unreadable_never_zero(rawstore_tmp_db, monkeypatch):
    _seed()
    _seed_cusip_map()
    from RevenueChaser.module_sec_13f import engine as sec_13f
    monkeypatch.setattr(sec_13f, "unmatched_summary",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked")))
    rows = _rows(_coverage(), "identifiers")
    assert rows["sec13f_left_out"]["covered"] is None
    assert rows["sec13f_left_out"]["state"] == "unreadable"
    assert rows["cusip_never_asked"]["covered"] == 1, "the map queue still answers"


# ── cost ───────────────────────────────────────────────────────────────────
def test_the_section_asks_each_store_summary_once_per_collect(rawstore_tmp_db, monkeypatch):
    """The section adds reads, not a second full sweep of the two summary files.

    Counted against the same collect without the section: whatever the page asked before, the
    section must not raise that number.
    """
    _seed()
    from RevenueChaser.core import prices, quotes
    calls = {"prices": 0, "quotes": 0}
    real = {"prices": prices.health_summary, "quotes": quotes.health_summary}

    def counted(name):
        def run(*a, **k):
            calls[name] += 1
            return real[name](*a, **k)
        return run

    monkeypatch.setattr(prices, "health_summary", counted("prices"))
    monkeypatch.setattr(quotes, "health_summary", counted("quotes"))

    payload = inventory.collect(now=FIXED)
    assert payload["identity_coverage"]["inputs"], "the section was built"
    with_section = dict(calls)

    calls.update(prices=0, quotes=0)
    monkeypatch.setattr(inventory, "identity_coverage", lambda tickers, now=None: {
        "names_kept": len(tickers), "funnel": [], "funnel_note": "", "inputs": [], "identifiers": []})
    inventory.collect(now=FIXED)
    without_section = dict(calls)

    assert with_section == without_section, "the section reused the summaries the rows already read"



def test_the_whole_collect_stays_under_the_budget_on_a_seeded_store(rawstore_tmp_db):
    """The section adds reads, not screens: the collect is measured, and it stays well under 2.5 s."""
    _seed()
    _seed_cusip_map()
    payload = inventory.collect(now=FIXED)
    assert payload["identity_coverage"]["funnel"], "the section is part of the payload"
    assert payload["timing"]["collect_seconds"] < 2.5
