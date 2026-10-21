"""Identity master (issue #85), end to end: ``POST /run/identity`` against the mock providers.

Every identity source is pointed at the mock provider server through its ``RC_*_BASE`` variable (set in
``conftest.e2e_env``); yfinance goes through the existing fake. The temp store is seeded with a small
``equity_universe`` and the job must:

* fill ``security_master`` (types + sectors with their sources) and write an ``identity_runs`` row;
* decide the sector in the order manual, S&P 1500 list (its own names only), UW, Yahoo, SIC, type rule (#272), keep the
  11 Morningstar names whichever step decides, and write the raw SIC columns from the SEC submissions cache;
* apply the master's sector and industry (UW's own industry text, #265) and UW's next earnings date to
  ``equity_universe`` (issue #86) and change **nothing else** there (a hash of every other column before and after);
* fetch only deltas on a second run, and let a healthy source that stopped listing a ticker un-freeze it;
* never delete SEC ticker-map rows when the map fetch fails;
* leave a Settings Refresh at zero provider calls, render the Identity health card and carry the
  Fetch identity button (R6, #89);
* run the SPDR sector check (a check, never a value; the S&P 500 members come from ``index_constituents``):
  a mismatch writes a ``spdr`` conflict, a failed fetch keeps the run ``ok`` and shows on the card.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from .harness.mock_provider_server import route_spdr

_UNIVERSE = [("AAPL", "Apple Inc.", 3.0e12), ("PDI", "PIMCO Dynamic Income Fund", 2.0e9),
             ("ARCC", "Ares Capital Corporation", 1.2e10), ("OZK", "Bank OZK", 6.0e9),
             ("ASML", "ASML Holding N.V.", 3.0e11), ("LAZR", "Luminar Technologies, Inc.", 3.0e8),
             ("ABCD", "Alpha Acquisition Corp", 2.0e8), ("FLAGX", "Flag Holdings", 1.0e8)]
_CIK = {"AAPL": 320193, "PDI": 1510281, "ARCC": 1287750, "ASML": 937966, "LAZR": 1758057, "ABCD": 1852207}
_FORMS = {"AAPL": ["10-K", "10-Q"], "PDI": ["N-CSR", "NPORT-P"], "ARCC": ["10-K", "N-2", "N-54A"],
          "ASML": ["20-F", "6-K"], "LAZR": ["10-K", "10-Q"], "ABCD": ["10-K", "S-1"]}
_SIC = {"ABCD": "6770", "PDI": "", "ARCC": "6726"}
_FIGI = {"AAPL": ("Common Stock", "Common Stock"), "PDI": ("Closed-End Fund", "Mutual Fund"),
         "ARCC": ("Common Stock", "Common Stock"), "OZK": ("Common Stock", "Common Stock"),
         "ASML": ("ADR", "Depositary Receipt"), "LAZR": ("Common Stock", "Common Stock"),
         "ABCD": ("Common Stock", "Common Stock")}
_UW = [("AAPL", "Technology", "Consumer Electronics", "Common Stock", 3.0e12, "2026-10-30"),
       ("PDI", "Financial Services", "Asset Management", "Common Stock", 2.0e9, None),
       ("ARCC", "Financial Services", "Asset Management", "Common Stock", 1.2e10, "2026-10-28"),
       ("OZK", "Financial Services", "Banks - Regional", "Common Stock", 6.0e9, None),
       ("ASML", "Technology", "Semiconductor Equipment & Materials", "ADR", 3.0e11, None)]


def _wiki(rows):
    body = "".join(f"<tr><td>{t}</td><td>{t} Co</td><td>{s}</td><td>{sub}</td></tr>" for t, s, sub in rows)
    return ("<html><body><table><tr><th>Symbol</th><th>Security</th><th>GICS Sector</th>"
            f"<th>GICS Sub-Industry</th></tr>{body}</table></body></html>").encode()


def _nasdaq(flagx_is_etf: bool) -> bytes:
    head = "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
    rows = [("AAPL", "Apple Inc. - Common Stock", "N"), ("LAZR", "Luminar Technologies - Class A", "N"),
            ("FLAGX", "Flag Holdings ETF", "Y")] if flagx_is_etf else \
        [("AAPL", "Apple Inc. - Common Stock", "N"), ("LAZR", "Luminar Technologies - Class A", "N")]
    return (head + "".join(f"{t}|{n}|Q|N|N|100|{etf}|N\n" for t, n, etf in rows)
            + "File Creation Time: 0930202615:41||||||\n").encode()


_OTHER = ("ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
          "PDI|PIMCO Dynamic Income Fund Common Stock|N|PDI|N|100|N|PDI\n"
          "File Creation Time: 0930202615:41||||||\n").encode()


def _submission(t: str) -> dict:
    forms = _FORMS[t]
    return {"cik": str(_CIK[t]), "name": t + " REGISTRANT", "sic": _SIC.get(t, "3571"), "sicDescription": "x",
            "entityType": "operating", "tickers": [t], "exchanges": ["Nasdaq"],
            "filings": {"recent": {"form": forms, "items": [""] * len(forms)}, "files": []}}


def _figi(path, body):
    out = []
    for job in json.loads(body):
        t = job["idValue"].replace("/", "-")
        if t in _FIGI:
            st, st2 = _FIGI[t]
            out.append({"data": [{"figi": "BBG" + t, "name": t + " INC", "ticker": job["idValue"],
                                  "exchCode": "US", "securityType": st, "securityType2": st2,
                                  "marketSector": "Equity"}]})
        else:
            out.append({"warning": "No identifier found."})
    return out


def install(mock, flagx_is_etf: bool = True) -> None:
    """Route every identity source on the mock provider server (OZK is on the S&P 400 list as an Industrial: the one
    name where the list and UW disagree)."""
    mock.route("/files/company_tickers_exchange.json", payload={
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [[c, t + " Inc", t, "Nasdaq"] for t, c in _CIK.items()]})
    for t, c in _CIK.items():
        mock.route(f"/submissions/CIK{c:010d}.json", payload=_submission(t))
    mock.route("/v3/mapping", payload=_figi, with_body=True)
    mock.route("/dynamic/SymDir/nasdaqlisted.txt", raw=_nasdaq(flagx_is_etf), content_type="text/plain")
    mock.route("/dynamic/SymDir/otherlisted.txt", raw=_OTHER, content_type="text/plain")
    mock.route("/wiki/List_of_S%26P_500_companies", raw=_wiki([
        ("AAPL", "Information Technology", "Technology Hardware, Storage & Peripherals")]), content_type="text/html")
    mock.route("/wiki/List_of_S%26P_400_companies", raw=_wiki([("OZK", "Industrials", "Diversified Support Services")]),
               content_type="text/html")
    mock.route("/wiki/List_of_S%26P_600_companies", raw=_wiki([("XYZY", "Financials", "Regional Banks")]),
               content_type="text/html")
    mock.route("/api/screener/stocks", payload={"data": [
        {"ticker": t, "sector": s, "industry_type": i, "issue_type": it, "marketcap": str(int(cap)),
         "next_earnings_date": date}
        for t, s, i, it, cap, date in _UW]})
    mock.route("/banks/institutions", payload={"data": [{"data": {
        "NAME": "Bank OZK", "CERT": 110, "NAMEHCR": "", "CITY": "Little Rock", "STALP": "AR", "ASSET": 41703134}}]})
    route_spdr(mock, {"xlk": [("AAPL", "Apple Inc.")]})          # agrees with the list and UW for AAPL (Technology)


@pytest.fixture
def seeded(dashboard, provider_routes, monkeypatch):
    """The temp store with a small universe; the mock providers routed for the identity sources."""
    import RevenueChaser.core.rawstore as rawstore

    monkeypatch.setattr(rawstore, "DB_PATH", dashboard.store_dir / "raw.duckdb")
    rawstore.write_rows("equity_universe", [{"ticker": t, "name": n, "market_cap": c} for t, n, c in _UNIVERSE])
    rawstore.write_rows("index_constituents", [{"slug": "sp500", "ticker": "AAPL"}])      # the SPDR check's members
    install(provider_routes)
    return dashboard


def _q(sql: str) -> list[dict]:
    import RevenueChaser.core.rawstore as rawstore
    return rawstore.query(sql)


def _universe_hash() -> str:
    """Every ``equity_universe`` column except the ones the identity job writes (sector, industry, the two NULLed
    GICS-only columns and the earnings date)."""
    rows = _q("SELECT ticker, name, market_cap, price, quote FROM equity_universe ORDER BY ticker")
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def _universe_gics() -> dict[str, tuple]:
    return {r["ticker"]: (r["sector"], r["industry"]) for r in _q("SELECT ticker, sector, industry FROM equity_universe")}


def _master() -> dict[str, dict]:
    return {r["ticker"]: r for r in _q("SELECT * FROM security_master")}


def _json(v):
    return json.loads(v) if isinstance(v, str) else v


class TestIdentityRun:
    def test_fills_the_master_and_applies_its_sector_and_industry_to_the_universe(self, seeded):
        before = _universe_hash()
        result = seeded.run_job("/run/identity")
        assert result.ok, f"identity failed: {result}\n{seeded.logs()}"
        assert "8 tickers" in result.message and "sources ok 7/7" in result.message, result.message

        m = _master()
        assert {t: m[t]["security_type"] for t in m} == {
            "AAPL": "common", "PDI": "cef", "ARCC": "bdc", "OZK": "bank_nonsec", "ASML": "adr", "LAZR": "common",
            "ABCD": "spac", "FLAGX": "etp"}
        # the list says "Information Technology": written as the Morningstar name, source sp1500
        assert (m["AAPL"]["sector"], m["AAPL"]["sector_source"]) == ("Technology", "sp1500")
        assert (m["AAPL"]["industry"], m["AAPL"]["industry_source"]) == ("Consumer Electronics", "provider_uw")
        assert (m["ASML"]["sector"], m["ASML"]["sector_source"]) == ("Technology", "provider_uw")
        assert m["AAPL"]["cik"] == "320193"
        assert (m["PDI"]["sector"], m["PDI"]["industry"]) == ("Financial Services", "Asset Management")
        assert m["PDI"]["sector_source"] == "provider_uw", "a fund UW knows: UW decides, the type rule stays below"
        assert m["AAPL"]["industry_group"] is None and m["AAPL"]["sub_industry"] is None
        runs = _q("SELECT * FROM identity_runs")
        assert len(runs) == 1 and runs[0]["status"] == "ok"
        assert _json(runs[0]["counts"])["tickers"] == 8
        assert _json(runs[0]["sources"])["openfigi"]["fetched"] == 8
        assert _universe_hash() == before, "the identity job must change only the classification and the date"
        # R3 (#86): after the job, the universe's sectors come from the master (the single classification writer)
        assert _universe_gics() == {t: (r["sector"], r["industry"]) for t, r in m.items()}
        assert _universe_gics()["AAPL"] == ("Technology", "Consumer Electronics")
        assert _universe_gics()["LAZR"] == ("Technology", "Unclassified"), "the SIC sector reaches the universe too"
        assert _universe_gics()["FLAGX"] == ("Unclassified",) * 2          # an ETP has no classification
        dates = {r["ticker"]: r["next_earnings_date"] for r in _q(
            "SELECT ticker, next_earnings_date FROM equity_universe")}
        assert dates["AAPL"] == "2026-10-30" and dates["ARCC"] == "2026-10-28", "UW's date reaches the universe"
        assert dates["PDI"] is None and dates["FLAGX"] is None
        assert all(r["industry_group"] is None and r["sub_industry"] is None for r in _q(
            "SELECT industry_group, sub_industry FROM equity_universe")), "the GICS-only levels are NULL"
        counts = _json(runs[0]["counts"])
        assert (counts["applied"], counts["unchanged"], counts["not_in_master"]) == (8, 0, 0)

    def test_second_run_fetches_only_deltas_and_unfreezes_a_delisted_flag(self, seeded, provider_routes):
        assert seeded.run_job("/run/identity").ok
        assert _master()["FLAGX"]["security_type"] == "etp"
        first = {p: len(provider_routes.requests(p)) for p in ("/submissions/", "/v3/mapping", "/files/company", "/dynamic")}
        install(provider_routes, flagx_is_etf=False)          # Nasdaq (healthy) no longer lists FLAGX
        result = seeded.run_job("/run/identity")
        assert result.ok, f"second identity run failed: {result}\n{seeded.logs()}"
        second = {p: len(provider_routes.requests(p)) for p in first}
        assert second["/submissions/"] == first["/submissions/"], "SEC submissions are a 30-day delta"
        assert second["/v3/mapping"] == first["/v3/mapping"], "OpenFIGI is a 90-day delta"
        assert second["/files/company"] > first["/files/company"], "the SEC exchange map refreshes every run"
        assert second["/dynamic"] > first["/dynamic"], "Nasdaq refreshes every run"
        assert _master()["FLAGX"]["security_type"] != "etp", (
            "a healthy Nasdaq that no longer lists FLAGX must un-freeze its ETF type")
        assert len(_q("SELECT * FROM identity_runs")) == 2

    def test_a_failed_sec_map_keeps_the_stored_map_and_the_run_is_partial(self, seeded, provider_routes):
        import RevenueChaser.core.rawstore as rawstore
        rawstore.write_rows("sec_company_tickers", [{"ticker": "GONE", "cik": "5", "name": "Delisted Co"},
                                                    {"ticker": "AAPL", "cik": "320193", "name": "Apple"}])
        provider_routes.route("/files/company_tickers_exchange.json", payload={"error": "down"}, status=500)
        result = seeded.run_job("/run/identity")
        assert result.ok, f"{result}\n{seeded.logs()}"
        assert "sources ok 6/7" in result.message, result.message
        assert {r["ticker"] for r in _q("SELECT ticker FROM sec_company_tickers")} == {"GONE", "AAPL"}
        assert _q("SELECT status FROM identity_runs")[0]["status"] == "partial"
        assert _master()["AAPL"]["cik"] == "320193"

    def test_a_list_name_takes_the_lists_sector_and_keeps_uws_industry(self, seeded):
        assert seeded.run_job("/run/identity").ok
        ozk = _master()["OZK"]
        # UW says Financial Services / Banks - Regional; the S&P 400 page says Industrials
        assert (ozk["sector"], ozk["sector_source"]) == ("Industrials", "sp1500")
        assert (ozk["industry"], ozk["industry_source"]) == ("Banks - Regional", "provider_uw")
        assert "Diversified Support Services" not in {ozk["industry"], ozk["industry_group"], ozk["sub_industry"]}
        assert _universe_gics()["OZK"] == ("Industrials", "Banks - Regional")
        # a name that is not on any list keeps UW's sector
        assert (_master()["ARCC"]["sector"], _master()["ARCC"]["sector_source"]) == ("Financial Services", "provider_uw")

    def test_the_sic_sector_decides_for_a_name_no_other_source_has(self, seeded):
        assert seeded.run_job("/run/identity").ok
        m = _master()
        # LAZR: no list row, no UW row, SIC 3571 (computer hardware); ABCD: SIC 6770 (blank check)
        assert (m["LAZR"]["sector"], m["LAZR"]["sector_source"]) == ("Technology", "sic_explicit")
        assert (m["LAZR"]["industry"], m["LAZR"]["industry_source"]) == ("Unclassified", "none")
        assert (m["ABCD"]["sector"], m["ABCD"]["sector_source"]) == ("Financial Services", "sic_explicit")
        # an ETP stays unclassified whatever its SIC says
        assert (m["FLAGX"]["sector"], m["FLAGX"]["sector_source"].startswith("type_rule")) == ("Unclassified", True)

    def test_the_raw_sic_columns_come_from_the_submissions_cache(self, seeded, provider_routes):
        assert seeded.run_job("/run/identity").ok
        m = _master()
        assert {t: (r["sic"], r["sic_group2"], r["sic_source"]) for t, r in m.items() if r["sic"]} == {
            "AAPL": ("3571", "35", "sec_submissions"), "ARCC": ("6726", "67", "sec_submissions"),
            "ASML": ("3571", "35", "sec_submissions"), "LAZR": ("3571", "35", "sec_submissions"),
            "ABCD": ("6770", "67", "sec_submissions")}
        assert m["AAPL"]["sic_description"] == "x"
        assert m["PDI"]["sic"] is None and m["PDI"]["sic_source"] is None, "no SIC on the filing: nothing is invented"
        # the columns are fed from the cache: the second run adds no SEC submissions call
        calls = len(provider_routes.requests("/submissions/"))
        assert seeded.run_job("/run/identity").ok
        assert len(provider_routes.requests("/submissions/")) == calls
        assert _master()["AAPL"]["sic"] == "3571"

    def test_a_failed_wikipedia_page_leaves_the_sector_to_uw_and_the_run_partial(self, seeded, provider_routes):
        provider_routes.route("/wiki/List_of_S%26P_500_companies", payload={"error": "down"}, status=500)
        result = seeded.run_job("/run/identity")
        assert result.ok, f"{result}\n{seeded.logs()}"
        assert "sources ok 6/7" in result.message, result.message
        assert _q("SELECT status FROM identity_runs")[0]["status"] == "partial"
        m = _master()
        assert (m["AAPL"]["sector"], m["AAPL"]["sector_source"]) == ("Technology", "provider_uw")
        # all three pages must parse: one failed page means no list rows at all, so UW's sector stands for OZK too
        assert (m["OZK"]["sector"], m["OZK"]["sector_source"]) == ("Financial Services", "provider_uw")

    def test_limit_builds_only_the_first_tickers(self, seeded):
        result = seeded.run_job("/run/identity", limit="2")
        assert result.ok and "2 tickers" in result.message, result
        assert set(_master()) == {"AAPL", "ABCD"}
        gics = _universe_gics()
        assert gics["AAPL"][0] == "Technology"
        assert all(gics[t] == (None, None) for t in gics if t not in ("AAPL", "ABCD")), (
            "tickers without a master row keep what they had (never blanked, never Unclassified)")

    def test_a_bad_limit_is_refused(self, seeded):
        assert seeded.post_fetch("/run/identity", limit="0").status_code == 400
        assert seeded.post_fetch("/run/identity", limit="x").status_code == 400


class TestSettingsRefreshAfterIdentity:
    def test_refresh_makes_no_provider_calls(self, seeded, provider_routes, clear_cache):
        assert seeded.run_job("/run/identity", limit="3").ok
        clear_cache()
        before, before_yf = provider_routes.request_count, seeded.yf_calls()
        assert seeded.view("module_settings", refresh=True).status_code == 200
        assert provider_routes.request_count == before, (
            f"refresh reached the network: {[q.path for q in provider_routes.requests()[before:]]}")
        assert seeded.yf_calls() == before_yf

    def test_the_settings_card_and_the_fetch_identity_button_render_after_a_run(
            self, seeded, provider_routes, clear_cache):
        assert seeded.run_job("/run/identity").ok
        clear_cache()
        before, before_yf = provider_routes.request_count, seeded.yf_calls()
        r = seeded.view("module_settings", refresh=True)
        assert r.status_code == 200
        assert provider_routes.request_count == before, (
            f"refresh reached the network: {[q.path for q in provider_routes.requests()[before:]]}")
        assert seeded.yf_calls() == before_yf
        html = r.text
        assert 'id="identity-health"' in html, "the Identity health card is missing from the Settings board"
        assert '<section id="identity">' in html, "the card must be reachable at #identity"
        assert 'id="identity-types"' in html, "the type-mix section is missing from the Identity health card"
        assert 'data-fetch="/run/identity"' in html and "Fetch identity" in html, (
            "the Fetch identity button is missing from the inventory table")
        assert "SPDR check: 1/1 agree" in html

    def test_the_button_route_runs_the_identity_job(self, seeded):
        result = seeded.run_job("/run/identity")       # exactly what the Fetch identity button posts
        assert result.ok, f"{result}\n{seeded.logs()}"
        assert len(_q("SELECT * FROM identity_runs")) == 1

    def test_first_load_with_no_identity_run_shows_the_empty_state_and_the_button(self, seeded):
        html = seeded.view("module_settings").text
        assert "No identity run yet. Use Fetch identity in the table above." in html
        assert 'data-fetch="/run/identity"' in html, "the Fetch identity button must show before the first run"


class TestSpdrCheck:
    def test_the_run_counts_carry_the_check(self, seeded):
        result = seeded.run_job("/run/identity")
        assert result.ok, f"{result}\n{seeded.logs()}"
        counts = _json(_q("SELECT counts FROM identity_runs")[0]["counts"])
        assert counts["spdr_checked"] == 1 and counts["spdr_agree"] == 1, counts
        assert not _q("SELECT * FROM identity_conflicts WHERE kind = 'spdr'")

    def test_a_mismatch_writes_a_spdr_conflict_and_never_changes_the_master(self, seeded, provider_routes):
        route_spdr(provider_routes, {"xlc": [("AAPL", "Apple Inc.")]})
        result = seeded.run_job("/run/identity")
        assert result.ok, f"{result}\n{seeded.logs()}"
        c, = _q("SELECT * FROM identity_conflicts WHERE kind = 'spdr'")
        assert (c["ticker"], c["field"]) == ("AAPL", "sector")
        assert c["side_a"] == "Technology" and c["side_b"] == "Communication Services", c
        assert _master()["AAPL"]["sector"] == "Technology", "SPDR is a check: it never sets a value"
        assert _json(_q("SELECT counts FROM identity_runs")[0]["counts"])["spdr_agree"] == 0

    def test_a_spdr_500_keeps_the_run_ok_writes_no_conflict_and_shows_on_the_card(
            self, seeded, provider_routes, clear_cache):
        provider_routes.route("/spdr/holdings-daily-us-en-xle.xlsx", payload={"error": "down"}, status=500)
        result = seeded.run_job("/run/identity")
        assert result.ok, f"{result}\n{seeded.logs()}"
        assert "sources ok 7/7" in result.message, result.message
        run = _q("SELECT * FROM identity_runs")[0]
        assert run["status"] == "ok", "a failed SPDR check must not make the run partial"
        assert "spdr_checked" not in _json(run["counts"])
        assert not _q("SELECT * FROM identity_conflicts WHERE kind = 'spdr'")
        clear_cache()
        html = seeded.view("module_settings", refresh=True).text
        assert "SPDR check failed" in html, "the Settings card must say the SPDR check failed"
