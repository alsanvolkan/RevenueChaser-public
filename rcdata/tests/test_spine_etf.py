"""``etf``: the ETF universe, one row per ticker, built by ``spine/security`` from Alpha Vantage's listing status,
the N-CEN data sets and the spine's own ``etp`` rows (#346, avo's decisions of 1 Nov 2026).

No threshold and no ``active`` column: every ETF is stored, thresholds are a board's settings at read time.
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.spine import av_listing, etf, ncen, sec_tickers, security

from . import spine_fixture as fx

# The equity file's ETP rows: SPY a unit investment trust (its trust files N-CEN as an S-6 registrant, ``NCEN_Q2``),
# GLD a commodity trust (files no N-CEN), FNGU a bank's note (typed etp by its Cboe venue in this fixture).
SEC_FILE = {"fields": ["cik", "name", "ticker", "exchange"],
            "data": [[789019, "MICROSOFT CORP", "MSFT", "Nasdaq"],
                     [884394, "SPDR S&P 500 ETF TRUST", "SPY", "NYSE"],
                     [1222333, "SPDR GOLD TRUST", "GLD", "NYSE"],
                     [927971, "BANK OF MONTREAL /CAN/", "FNGU", "CBOE"]]}
AV = ("symbol,name,exchange,assetType,ipoDate,delistingDate,status\r\n"
      "A,Agilent Technologies Inc,NYSE,Stock,1999-11-18,null,Active\r\n"
      "XLK,TECHNOLOGY SELECT SECTOR SPDR FUND ,NYSE ARCA,ETF,1998-12-16,null,Active\r\n"
      "SOXX,ISHARES SEMICONDUCTOR ETF ,NASDAQ,ETF,2001-07-10,null,Active\r\n"
      "SPY,SPDR S&P 500 ETF TRUST ,NYSE ARCA,ETF,1993-01-22,null,Active\r\n"
      "GLD,SPDR GOLD SHARES ,NYSE ARCA,ETF,2004-11-18,null,Active\r\n"
      "BNKU,Bank of Montreal,NYSE ARCA,ETF,2019-04-03,null,Active\r\n"
      "QYLD,GLOBAL X NASDAQ 100 COVERED CALL ETF ,NASDAQ,ETF,2013-12-12,null,Active\r\n"
      "NEWE,NEW EXAMPLE ETF ,NYSE ARCA,ETF,2026-10-01,null,Active\r\n"
      "TAPR,Barclays Bank PLC,BATS,ETF,2014-07-15,null,Active\r\n")


@pytest.fixture
def built(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setenv("RC_HTTP_CONTACT", "ops@example.org")
    monkeypatch.setenv("RC_ALPHAVANTAGE_BASE", "https://av.test")
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "av-test-key")
    http_mock.add("company_tickers_exchange.json", SEC_FILE)
    fx.add_ncen_routes(http_mock)
    http_mock.add("state=delisted", "{}")
    http_mock.add("av.test/query", AV)
    base.run_request({"dataset": ncen.NAME, "params": {"mode": "backfill", "to": "2026-11-02"}})
    base.run_request({"dataset": av_listing.NAME, "params": {"as_of": "2026-11-02"}})
    return base.run_request({"dataset": sec_tickers.NAME, "params": {"as_of": "2026-11-02"}})


def _etf() -> dict[str, dict]:
    with serving.open_read("spine") as con:
        cur = con.execute("SELECT * FROM etf ORDER BY ticker")
        cols = [d[0] for d in cur.description]
        return {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}


def test_the_universe_is_av_etfs_and_ncen_etfs_and_the_spines_etp_rows_with_their_source(built):
    rows = _etf()
    assert {t: r["source"] for t, r in rows.items()} == {
        "XLK": "both", "SOXX": "both", "SOXL": "ncen", "CCOI": "ncen", "SPY": "av", "GLD": "av", "BNKU": "av",
        "QYLD": "av", "NEWE": "av", "FNGU": "etp", "LEVX": "ncen", "TAPR": "both"}
    assert "A" not in rows and "MSFT" not in rows             # AV stocks and the spine's common rows are not ETFs


def test_the_columns_are_the_plans_and_there_is_no_active_column(built):
    with serving.open_read("spine") as con:
        cols = [d[0] for d in con.execute("SELECT * FROM etf LIMIT 0").description]
    assert cols == ["ticker", "name", "series_id", "cik", "source", "is_etn", "asset_class", "focus", "country",
                    "is_index", "is_lev_inverse", "is_money_market", "is_option_income", "is_fund_of_funds",
                    "inception", "aum", "aum_as_of", "adv_usd_20d", "expense_ratio", "sector_name", "source_as_of",
                    "fetched_at", "run_id"]


def test_ncen_fills_the_flags_the_newest_filings_aum_and_av_the_inception(built):
    xlk, soxx, soxl = (_etf()[t] for t in ("XLK", "SOXX", "SOXL"))
    assert (xlk["name"], xlk["series_id"], xlk["cik"]) == ("Technology Select Sector SPDR Fund", "S000006408",
                                                           "1064641")
    assert xlk["is_index"] is True and xlk["is_lev_inverse"] is False and xlk["is_money_market"] is False
    assert (xlk["aum"], xlk["aum_as_of"]) == (80_000_000_000.0, date(2026, 4, 30))     # the July filing, not April's
    assert xlk["inception"] == date(1998, 12, 16) and xlk["sector_name"] == "Technology"
    assert (soxx["aum"], soxx["aum_as_of"]) == (15_000_000_000.0, date(2026, 3, 31))   # daily first, then monthly
    assert soxl["is_lev_inverse"] is True and soxl["inception"] is None and soxl["sector_name"] is None


def test_a_row_without_ncen_has_unknown_flags_not_false_ones(built):
    qyld = _etf()["QYLD"]
    assert qyld["is_index"] is None and qyld["is_fund_of_funds"] is None and qyld["aum"] is None
    assert qyld["is_option_income"] is True                   # the name rule needs no N-CEN


def test_option_income_is_a_name_rule(built):
    rows = _etf()
    assert rows["CCOI"]["is_option_income"] is True and rows["CCOI"]["is_fund_of_funds"] is True
    assert rows["XLK"]["is_option_income"] is False


@pytest.mark.parametrize("name,etn", [
    ("iPath Bloomberg Commodity Index Total Return ETN", True), ("Bank of Montreal", True),
    ("BANK OF MONTREAL /CAN/", True), ("UBS AG London Branch", True), ("Barclays Bank PLC", True),
    ("Deutsche Bank AG London", True), ("Credit Suisse AG Nassau Branch", True),
    ("ETRACS 2xMonthly Pay Leveraged Preferred Stock Index ETN due September 25 2048", True),
    ("Abacus Global Management Inc. 9.875% Fixed Rate Senior Notes due 2028", True),
    ("F/M US TREASURY 10 YEAR NOTE ETF", False), ("Invesco AAA CLO Floating Rate Note ETF", False),
    ("Credit Suisse High Yield Bond Fund", False), ("INVESCO KBW BANK ETF", False),
    ("DIREXION DAILY REGIONAL BANKS BULL 3X SHARES", False)])
def test_the_etn_name_rule(name, etn):
    """Names from Alpha Vantage's list of 1 Nov 2026: the notes and the banks' products, and the funds whose names
    carry a bank or the word note and are not one."""
    assert etf.is_etn(name) is etn


@pytest.mark.parametrize("name,income", [
    ("Global X Nasdaq 100 Covered Call ETF", True), ("JPMorgan Equity Premium Income ETF", False),
    ("FT Vest U.S. Equity Buffer ETF - March", True), ("Innovator Defined Outcome Buffered ETF", True),
    ("Calamos Autocallable Income ETF", True), ("Example Target Outcome ETF", True),
    ("Example Option Income Strategy ETF", True), ("Technology Select Sector SPDR Fund", False)])
def test_the_option_income_name_rule(name, income):
    """The plan's words only; ``premium income`` is not one of them (an open question, not a guess)."""
    assert etf.is_option_income(name) is income


