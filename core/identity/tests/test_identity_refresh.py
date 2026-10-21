"""The identity refresh orchestration (issue #85 Part 3, #265, #272): deltas, the never-delete SEC map, the S&P 1500
diff gate, the SPDR check, the ``None`` vs ``{}`` input contract, partial failure, and the run record. Sources are
injected fakes."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from RevenueChaser.core import index_registry, rawstore
from RevenueChaser.core.identity import apply as universe_apply
from RevenueChaser.core.identity import cache, master, refresh as R
from RevenueChaser.core.identity.sources import SourceResult

NOW = "2026-09-30T00:00:00Z"
LATER = "2026-10-05T00:00:00Z"
LATEST = "2026-10-12T00:00:00Z"
OLD_10D = "2026-09-20T00:00:00Z"
OLD_40D = "2026-08-21T00:00:00Z"
OLD_100D = "2026-06-22T00:00:00Z"

UNIVERSE = [("AAPL", "Apple Inc.", 3.0e12), ("PDI", "PIMCO Dynamic Income Fund", 2.0e9),
            ("ZZZ", "Zed Zed Corp", 1.0e9)]


@pytest.fixture(autouse=True)
def store():
    rawstore.write_rows("equity_universe", [{"ticker": t, "name": n, "market_cap": c} for t, n, c in UNIVERSE])
    index_registry.save_index("sp500", {"AAPL"})        # the SPDR check reads its members from here


def ok(source, rows, at=NOW):
    return SourceResult(source, "ok", rows, None, at)


def bad(source, rows=(), at=NOW):
    return SourceResult(source, "error", list(rows), "boom", at)


class Fakes:
    """Injectable source functions that record how they were called."""

    def __init__(self):
        self.calls: dict[str, list] = {}
        self.clock = NOW            # the fetch time the default sec_submissions answer is stamped with
        self.results: dict[str, object] = {
            "sec_exchange": ok("sec_exchange", [
                {"ticker": "AAPL", "cik": "320193", "name": "Apple Inc.", "exchange": "Nasdaq"},
                {"ticker": "PDI", "cik": "1510281", "name": "PIMCO Dynamic Income Fund", "exchange": "NYSE"},
                {"ticker": "ZZZ", "cik": "999", "name": "Zed Zed", "exchange": "NYSE"}]),
            "nasdaq": ok("nasdaq", [{"ticker": "AAPL", "security_name": "Apple Inc. - Common Stock",
                                     "is_etf": False, "exchange": "Q"},
                                    {"ticker": "PDI", "security_name": "PIMCO Dynamic Income Fund",
                                     "is_etf": False, "exchange": "N"}]),
            "sp1500": ok("sp1500", []),          # a healthy list with none of the universe on it; tests add rows
            "uw": ok("uw_sector", [
                {"ticker": "AAPL", "sector": "Technology", "industry": "Consumer Electronics",
                 "security_type": "Common Stock", "market_cap": 3.0e12, "next_earnings_date": "2026-10-30"},
                {"ticker": "PDI", "sector": "Financial Services", "industry": "Asset Management",
                 "security_type": "Common Stock", "market_cap": 2.0e9}]),
            "spdr": ok("spdr", [{"ticker": "AAPL", "name": "Apple Inc.", "fund": "XLK",
                                 "sector": "Technology", "weight": 14.0}]),
        }

    def _log(self, name, *args):
        self.calls.setdefault(name, []).append(args)

    def sec_exchange(self):
        self._log("sec_exchange")
        return self.results["sec_exchange"]

    def sec_submissions(self, ciks, ticker_by_cik=None):
        self._log("sec_submissions", sorted(int(c) for c in ciks), dict(ticker_by_cik or {}))
        if "sec_submissions" in self.results:
            return self.results["sec_submissions"]
        forms = {"320193": ["10-K", "10-Q"], "1510281": ["N-CSR", "NPORT-P"], "999": ["10-K"]}
        rows = []
        for c in sorted(int(c) for c in ciks):
            t = (ticker_by_cik or {}).get(c)
            r = {"cik": str(c), "status": "ok", "name": f"NAME {c}", "sic": "3571",
                 "sic_description": "Electronic Computers", "forms": forms.get(str(c), ["10-K"]), "shell_closed": False}
            if t:
                r["ticker"] = t
            rows.append(r)
        return ok("sec_submissions", rows, self.clock)

    def openfigi(self, tickers):
        tickers = sorted(tickers)
        self._log("openfigi", tickers)
        if "openfigi" in self.results:
            return self.results["openfigi"]
        types = {"AAPL": ("Common Stock", "Common Stock"), "PDI": ("Closed-End Fund", "Mutual Fund")}
        rows = []
        for t in tickers:
            st = types.get(t)
            row = {"ticker": t, "status": "ok" if st else "no_match", "security_type": st and st[0],
                   "security_type2": st and st[1], "name": f"{t} FIGI"}
            if st:
                row.update(figi=f"BBG-{t}", composite_figi=f"BBG-{t}-C", share_class_figi=f"BBG-{t}-S",
                           exch_code="US", security_description=t)
            rows.append(row)
        return ok("openfigi", rows)

    def nasdaq(self):
        self._log("nasdaq")
        return self.results["nasdaq"]

    def sp1500(self):
        self._log("sp1500")
        return self.results["sp1500"]

    def uw(self):
        self._log("uw")
        return self.results["uw"]

    def yahoo(self, tickers):
        tickers = list(tickers)
        self._log("yahoo", tickers)
        return ok("yahoo_sector", [{"ticker": t, "status": "ok", "sector": "Healthcare", "industry": "Biotechnology"}
                                   for t in tickers])

    def fdic(self, names):
        self._log("fdic", dict(names))
        return ok("fdic", [{"ticker": t, "status": "no_match"} for t in names])

    def spdr(self):
        self._log("spdr")
        return self.results["spdr"]

    def clients(self):
        return {"sec_exchange": self.sec_exchange, "sec_submissions": self.sec_submissions,
                "openfigi": self.openfigi, "nasdaq": self.nasdaq, "sp1500": self.sp1500, "uw": self.uw,
                "yahoo": self.yahoo, "fdic": self.fdic, "spdr": self.spdr}


@pytest.fixture
def fk():
    return Fakes()


def run(fk, now=NOW, **kw):
    return R.refresh(now=now, clients=fk.clients(), **kw)


def master_rows():
    return {r["ticker"]: r for r in rawstore.query("SELECT * FROM security_master")}


def universe_hash():
    """Every column the identity refresh must NOT write (it writes sector, industry, the two NULLed GICS columns
    and the earnings date, nothing else)."""
    rows = rawstore.query("SELECT ticker, name, market_cap, price, quote FROM equity_universe ORDER BY ticker")
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def universe_gics():
    """``{ticker: (sector, industry)}`` as stored in the universe."""
    return {r["ticker"]: (r["sector"], r["industry"]) for r in rawstore.query(
        "SELECT ticker, sector, industry FROM equity_universe")}


def seed_cache(source, rows, at):
    cache.save_results([SourceResult(source, "ok", rows, None, at)])


# ── the happy path and the run record ─────────────────────────────────────────────────────────
def test_fills_master_and_records_the_run(fk):
    before = universe_hash()
    res = run(fk)
    assert res["status"] == "ok"
    m = master_rows()
    assert set(m) == {"AAPL", "PDI", "ZZZ"}
    assert m["AAPL"]["security_type"] == "common" and m["AAPL"]["sector"] == "Technology"
    assert m["AAPL"]["sector_source"] == "provider_uw" and m["AAPL"]["cik"] == "320193"
    assert (m["AAPL"]["industry"], m["AAPL"]["industry_source"]) == ("Consumer Electronics", "provider_uw")
    assert m["AAPL"]["industry_group"] is None and m["AAPL"]["sub_industry"] is None
    assert m["PDI"]["security_type"] == "cef" and m["PDI"]["exchange"] == "NYSE"
    assert (m["AAPL"]["figi"], m["AAPL"]["composite_figi"], m["AAPL"]["share_class_figi"], m["AAPL"]["exch_code"],
            m["AAPL"]["security_description"], m["AAPL"]["figi_source"]) == (
        "BBG-AAPL", "BBG-AAPL-C", "BBG-AAPL-S", "US", "AAPL", "openfigi")
    assert m["ZZZ"]["figi"] is None and m["ZZZ"]["figi_source"] is None     # OpenFIGI had no match
    runs = rawstore.query("SELECT * FROM identity_runs")
    assert len(runs) == 1 and runs[0]["status"] == "ok" and runs[0]["run_id"] == res["run_id"]
    assert runs[0]["started_at"] == NOW and runs[0]["finished_at"]
    src = json.loads(runs[0]["sources"]) if isinstance(runs[0]["sources"], str) else runs[0]["sources"]
    assert set(src) == {"sec_exchange", "sec_submissions", "openfigi", "nasdaq", "sp1500", "uw_sector",
                        "yahoo_sector", "fdic", "spdr"}
    assert src["nasdaq"]["status"] == "ok" and src["nasdaq"]["rows"] == 2
    assert src["openfigi"]["fetched"] == 3
    counts = json.loads(runs[0]["counts"]) if isinstance(runs[0]["counts"], str) else runs[0]["counts"]
    assert counts["tickers"] == 3 and counts["by_type"]["cef"] == 1
    assert counts["by_sector_source"] == {"provider_uw": 2, "provider_yf": 1}
    assert universe_hash() == before, "the identity refresh must write only the classification and the date"


def test_the_master_carries_the_raw_sic_group_from_the_submissions_cache(fk):
    run(fk)
    m = master_rows()
    for t in ("AAPL", "PDI", "ZZZ"):
        assert (m[t]["sic"], m[t]["sic_description"], m[t]["sic_group2"], m[t]["sic_source"]) == (
            "3571", "Electronic Computers", "35", "sec_submissions"), t
    cols = {r["column_name"] for r in rawstore.query(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'equity_universe'")}
    assert not cols & {"sic", "sic_description", "sic_group2", "sic_source"}, "the SIC group stays on the master"


def test_the_raw_sic_is_read_from_the_caches_and_makes_no_new_sec_call(fk):
    run(fk)
    fk.calls.clear()
    run(fk, now=LATER)
    assert not fk.calls.get("sec_submissions") or fk.calls["sec_submissions"][0][0] == [], "no EDGAR fetch for it"
    assert master_rows()["AAPL"]["sic"] == "3571" and master_rows()["AAPL"]["sic_group2"] == "35"


def test_a_ticker_the_submissions_cache_has_no_code_for_takes_the_stores_sic(fk):
    rawstore.write_rows("sec_company_sic", [{"ticker": "AAPL", "cik": "320193", "sic": "3571",
                                             "sic_description": "Electronic Computers", "fetched_at": 1.0},
                                            {"ticker": "ZZZ", "cik": "999", "sic": "2834",
                                             "sic_description": "Pharmaceutical Preparations", "fetched_at": 1.0}])
    res = run(fk, sources=["nasdaq"])               # the SEC sources are skipped and nothing is cached for them
    assert res["sources"]["sec_submissions"]["status"] == "skipped"
    m = master_rows()
    assert (m["ZZZ"]["sic"], m["ZZZ"]["sic_description"], m["ZZZ"]["sic_group2"], m["ZZZ"]["sic_source"]) == (
        "2834", "Pharmaceutical Preparations", "28", "sec_company_sic")
    assert (m["ZZZ"]["sector"], m["ZZZ"]["sector_source"]) == ("Healthcare", "sic_explicit")
    assert m["PDI"]["sic"] is None and m["PDI"]["sic_source"] is None


def test_a_cached_openfigi_row_from_before_the_extra_fields_still_fills_the_figi(fk):
    seed_cache("openfigi", [{"ticker": "AAPL", "status": "ok", "security_type": "Common Stock",
                             "security_type2": "Common Stock", "name": "APPLE INC", "figi": "BBG000B9XRY4"}],
               "2026-09-20T00:00:00Z")
    run(fk)
    m = master_rows()["AAPL"]
    assert (m["figi"], m["composite_figi"], m["share_class_figi"]) == ("BBG000B9XRY4", None, None)
    assert m["figi_source"] == "openfigi"


def test_summary_line_reports_counts(fk):
    line = R.summary(run(fk))
    assert "3 tickers" in line and "sources ok 7/7" in line and "conflicts" in line
    assert "sectors: S&P 0 / UW 2 / Yahoo 1 / SIC 0 / unclassified 0" in line
    assert "universe 3 updated / 0 without a master row" in line


def test_summary_line_counts_the_list_and_the_sic_fallback(fk):
    fk.results["sp1500"] = ok("sp1500", [{"ticker": "AAPL", "sector": "Information Technology",
                                          "sub_industry": "Technology Hardware, Storage & Peripherals",
                                          "index": "sp500", "name": "Apple"}])
    cl = fk.clients()
    cl["yahoo"] = lambda tickers: ok("yahoo_sector", [])          # ZZZ: no UW, no Yahoo -> its SIC decides
    line = R.summary(R.refresh(now=NOW, clients=cl))
    assert "sectors: S&P 1 / UW 1 / Yahoo 0 / SIC 1 / unclassified 0" in line, line


def test_summary_line_copes_with_a_record_that_has_no_apply_counts():
    rec = {"status": "ok", "counts": {"tickers": 1, "by_type": {}, "by_sector_source": {}, "unclassified": 0,
                                      "conflicts": 0}, "sources": {s: {"selected": False, "status": "skipped"}
                                                                   for s in R.SOURCES}}
    assert "universe" not in R.summary(rec)


def test_limit_takes_the_first_n_universe_tickers(fk):
    run(fk, limit=2)
    assert set(master_rows()) == {"AAPL", "PDI"}


def test_sources_filter_runs_only_the_named_sources(fk):
    res = run(fk, sources=["nasdaq"])
    assert set(fk.calls) == {"nasdaq"}
    assert res["sources"]["nasdaq"]["status"] == "ok" and res["sources"]["openfigi"]["status"] == "skipped"


# ── the SEC exchange map: upsert only, never delete ───────────────────────────────────────────
def test_sec_map_inserts_new_and_updates_changed_ciks_and_never_deletes(fk):
    rawstore.write_rows("sec_company_tickers", [
        {"ticker": "AAPL", "cik": "111", "name": "Apple old"},
        {"ticker": "GONE", "cik": "5", "name": "Delisted Co"},
        {"ticker": "PDI", "cik": "1510281", "name": "PIMCO Dynamic Income Fund"}])
    run(fk)
    rows = {r["ticker"]: r for r in rawstore.query("SELECT * FROM sec_company_tickers")}
    assert rows["AAPL"]["cik"] == "320193", "changed CIK is updated"
    assert rows["ZZZ"]["cik"] == "999", "new ticker is inserted"
    assert rows["GONE"]["cik"] == "5", "a ticker missing from the new map must survive"


def test_a_failed_sec_map_leaves_every_row_alone(fk):
    rawstore.write_rows("sec_company_tickers", [{"ticker": "AAPL", "cik": "111", "name": "Apple old"},
                                                {"ticker": "GONE", "cik": "5", "name": "Delisted Co"}])
    fk.results["sec_exchange"] = bad("sec_exchange")
    res = run(fk)
    rows = {r["ticker"]: r["cik"] for r in rawstore.query("SELECT * FROM sec_company_tickers")}
    assert rows == {"AAPL": "111", "GONE": "5"}
    assert res["status"] == "partial" and res["sources"]["sec_exchange"]["status"] == "error"
    assert master_rows()["AAPL"]["cik"] == "111", "the master falls back to the stored SEC map"


def test_a_partial_sec_map_does_not_shrink_the_table(fk):
    rawstore.write_rows("sec_company_tickers", [{"ticker": "AAPL", "cik": "320193", "name": "Apple"},
                                                {"ticker": "PDI", "cik": "1510281", "name": "PDI"},
                                                {"ticker": "GONE", "cik": "5", "name": "Delisted Co"}])
    fk.results["sec_exchange"] = ok("sec_exchange", [{"ticker": "AAPL", "cik": "320193", "name": "Apple",
                                                      "exchange": "Nasdaq"}])
    run(fk)
    assert {r["ticker"] for r in rawstore.query("SELECT ticker FROM sec_company_tickers")} == {"AAPL", "PDI", "GONE"}


# ── deltas ────────────────────────────────────────────────────────────────────────────────────
def test_sec_submissions_delta_is_new_or_older_than_30_days_and_keyed_by_ticker(fk):
    seed_cache("sec_submissions", [
        {"ticker": "AAPL", "cik": "320193", "status": "ok", "forms": ["10-K"], "sic": "3571", "name": "A"}], OLD_10D)
    seed_cache("sec_submissions", [
        {"ticker": "PDI", "cik": "1510281", "status": "ok", "forms": ["N-CSR"], "sic": "", "name": "P"}], OLD_40D)
    run(fk)
    (ciks, tbc), = fk.calls["sec_submissions"]
    assert ciks == [999, 1510281], "AAPL is fresh (10 d); PDI is stale (40 d); ZZZ is new"
    assert tbc == {999: "ZZZ", 1510281: "PDI"}
    keys = {r["ticker"] for r in rawstore.query("SELECT ticker FROM identity_source_cache WHERE source='sec_submissions'")}
    assert keys == {"AAPL", "PDI", "ZZZ"}, "cache rows are keyed by ticker, never by CIK"


def test_a_shared_cik_is_fetched_once_and_cached_for_each_ticker(fk):
    rawstore.write_rows("equity_universe", [{"ticker": "ZZZB", "name": "Zed B", "market_cap": 5e8}])
    fk.results["sec_exchange"] = ok("sec_exchange", fk.results["sec_exchange"].rows + [
        {"ticker": "ZZZB", "cik": "999", "name": "Zed", "exchange": "NYSE"}])
    run(fk)
    (ciks, _), = fk.calls["sec_submissions"]
    assert ciks.count(999) == 1
    keys = {r["ticker"] for r in rawstore.query("SELECT ticker FROM identity_source_cache WHERE source='sec_submissions'")}
    assert {"ZZZ", "ZZZB"} <= keys
    run(fk, now=NOW)
    assert len(fk.calls["sec_submissions"]) == 1, "both tickers are cached, so the second run fetches nothing"


def test_openfigi_delta_is_new_or_older_than_90_days(fk):
    seed_cache("openfigi", [{"ticker": "AAPL", "status": "ok", "security_type": "Common Stock",
                             "security_type2": "Common Stock", "name": "x"}], "2026-09-01T00:00:00Z")
    seed_cache("openfigi", [{"ticker": "PDI", "status": "ok", "security_type": "Closed-End Fund",
                             "security_type2": "Mutual Fund", "name": "x"}], OLD_100D)
    run(fk)
    (tickers,), = fk.calls["openfigi"]
    assert tickers == ["PDI", "ZZZ"]


def test_openfigi_misses_are_asked_again_after_a_week(fk):
    """A ``no_match`` row is retried after FIGI_MISS_DAYS, not FIGI_DAYS: OpenFIGI lists new
    units and warrants late, and a spelling fix must reach old misses."""
    seed_cache("openfigi", [{"ticker": "AAPL", "status": "ok", "security_type": "Common Stock",
                             "security_type2": "Common Stock", "name": "x"}], OLD_10D)
    seed_cache("openfigi", [{"ticker": "PDI", "status": "no_match"}], "2026-09-28T00:00:00Z")
    seed_cache("openfigi", [{"ticker": "ZZZ", "status": "no_match"}], OLD_10D)
    run(fk)
    (tickers,), = fk.calls["openfigi"]
    assert tickers == ["ZZZ"], "a 10-day-old miss is retried; a 2-day-old miss and a hit are not"


def test_second_run_refetches_only_deltas(fk):
    run(fk)
    fk.calls.clear()
    run(fk, now=LATER)
    assert fk.calls["sec_exchange"] and fk.calls["nasdaq"] and fk.calls["uw"]
    assert not fk.calls.get("openfigi") or fk.calls["openfigi"] == [([],)]
    assert not fk.calls.get("sec_submissions") or fk.calls["sec_submissions"][0][0] == []


# ── the S&P 1500 list: a sector source again, behind its diff gate (#272) ─────────────────────
LIST_AAPL = {"ticker": "AAPL", "sector": "Information Technology",
             "sub_industry": "Technology Hardware, Storage & Peripherals", "index": "sp500", "name": "Apple"}


def test_the_sp1500_source_is_back_in_its_old_place():
    assert R.SOURCES == ("sec_exchange", "sec_submissions", "openfigi", "nasdaq", "sp1500", "uw_sector",
                         "yahoo_sector", "fdic", "spdr")
    assert R.GROUPS["sp1500"] == ("sp1500",)
    assert ("sp1500",) in R._SUMMARY_GROUPS and "sp1500" not in R.CHECKS


def test_a_list_name_gets_the_lists_sector_as_the_morningstar_name_and_uws_industry(fk):
    fk.results["sp1500"] = ok("sp1500", [dict(LIST_AAPL, sector="Industrials")])
    res = run(fk)
    m = master_rows()["AAPL"]
    assert (m["sector"], m["sector_source"]) == ("Industrials", "sp1500")
    assert (m["industry"], m["industry_source"]) == ("Consumer Electronics", "provider_uw")
    assert (m["industry_group"], m["sub_industry"]) == (None, None)
    assert master_rows()["PDI"]["sector_source"] == "provider_uw", "a name off the list keeps its UW sector"
    assert res["counts"]["by_sector_source"] == {"sp1500": 1, "provider_uw": 1, "provider_yf": 1}
    assert universe_gics()["AAPL"] == ("Industrials", "Consumer Electronics"), "the apply copies it"


def test_a_list_name_the_list_and_uw_agree_on_is_written_in_morningstar_spelling(fk):
    fk.results["sp1500"] = ok("sp1500", [LIST_AAPL])
    run(fk)
    m = master_rows()["AAPL"]
    assert (m["sector"], m["sector_source"]) == ("Technology", "sp1500")
    assert "Information Technology" not in {r["sector"] for r in master_rows().values()}


def test_the_list_is_fetched_every_run_and_can_be_the_only_source_named(fk):
    run(fk)
    assert fk.calls["sp1500"]
    fk.calls.clear()
    res = run(fk, sources=["sp1500"], now=LATER)
    assert set(fk.calls) == {"sp1500"}
    assert res["sources"]["sp1500"]["status"] == "ok" and res["sources"]["uw_sector"]["status"] == "skipped"


def test_a_rejected_sp1500_snapshot_keeps_the_old_one_and_records_a_conflict(fk):
    old = [{"ticker": t, "sector": "Information Technology", "sub_industry": "Software", "index": "sp500"}
           for t in ("AAPL", "MSFT", "ORCL", "ADBE")]
    seed_cache("sp1500", old, OLD_10D)
    new = [dict(r, sector="Financials") for r in old[:2]] + old[2:]      # 50% changed
    fk.results["sp1500"] = ok("sp1500", new)
    res = run(fk)
    assert res["sources"]["sp1500"]["status"] == "gated"
    assert res["status"] == "partial"
    snap = {r["ticker"]: r["sector"] for r in cache.last_good("sp1500")}
    assert set(snap.values()) == {"Information Technology"}, "the previous snapshot stays in the cache"
    assert (master_rows()["AAPL"]["sector"], master_rows()["AAPL"]["sector_source"]) == ("Technology", "sp1500")
    conf = [c for c in rawstore.query("SELECT * FROM identity_conflicts WHERE field = 'sp1500_snapshot'")]
    assert len(conf) == 1 and conf[0]["kind"] == "source" and "50" in conf[0]["note"]


def test_an_accepted_sp1500_snapshot_replaces_the_old_one(fk):
    old = [{"ticker": t, "sector": "Information Technology", "sub_industry": "Software", "index": "sp500"}
           for t in ("AAPL", "MSFT")]
    seed_cache("sp1500", old, OLD_10D)
    fk.results["sp1500"] = ok("sp1500", [dict(old[0], sector="Information Technology"), old[1],
                                         {"ticker": "NEW", "sector": "Financials", "sub_industry": "Banks",
                                          "index": "sp400"}])
    res = run(fk)
    assert res["sources"]["sp1500"]["status"] == "ok"
    assert "NEW" in {r["ticker"] for r in cache.last_good("sp1500")}
    assert not rawstore.query("SELECT * FROM identity_conflicts WHERE field = 'sp1500_snapshot'")


def test_a_failed_list_fetch_keeps_the_cached_sector_and_makes_the_run_partial(fk):
    seed_cache("sp1500", [dict(LIST_AAPL, sector="Industrials")], OLD_10D)
    fk.results["sp1500"] = bad("sp1500")
    res = run(fk)
    assert res["status"] == "partial" and res["sources"]["sp1500"]["status"] == "error"
    assert (master_rows()["AAPL"]["sector"], master_rows()["AAPL"]["sector_source"]) == ("Industrials", "sp1500")


def test_a_list_that_failed_with_no_cache_does_not_take_a_sector_away(fk, spy):
    fk.results["sp1500"] = bad("sp1500")
    run(fk)
    assert spy["inputs"]["AAPL"]["sp1500"] is None
    assert master_rows()["AAPL"]["sector_source"] == "provider_uw"


def test_a_ticker_that_left_the_list_goes_back_to_its_provider_sector(fk):
    fk.results["sp1500"] = ok("sp1500", [dict(LIST_AAPL, sector="Industrials")])
    run(fk)
    assert master_rows()["AAPL"]["sector_source"] == "sp1500"
    fk.results["sp1500"] = ok("sp1500", [dict(LIST_AAPL, ticker="OTHER")])
    run(fk, now=LATER)
    m = master_rows()["AAPL"]
    assert (m["sector"], m["sector_source"]) == ("Technology", "provider_uw")


def _aapl_has_left_the_list(fk):
    """Run 1: AAPL is on the list (Industrials). Run 2: a later accepted snapshot without it. The cache still holds
    AAPL's row from run 1 (the cache never deletes), stamped with the first snapshot's time."""
    fk.results["sp1500"] = ok("sp1500", [dict(LIST_AAPL, sector="Industrials")], at=NOW)
    run(fk, now=NOW)
    assert master_rows()["AAPL"]["sector_source"] == "sp1500"
    fk.results["sp1500"] = ok("sp1500", [dict(LIST_AAPL, ticker="OTHER")], at=LATER)
    run(fk, now=LATER)
    m = master_rows()["AAPL"]
    assert (m["sector"], m["sector_source"]) == ("Technology", "provider_uw")
    assert "AAPL" in {r["ticker"] for r in cache.last_good("sp1500")}, "the cache keeps the old row"


def test_a_name_that_left_the_list_does_not_get_the_list_sector_back_when_the_next_fetch_fails(fk):
    _aapl_has_left_the_list(fk)
    fk.results["sp1500"] = bad("sp1500", at=LATEST)
    res = run(fk, now=LATEST)
    assert res["sources"]["sp1500"]["status"] == "error"
    m = master_rows()["AAPL"]
    assert (m["sector"], m["sector_source"]) == ("Technology", "provider_uw")


def test_a_name_that_left_the_list_does_not_get_the_list_sector_back_when_the_next_snapshot_is_gated(fk):
    _aapl_has_left_the_list(fk)
    fk.results["sp1500"] = ok("sp1500", [dict(LIST_AAPL, ticker="OTHER", sector="Financials")], at=LATEST)
    res = run(fk, now=LATEST)
    assert res["sources"]["sp1500"]["status"] == "gated"
    m = master_rows()["AAPL"]
    assert (m["sector"], m["sector_source"]) == ("Technology", "provider_uw")


def test_a_name_that_left_the_list_does_not_get_the_list_sector_back_on_a_run_that_skips_the_list(fk):
    _aapl_has_left_the_list(fk)
    fk.calls.clear()
    run(fk, sources=["nasdaq"], now=LATEST)
    assert "sp1500" not in fk.calls
    m = master_rows()["AAPL"]
    assert (m["sector"], m["sector_source"]) == ("Technology", "provider_uw")


def test_the_members_of_the_last_accepted_snapshot_still_read_from_the_cache_when_the_fetch_fails(fk, spy):
    _aapl_has_left_the_list(fk)
    seed_cache("sp1500", [dict(LIST_AAPL, ticker="ZZZ", sector="Utilities")], LATER)       # a second LATER member
    fk.results["sp1500"] = bad("sp1500", at=LATEST)
    run(fk, now=LATEST)
    assert spy["inputs"]["ZZZ"]["sp1500"]["sector"] == "Utilities", "the last snapshot's members are listed"
    assert spy["inputs"]["AAPL"]["sp1500"] is None, "a name outside it has no answer from a failed fetch"
    assert (master_rows()["ZZZ"]["sector"], master_rows()["ZZZ"]["sector_source"]) == ("Utilities", "sp1500")


def test_the_diff_gate_compares_a_new_snapshot_with_the_last_accepted_one_not_with_every_row_ever_cached(fk):
    stay = [{"ticker": f"N{i:02d}", "sector": "Information Technology", "sub_industry": "Software", "index": "sp500"}
            for i in range(40)]
    gone = [{"ticker": t, "sector": "Industrials", "sub_industry": "Airlines", "index": "sp400"} for t in ("EX1", "EX2")]
    seed_cache("sp1500", stay + gone, OLD_100D)
    seed_cache("sp1500", stay, OLD_10D)                      # EX1 and EX2 left the list; their old rows stay cached
    # they return, with another sector: 2 of 42 names would read as changed against every cached row (4.8%)
    fk.results["sp1500"] = ok("sp1500", stay + [dict(r, sector="Financials") for r in gone], at=NOW)
    res = run(fk)
    assert res["sources"]["sp1500"]["status"] == "ok", "against the last snapshot nothing changed"
    assert not rawstore.query("SELECT * FROM identity_conflicts WHERE field = 'sp1500_snapshot'")
    assert {r["ticker"]: r["sector"] for r in cache.last_good("sp1500")}["EX1"] == "Financials"


def test_the_sic_decides_a_name_with_no_list_uw_or_yahoo_sector(fk):
    cl = fk.clients()
    cl["yahoo"] = lambda tickers: ok("yahoo_sector", [])
    res = R.refresh(now=NOW, clients=cl)
    z = master_rows()["ZZZ"]
    assert (z["sector"], z["sector_source"]) == ("Technology", "sic_explicit"), "the fake SIC 3571"
    assert (z["industry"], z["industry_source"]) == ("Unclassified", "none")
    assert res["counts"]["by_sector_source"]["sic_explicit"] == 1 and res["counts"]["unclassified"] == 0
    assert universe_gics()["ZZZ"] == ("Technology", "Unclassified")


# ── the None vs {} input contract ─────────────────────────────────────────────────────────────
@pytest.fixture
def spy(monkeypatch):
    seen = {}
    real = master.build

    def build(inputs, *a, **kw):
        seen["inputs"] = inputs
        return real(inputs, *a, **kw)
    monkeypatch.setattr(master, "build", build)
    return seen


def test_a_healthy_source_without_a_row_gives_empty_dict_not_none(fk, spy):
    run(fk)
    zzz = spy["inputs"]["ZZZ"]
    assert zzz["nasdaq"] == {}, "Nasdaq answered and does not list ZZZ: {} (else the old value freezes)"
    assert zzz["sp1500"] == {} and zzz["uw"] == {}
    assert spy["inputs"]["AAPL"]["nasdaq"]["is_etf"] is False


def test_a_failed_source_with_no_cache_gives_none(fk, spy):
    fk.results["nasdaq"] = bad("nasdaq")
    run(fk)
    assert spy["inputs"]["AAPL"]["nasdaq"] is None and spy["inputs"]["ZZZ"]["nasdaq"] is None


def test_fdic_is_empty_dict_for_non_candidates_and_a_match_dict_for_candidates(fk, spy):
    # ZZZ: SEC says no periodic filings + OpenFIGI common -> a candidate; AAPL is a normal filer.
    fk.results["sec_submissions"] = ok("sec_submissions", [
        {"ticker": "AAPL", "cik": "320193", "status": "ok", "forms": ["10-K"], "sic": "3571", "name": "A"},
        {"ticker": "PDI", "cik": "1510281", "status": "ok", "forms": ["N-CSR"], "sic": "", "name": "P"},
        {"ticker": "ZZZ", "cik": "999", "status": "not_found"}])
    fk.results["openfigi"] = ok("openfigi", [
        {"ticker": t, "status": "ok", "security_type": "Common Stock", "security_type2": "Common Stock", "name": t}
        for t in ("AAPL", "ZZZ")] + [{"ticker": "PDI", "status": "ok", "security_type": "Closed-End Fund",
                                      "security_type2": "Mutual Fund", "name": "P"}])
    run(fk)
    (names,), = fk.calls["fdic"]
    assert set(names) == {"ZZZ"}
    assert spy["inputs"]["AAPL"]["fdic"] == {}, "healthy FDIC, not a candidate -> {}"
    assert spy["inputs"]["ZZZ"]["fdic"] == {"match": None}


def test_a_healthy_source_not_listing_a_ticker_unfreezes_the_previous_value(fk):
    """Run 1 types XYZ as an ETP from the Nasdaq flag; run 2 Nasdaq answers without XYZ -> no longer frozen."""
    rawstore.write_rows("equity_universe", [{"ticker": "XYZ", "name": "Xyz Holdings", "market_cap": 1e9}])
    fk.results["nasdaq"] = ok("nasdaq", fk.results["nasdaq"].rows + [
        {"ticker": "XYZ", "security_name": "Xyz ETF", "is_etf": True, "exchange": "P"}])
    run(fk)
    assert master_rows()["XYZ"]["security_type"] == "etp"
    fk.results["nasdaq"] = ok("nasdaq", fk.results["nasdaq"].rows[:2])
    fk.calls.clear()
    run(fk, now=LATER)
    assert master_rows()["XYZ"]["security_type"] != "etp"


# ── failure handling ──────────────────────────────────────────────────────────────────────────
def test_one_failing_source_makes_the_run_partial_and_the_others_still_process(fk):
    fk.results["nasdaq"] = bad("nasdaq")
    res = run(fk)
    assert res["status"] == "partial"
    assert res["sources"]["nasdaq"]["status"] == "error" and res["sources"]["uw_sector"]["status"] == "ok"
    assert master_rows()["AAPL"]["sector_source"] == "provider_uw"
    assert rawstore.query("SELECT status FROM identity_runs")[0]["status"] == "partial"


def test_a_source_that_raises_is_an_error_source_not_a_crash(fk):
    def boom():
        raise RuntimeError("kaboom")
    cl = fk.clients()
    cl["nasdaq"] = boom
    res = R.refresh(now=NOW, clients=cl)
    assert res["status"] == "partial" and res["sources"]["nasdaq"]["status"] == "error"
    assert "kaboom" in res["sources"]["nasdaq"]["error"]


def test_the_master_keeps_previous_values_when_their_source_failed_with_no_cache(fk):
    rawstore.write_rows("security_master", [{
        "ticker": "ZZZ", "security_type": "common", "type_source": "openfigi", "type_asof": OLD_40D,
        "sector": "Healthcare", "sector_source": "provider_uw", "industry": "Biotechnology",
        "industry_source": "provider_uw", "gics_asof": OLD_40D, "updated_at": OLD_40D}])
    fk.results["uw"] = SourceResult("uw_sector", "skipped", [], "no key", NOW)
    res = run(fk)
    row = master_rows()["ZZZ"]
    assert (row["sector"], row["sector_source"], row["gics_asof"]) == ("Healthcare", "provider_uw", OLD_40D)
    assert (row["industry"], row["industry_source"]) == ("Biotechnology", "provider_uw")
    assert res["status"] == "partial" and res["sources"]["uw_sector"]["status"] == "skipped"


def test_an_exception_anywhere_is_a_recorded_error_run_and_never_raises(fk, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("build exploded")
    monkeypatch.setattr(master, "build", boom)
    res = run(fk)
    assert res["status"] == "error" and "build exploded" in res["error"]
    runs = rawstore.query("SELECT * FROM identity_runs")
    assert len(runs) == 1 and runs[0]["status"] == "error"


def test_an_empty_universe_is_an_error_run(fk):
    rawstore.execute("DELETE FROM equity_universe")
    res = run(fk)
    assert res["status"] == "error" and "universe" in res["error"].lower()


# ── Yahoo and conflicts ───────────────────────────────────────────────────────────────────────
def test_yahoo_is_asked_only_for_tickers_without_a_uw_sector_and_respects_the_cap(fk):
    rawstore.write_rows("equity_universe", [{"ticker": "NOUW2", "name": "No Uw Two", "market_cap": 5e8}])
    run(fk, yahoo_limit=1)
    assert fk.calls["yahoo"] == [(["ZZZ"],)], "AAPL and PDI have a UW sector; the larger cap goes first"
    run(fk, yahoo_limit=1)
    assert fk.calls["yahoo"][-1] == (["NOUW2"],), "ZZZ is cached now, so the cap reaches the next ticker"


def test_yahoo_is_asked_when_the_uw_sector_is_not_one_of_the_eleven_names(fk):
    """A UW sector the master cannot use is no sector: Yahoo is consulted for it, as for a blank one."""
    fk.results["uw"] = ok("uw_sector", [
        {"ticker": "AAPL", "sector": "Technology", "industry": "Consumer Electronics", "security_type": "Common Stock"},
        {"ticker": "PDI", "sector": "Financials", "industry": "Asset Management", "security_type": "Common Stock"},
        {"ticker": "ZZZ", "sector": "", "industry": None, "security_type": "Common Stock"}])
    run(fk)
    (asked,), = fk.calls["yahoo"]
    assert sorted(asked) == ["PDI", "ZZZ"], "PDI's 'Financials' is not a Morningstar name; AAPL's sector is"
    assert master_rows()["PDI"]["sector"] == "Healthcare" and master_rows()["PDI"]["sector_source"] == "provider_yf"


def test_yahoo_is_capped_per_run_by_default(fk, monkeypatch):
    # A job run passes no yahoo_limit: it must still never ask Yahoo for the whole
    # universe in one go (docs/data_health.md); the default per-run cap applies.
    monkeypatch.setattr(R, "YAHOO_PER_RUN", 1)
    rawstore.write_rows("equity_universe", [{"ticker": "NOUW2", "name": "No Uw Two", "market_cap": 5e8}])
    run(fk)
    assert fk.calls["yahoo"] == [(["ZZZ"],)], "default cap of 1 -> only the largest gap"


def test_yahoo_is_skipped_when_uw_gave_nothing_at_all(fk):
    fk.results["uw"] = SourceResult("uw_sector", "skipped", [], "no key", NOW)
    run(fk)
    assert "yahoo" not in fk.calls, "without any UW answer every ticker would look sector-less"


def test_conflicts_keep_first_seen_and_move_last_seen(fk):
    fk.results["uw"] = ok("uw_sector", fk.results["uw"].rows + [
        {"ticker": "ZZZ", "sector": "Technology", "industry": "Software", "security_type": "Common Stock",
         "market_cap": 5.0e10}])                                  # 50x the stored 1e9 -> a cap conflict
    run(fk)
    c1 = rawstore.query("SELECT * FROM identity_conflicts WHERE ticker='ZZZ' AND kind='cap'")
    assert len(c1) == 1 and c1[0]["first_seen"] == NOW and c1[0]["last_seen"] == NOW
    run(fk, now=LATER)
    c2 = rawstore.query("SELECT * FROM identity_conflicts WHERE ticker='ZZZ' AND kind='cap'")
    assert len(c2) == 1 and c2[0]["first_seen"] == NOW and c2[0]["last_seen"] == LATER


# ── R3 (#86): the successful master is applied to equity_universe ─────────────────────────────
def test_a_successful_run_applies_the_master_to_the_universe_and_records_the_counts(fk):
    res = run(fk)
    assert res["status"] == "ok"
    m = master_rows()
    assert universe_gics() == {t: (r["sector"], r["industry"]) for t, r in m.items()}
    assert universe_gics()["AAPL"] == ("Technology", "Consumer Electronics")
    assert universe_gics()["ZZZ"] == ("Healthcare", "Biotechnology")      # no UW sector: the Yahoo fallback
    counts = res["counts"]
    assert (counts["applied"], counts["unchanged"], counts["not_in_master"]) == (3, 0, 0)
    stored = rawstore.query("SELECT counts FROM identity_runs")[0]["counts"]
    stored = json.loads(stored) if isinstance(stored, str) else stored
    assert stored["applied"] == 3 and stored["not_in_master"] == 0


def test_a_successful_run_copies_the_uw_earnings_date_and_leaves_the_gics_only_columns_null(fk):
    rawstore.update_columns("equity_universe", "ticker", [
        {"ticker": t, "industry_group": "Old Group", "sub_industry": "Old Sub"} for t in ("AAPL", "PDI", "ZZZ")],
        ["industry_group", "sub_industry"])
    run(fk)
    rows = {r["ticker"]: r for r in rawstore.query(
        "SELECT ticker, industry_group, sub_industry, next_earnings_date FROM equity_universe")}
    assert rows["AAPL"]["next_earnings_date"] == "2026-10-30"
    assert rows["PDI"]["next_earnings_date"] is None and rows["ZZZ"]["next_earnings_date"] is None
    assert all(r["industry_group"] is None and r["sub_industry"] is None for r in rows.values())


def test_a_second_run_leaves_the_universe_as_it_was(fk):
    run(fk)
    first = universe_gics()
    res = run(fk, now=LATER)
    assert universe_gics() == first
    assert (res["counts"]["applied"], res["counts"]["unchanged"]) == (0, 3)


def test_a_partial_run_applies_too(fk):
    fk.results["nasdaq"] = bad("nasdaq")
    res = run(fk)
    assert res["status"] == "partial"
    assert res["counts"]["applied"] == 3
    assert universe_gics()["AAPL"][0] == "Technology"


def test_an_errored_run_does_not_apply(fk, monkeypatch):
    called = []
    monkeypatch.setattr(universe_apply, "apply_to_universe", lambda: called.append(1) or {})
    rawstore.clear("equity_universe")                      # "equity_universe is empty": the run errors
    res = run(fk)
    assert res["status"] == "error" and not called
    assert "applied" not in res["counts"]


def test_an_error_while_building_the_master_does_not_apply(fk, monkeypatch):
    called = []
    monkeypatch.setattr(universe_apply, "apply_to_universe", lambda: called.append(1) or {})

    def boom(*a, **k):
        raise RuntimeError("build blew up")

    monkeypatch.setattr(master, "build", boom)
    before = universe_gics()
    res = run(fk)
    assert res["status"] == "error" and "build blew up" in res["error"]
    assert not called and universe_gics() == before


def test_a_failing_apply_is_a_recorded_error_not_an_exception(fk, monkeypatch):
    def boom():
        raise RuntimeError("apply blew up")

    monkeypatch.setattr(universe_apply, "apply_to_universe", boom)
    res = run(fk)
    assert res["status"] == "error" and "apply blew up" in res["error"]
    assert len(master_rows()) == 3, "the master was built and stays"
    assert rawstore.query("SELECT status FROM identity_runs")[0]["status"] == "error"


def test_a_limited_run_never_blanks_the_tickers_it_left_out(fk):
    rawstore.update_columns("equity_universe", "ticker", [
        {"ticker": "ZZZ", "sector": "Energy", "industry": "Oil & Gas Drilling"}], ["sector", "industry"])
    res = run(fk, limit=2)
    assert set(master_rows()) == {"AAPL", "PDI"}
    assert universe_gics()["ZZZ"] == ("Energy", "Oil & Gas Drilling")      # vendor values kept
    assert (res["counts"]["applied"], res["counts"]["not_in_master"]) == (2, 1)


# ── SPDR sector check (issue #89): a check, never a value ─────────────────────────────────────
NO_SPDR = ["sec", "openfigi", "nasdaq", "sp1500", "uw", "yahoo", "fdic"]


def spdr_conflicts():
    return rawstore.query("SELECT * FROM identity_conflicts WHERE kind = 'spdr' ORDER BY ticker")


def test_a_spdr_sector_mismatch_writes_a_conflict_and_the_counts(fk):
    fk.results["spdr"] = ok("spdr", [{"ticker": "AAPL", "name": "Apple", "fund": "XLC",
                                      "sector": "Communication Services", "weight": 1.0}])
    res = run(fk)
    c, = spdr_conflicts()
    assert (c["ticker"], c["kind"], c["field"]) == ("AAPL", "spdr", "sector")
    assert c["side_a"] == "Technology" and c["side_b"] == "Communication Services"
    assert c["note"] == "SPDR XLC holdings" and c["first_seen"] == NOW and c["last_seen"] == NOW
    assert res["counts"]["spdr_checked"] == 1 and res["counts"]["spdr_agree"] == 0
    assert res["counts"]["conflicts_by_kind"]["spdr"] == 1
    assert master_rows()["AAPL"]["sector"] == "Technology", "SPDR never sets a value"
    assert res["status"] == "ok", "a SPDR mismatch is a finding, not a failure"


def test_spdr_agreement_counts_checked_and_agree_without_a_conflict(fk):
    res = run(fk)
    assert res["counts"]["spdr_checked"] == 1 and res["counts"]["spdr_agree"] == 1
    assert not spdr_conflicts()
    assert res["sources"]["spdr"]["status"] == "ok" and res["sources"]["spdr"]["rows"] == 1
    runs = rawstore.query("SELECT counts FROM identity_runs")
    counts = json.loads(runs[0]["counts"]) if isinstance(runs[0]["counts"], str) else runs[0]["counts"]
    assert counts["spdr_checked"] == 1 and counts["spdr_agree"] == 1


def test_only_sp500_members_held_by_a_fund_and_present_in_the_master_are_compared(fk):
    """Members come from ``index_constituents`` (UW spelling, BRK.B); the others in the holdings are not compared."""
    rawstore.write_rows("equity_universe", [{"ticker": "BRK-B", "name": "Berkshire Hathaway", "market_cap": 1e12},
                                            {"ticker": "XYZ", "name": "Xyz", "market_cap": 1e9}])
    index_registry.save_index("sp500", {"AAPL", "BRK.B", "NOTLISTED"})
    fk.results["uw"] = ok("uw_sector", fk.results["uw"].rows + [
        {"ticker": "BRK-B", "sector": "Financial Services", "industry": "Insurance - Diversified",
         "security_type": "Common Stock", "market_cap": 1e12},
        {"ticker": "XYZ", "sector": "Financial Services", "industry": "Banks - Regional",
         "security_type": "Common Stock", "market_cap": 1e9}])
    fk.results["spdr"] = ok("spdr", fk.results["spdr"].rows + [
        {"ticker": "BRK-B", "name": "Berkshire", "fund": "XLF", "sector": "Financial Services", "weight": 1.0},
        {"ticker": "XYZ", "name": "Xyz", "fund": "XLRE", "sector": "Real Estate", "weight": 0.1},          # not S&P 500
        {"ticker": "NOTLISTED", "name": "Other", "fund": "XLU", "sector": "Utilities", "weight": 0.1}])   # not in master
    res = run(fk)
    assert not spdr_conflicts(), "XYZ is not an S&P 500 member and NOTLISTED is in no universe: neither is compared"
    assert res["counts"]["spdr_checked"] == 2 and res["counts"]["spdr_agree"] == 2, "AAPL and BRK-B (BRK.B normalised)"


def test_the_check_compares_the_master_sector_this_run_built_not_the_stored_one(fk):
    rawstore.write_rows("security_master", [{
        "ticker": "AAPL", "security_type": "common", "type_source": "openfigi", "type_asof": OLD_40D,
        "sector": "Energy", "sector_source": "provider_uw", "industry": "Oil", "industry_source": "provider_uw",
        "gics_asof": OLD_40D, "updated_at": OLD_40D}])
    res = run(fk)
    assert master_rows()["AAPL"]["sector"] == "Technology"
    assert not spdr_conflicts() and res["counts"]["spdr_agree"] == 1


def test_an_unclassified_sp500_member_the_fund_holds_is_flagged(fk):
    fk.results["uw"] = ok("uw_sector", [r for r in fk.results["uw"].rows if r["ticker"] != "AAPL"])
    fk.results["sec_submissions"] = ok("sec_submissions", [        # no SIC code either: nothing can classify AAPL
        {"ticker": "AAPL", "cik": "320193", "status": "ok", "forms": ["10-K"], "sic": "", "name": "A"},
        {"ticker": "PDI", "cik": "1510281", "status": "ok", "forms": ["N-CSR"], "sic": "", "name": "P"},
        {"ticker": "ZZZ", "cik": "999", "status": "ok", "forms": ["10-K"], "sic": "", "name": "Z"}])
    cl = fk.clients()
    cl["yahoo"] = lambda tickers: ok("yahoo_sector", [])           # Yahoo knows nothing either
    res = R.refresh(now=NOW, clients=cl)
    c, = spdr_conflicts()
    assert (c["ticker"], c["side_a"], c["side_b"]) == ("AAPL", "Unclassified", "Technology")
    assert res["counts"]["spdr_checked"] == 1 and res["counts"]["spdr_agree"] == 0


def test_an_sp500_member_only_the_sic_table_classifies_is_compared_like_any_other(fk):
    fk.results["uw"] = ok("uw_sector", [r for r in fk.results["uw"].rows if r["ticker"] != "AAPL"])
    cl = fk.clients()
    cl["yahoo"] = lambda tickers: ok("yahoo_sector", [])           # the fake SEC rows carry SIC 3571 (Technology)
    res = R.refresh(now=NOW, clients=cl)
    assert master_rows()["AAPL"]["sector_source"] == "sic_explicit"
    assert not spdr_conflicts() and res["counts"]["spdr_agree"] == 1


def test_without_an_sp500_list_nothing_is_checked_and_the_run_stays_ok(fk):
    rawstore.execute("DELETE FROM index_constituents")
    res = run(fk)
    assert res["status"] == "ok" and not spdr_conflicts()
    assert res["counts"]["spdr_checked"] == 0 and res["counts"]["spdr_agree"] == 0


@pytest.mark.parametrize("how", ["error result", "raising client"])
def test_a_failed_spdr_writes_nothing_and_leaves_the_run_ok(fk, how):
    run(fk, sources=NO_SPDR)
    before = master_rows()
    rawstore.execute("DELETE FROM identity_conflicts")
    if how == "error result":
        fk.results["spdr"] = bad("spdr")
        res = run(fk)
    else:
        def boom():
            raise RuntimeError("kaboom")
        cl = fk.clients()
        cl["spdr"] = boom
        res = R.refresh(now=NOW, clients=cl)
    assert res["sources"]["spdr"]["status"] == "error" and res["sources"]["spdr"]["error"]
    if how == "raising client":
        assert "kaboom" in res["sources"]["spdr"]["error"]
    assert res["status"] == "ok", "SPDR is a check: its failure must not make the run partial"
    assert "sources ok 7/7" in R.summary(res)
    assert not spdr_conflicts()
    assert "spdr_checked" not in res["counts"] and "spdr_agree" not in res["counts"]
    assert master_rows() == before, "a failed check must not blank or change anything in the master"
    assert rawstore.query("SELECT status FROM identity_runs ORDER BY started_at DESC")[0]["status"] == "ok"


def test_a_spdr_row_without_a_fund_cannot_take_the_run_down(fk):
    fk.results["spdr"] = ok("spdr", [{"ticker": "AAPL", "sector": "Communication Services"}])
    res = run(fk)
    assert res["status"] == "ok", res.get("error")
    c, = spdr_conflicts()
    assert c["side_b"] == "Communication Services" and c["note"] == "SPDR holdings"


def test_spdr_rows_are_never_cached(fk):
    run(fk)
    assert not rawstore.query("SELECT * FROM identity_source_cache WHERE source = 'spdr'")


def test_the_master_never_reads_spdr(fk, spy):
    fk.results["spdr"] = ok("spdr", [{"ticker": "AAPL", "name": "Apple", "fund": "XLC",
                                      "sector": "Communication Services", "weight": 1.0}])
    run(fk)
    assert all("spdr" not in i for i in spy["inputs"].values())


def test_spdr_is_not_in_the_sources_ok_summary_and_can_be_selected_alone(fk):
    res = run(fk)
    assert "sources ok 7/7" in R.summary(res)
    fk.calls.clear()
    res = run(fk, sources=["spdr"], now=LATER)
    assert set(fk.calls) == {"spdr"} and res["sources"]["spdr"]["selected"]
    assert res["status"] == "ok"


# ── SEC submissions stagger (issue #89, amends K5) ────────────────────────────────────────────
DAY0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
STAGGER_TICKERS = [f"T{i}" for i in range(8)]


def day(n: int) -> str:
    return (DAY0 + timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


def stagger_world(cached_at: dict | None = None, extra=()):
    """8 tickers with distinct CIKs (1001..1008), each with a cached sec_submissions row from day 0.

    Only the SEC map in ``sec_company_tickers`` is seeded and runs use ``sources=["sec_submissions"]``,
    so nothing else is fetched. ``cached_at`` overrides the fetch time of single tickers.
    """
    rawstore.execute("DELETE FROM equity_universe")
    names = list(STAGGER_TICKERS) + list(extra)
    rawstore.write_rows("equity_universe", [{"ticker": t, "name": f"{t} Corp", "market_cap": 1e9} for t in names])
    rawstore.write_rows("sec_company_tickers", [
        {"ticker": t, "cik": str(1001 + i), "name": t} for i, t in enumerate(names)])
    for i, t in enumerate(STAGGER_TICKERS):
        seed_cache("sec_submissions", [{"ticker": t, "cik": str(1001 + i), "status": "ok", "forms": ["10-K"],
                                        "sic": "3571", "name": t}], (cached_at or {}).get(t, day(0)))


def sec_run(fk, n):
    fk.clock = day(n)
    return R.refresh(now=day(n), clients=fk.clients(), sources=["sec_submissions"])


def cached_ages(n):
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    now = datetime.strptime(day(n), fmt).replace(tzinfo=timezone.utc)
    return [(now - datetime.strptime(r["fetched_at"], fmt).replace(tzinfo=timezone.utc)).days
            for r in rawstore.query("SELECT fetched_at FROM identity_source_cache WHERE source = 'sec_submissions'")]


def test_stagger_constants_are_a_quarter_of_the_cache_and_a_week_of_age():
    assert R.SEC_STAGGER == 0.25 and R.SEC_STAGGER_MIN_DAYS == 7


def test_weekly_runs_refresh_a_quarter_of_the_cache_each_and_cover_it_without_repeats(fk):
    stagger_world()
    seen: list[int] = []
    for n in (7, 14, 21, 28):
        before = len(fk.calls.get("sec_submissions", []))
        res = sec_run(fk, n)
        assert len(fk.calls["sec_submissions"]) == before + 1, f"day {n}: one sec_submissions request"
        ciks, _ = fk.calls["sec_submissions"][-1]
        assert len(ciks) == math.ceil(8 * 0.25) == 2, f"day {n}: exactly a quarter of the cache, got {ciks}"
        assert res["sources"]["sec_submissions"]["fetched"] == 2
        assert res["sources"]["sec_submissions"]["staggered"] == 2
        seen += ciks
    assert sorted(seen) == [1001 + i for i in range(8)], "four weekly runs cover all 8 CIKs, no repeats"


def test_daily_runs_in_the_first_week_request_no_sec_submissions_at_all(fk):
    stagger_world()
    for n in range(1, 7):
        res = sec_run(fk, n)
        assert res["sources"]["sec_submissions"]["fetched"] == 0
    assert "sec_submissions" not in fk.calls, "a row younger than 7 days is never staggered"


def test_over_twelve_weekly_runs_no_cached_row_ever_reaches_the_30_day_window(fk):
    """The point of the stagger: the whole universe was fetched one day and must not expire together."""
    stagger_world()
    for n in range(7, 7 * 13, 7):
        assert max(cached_ages(n)) < R.SEC_DAYS, f"day {n}: a cached row is already past the window"
        sec_run(fk, n)
        assert max(cached_ages(n)) < R.SEC_DAYS
    assert len(fk.calls["sec_submissions"]) == 12, "one two-CIK request per weekly run"
    assert all(len(c[0]) == 2 for c in fk.calls["sec_submissions"])


def test_stale_rows_count_against_the_quota_so_the_union_adds_at_most_quota(fk):
    """Literal reading: the oldest quarter may contain stale rows, so the union never adds more than quota."""
    stagger_world({"T0": day(-40)})
    res = sec_run(fk, 10)
    (ciks, _), = fk.calls["sec_submissions"]
    assert ciks == [1001, 1002], "T0 is stale (50 days) and the oldest; the stagger adds just the next oldest"
    assert res["sources"]["sec_submissions"]["staggered"] == 1, "only T1 is staggered beyond the stale set"


def test_a_new_ticker_is_fetched_and_does_not_count_toward_the_quota(fk):
    stagger_world(extra=["NEWCO"])
    res = sec_run(fk, 7)
    (ciks, _), = fk.calls["sec_submissions"]
    assert ciks == [1001, 1002, 1009], "the two oldest cached rows plus the never-cached NEWCO"
    assert res["sources"]["sec_submissions"]["fetched"] == 3


def test_the_stagger_picks_the_oldest_first_and_breaks_ties_by_ticker(fk):
    stagger_world({"T5": day(-3), "T6": day(-3), "T2": day(1)})
    sec_run(fk, 9)
    (ciks, _), = fk.calls["sec_submissions"]
    assert ciks == [1006, 1007], "T5 and T6 are the oldest (equal age: ticker order)"


def test_a_spdr_only_run_says_checks_only_not_sources_ok_0_of_0(fk):
    """#142c: a checks-only run has no source group to count."""
    res = run(fk, sources=["spdr"])
    line = R.summary(res)
    assert "sources ok 0/0" not in line
    assert line.endswith("checks only")
    assert "sources ok 7/7" in R.summary(run(fk, now=LATER)), "a full run still counts its source groups"