@pytest.mark.parametrize("name,lev", [
    ("Tradr 2X Long IBM Daily ETF", True), ("Defiance Daily Target 2X Long RIOT ETF", True),
    ("ProShares UltraPro QQQ", True), ("ProShares Ultra S&P500", True),
    ("ProShares UltraShort 20+ Year Treasury", True), ("ProShares UltraPro Short QQQ", True),
    ("Innovator U.S. Equity Ultra Buffer ETF - January", False), ("Angel Oak UltraShort Income ETF", False),
    ("Invesco S&P Ultra Dividend Revenue ETF", False), ("YieldMax Ultra Option Income Strategy ETF", False), ("Direxion Daily Semiconductor Bear 3X Shares", True),
    ("Direxion Daily Regional Banks Bull 3X Shares", True), ("ProShares Short S&P500", True),
    ("MicroSectors FANG+ Index -3X Inverse Leveraged ETNs", True), ("Simplify 1.5x Example ETF", True),
    ("AXS 1.25X NVDA Bear Daily ETF", True), ("GraniteShares 1x Short TSLA Daily ETF", True),
    ("ProShares S&P 500 Dividend Aristocrats ETF", False), ("iShares Short Treasury Bond ETF", False),
    ("JPMorgan Ultra-Short Income ETF", False), ("iShares Ultra Short-Term Bond ETF", False),
    ("SPDR Blackstone Senior Loan ETF", False), ("Invesco Senior Loan ETF", False),
    ("VanEck BDC Income ETF", False), ("Vanguard Short-Term Bond ETF", False),
    ("Technology Select Sector SPDR Fund", False)])
def test_the_leveraged_or_inverse_name_rule(name, lev):
    """Review S1 of PR 349: 185 leveraged ``IS_ETF=Y`` series lack N-CEN's flag. "Short" counts only in a
    leveraged or inverse context, never a short-duration bond fund's."""
    assert etf.is_lev_inverse(name) is lev


def test_lev_inverse_is_ncens_flag_or_the_name_rule(built):
    rows = _etf()
    assert rows["SOXL"]["is_lev_inverse"] is True              # N-CEN's flag
    assert rows["XLK"]["is_lev_inverse"] is False
    assert rows["LEVX"]["is_lev_inverse"] is True              # N-CEN has no flag; its name has the leverage
    assert rows["QYLD"]["is_lev_inverse"] is None              # not in N-CEN and no leverage in its name


def test_an_ncen_etf_is_never_flagged_a_note(built):
    """Review N3: N-CEN's ``IS_ETF=Y`` is a registered fund's own word, so it overrules the name rule (TAPR, a
    fund AV names "Barclays Bank PLC")."""
    rows = _etf()
    assert rows["TAPR"]["is_etn"] is False and rows["TAPR"]["source"] == "both"


def test_etns_are_flagged_and_kept(built):
    rows = _etf()
    assert rows["BNKU"]["is_etn"] is True and rows["FNGU"]["is_etn"] is True
    assert rows["XLK"]["is_etn"] is False and rows["GLD"]["is_etn"] is False


def test_an_etp_trust_that_files_no_ncen_is_a_commodity_and_a_uit_that_files_one_is_not(built):
    rows = _etf()
    assert rows["GLD"]["asset_class"] == "commodity"
    assert rows["SPY"]["asset_class"] is None                 # SPY's trust files N-CEN as an S-6 registrant
    assert rows["FNGU"]["asset_class"] is None                # a note, not a trust
    assert {r["asset_class"] for t, r in rows.items() if t not in ("GLD",)} == {None}


def test_the_columns_later_prs_fill_stay_null(built):
    for r in _etf().values():
        assert (r["focus"], r["country"], r["adv_usd_20d"], r["expense_ratio"]) == (None, None, None, None)


def test_with_fewer_than_four_quarters_held_no_etp_row_is_called_a_commodity(monkeypatch, http_mock):
    """Review M1 of PR 349: one or two quarters hold a fraction of the filers, so a 1940-Act trust that filed in
    another quarter would read as "files no N-CEN". GLD's call waits for the four quarters."""
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    http_mock.add("company_tickers_exchange.json", SEC_FILE)
    fx.add_ncen_routes(http_mock, quarters=("2026q3", "2026q2"))
    base.run_request({"dataset": ncen.NAME, "params": {"as_of": "2026-11-02"}})
    base.run_request({"dataset": sec_tickers.NAME, "params": {"as_of": "2026-11-02"}})
    assert ncen.quarters_held() == 2
    assert {r["asset_class"] for r in _etf().values()} == {None}


def test_without_ncen_no_etp_row_is_called_a_commodity(monkeypatch, http_mock):
    """No N-CEN quarter held: whether a trust files one is unknown, so the rule says nothing."""
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    http_mock.add("company_tickers_exchange.json", SEC_FILE)
    base.run_request({"dataset": sec_tickers.NAME, "params": {"as_of": "2026-11-02"}})
    rows = _etf()
    assert set(rows) == {"SPY", "GLD", "FNGU"} and {r["source"] for r in rows.values()} == {"etp"}
    assert {r["asset_class"] for r in rows.values()} == {None}


def test_the_spine_note_counts_etfs_without_the_etns(built):
    assert "12 in etf: 10 ETFs, 2 ETNs flagged" in built["note"]


def test_the_spine_serves_etf_at_schema_version_four(built):
    assert serving.SERVING_SCHEMA_VERSION["spine"] == 4 and "etf" in security.TABLES
    with serving.open_read("spine") as con:
        assert con.execute("SELECT version FROM serving_schema_version").fetchone() == (4,)
