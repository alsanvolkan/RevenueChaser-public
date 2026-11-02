"""``funds/etf_holdings``: the rotation board's ETFs read whole — the issuer's own file where there is one (the
SPDR workbooks and the iShares CSVs, both parsers already the index job's), UW's holdings answer for the rest, no
top-10 cap and no membership vote."""
from __future__ import annotations

from pathlib import Path

import pytest

from RevenueChaser.core.providers import sec_edgar, unusual_whales
from RevenueChaser.rcdata import ops, serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.spine import etf_holdings as etf, sec_tickers

from . import spine_fixture as fx

AS_OF = "2026-10-27"


@pytest.fixture
def env(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(unusual_whales, "BASE", "https://uw.test/api")
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "uw-test-key")
    monkeypatch.setattr(etf, "SWEEP_PER_RUN", 0)     # these tests are the daily funds; the sweep has its own file
    http_mock.add("company_tickers_exchange.json", fx.SEC_FILE)
    base.run_request({"dataset": sec_tickers.NAME, "params": {"as_of": AS_OF}})
    http_mock.requests.clear()
    fx.add_etf_routes(http_mock)
    return http_mock


def _served(where: str = "") -> list[tuple]:
    with serving.open_read("spine") as con:
        return con.execute(f"SELECT as_of, etf, provider, ticker, cusip, name, weight FROM {etf.TABLE} "
                           + (f"WHERE {where} " if where else "") + "ORDER BY etf, weight DESC").fetchall()


# ── The fund list and the URLs (the issuer of each fund was confirmed by fetching it once, 30 Oct 2026) ───────

def test_the_funds_are_the_rotation_boards_sector_and_theme_etfs_but_the_commodity_trusts():
    """The board's own 34 are the dataset's, less GLD, SLV and USO (commodity trusts: no equity to hold), plus the
    seven the relatedness peer sets need that the board does not run.

    The ``34`` is deliberate: adding an ETF to the board's sector or theme groups fails this test, which is where
    the decision whether the new fund has a keyless holdings file or goes to UW belongs."""
    from RevenueChaser.core.rotation_reference import ROTATION_ETFS
    board = {t for t, info in ROTATION_ETFS.items() if info["group"] in ("sector", "theme")}
    assert len(board) == 34
    assert {f.etf for f in etf.FILES} == (board - {"GLD", "SLV", "USO"}) | etf.EXTRA
    assert len(etf.FILES) == 38 and len({f.etf for f in etf.FILES}) == 38


def test_the_spdr_funds_are_the_issuers_daily_holdings_workbooks():
    urls = {f.etf: f.url() for f in etf.FILES if f.provider == "ssga"}
    assert len(urls) == 18
    assert urls["XLK"] == ("https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/"
                           "holdings-daily-us-en-xlk.xlsx")
    assert urls["KIE"].endswith("holdings-daily-us-en-kie.xlsx")


def test_iak_is_an_ishares_fund_not_a_spdr():
    """Measured 30 Oct 2026: the SPDR path answers 404 for IAK (KIE is the SPDR insurance fund), and iShares'
    own product screener gives IAK product 239515 (239516 is the Medical Devices fund)."""
    by_etf = {f.etf: f.provider for f in etf.FILES}
    assert by_etf["IAK"] == "ishares" and by_etf["KIE"] == "ssga"
    assert etf.ISHARES_PRODUCTS["IAK"][0] == "239515"


def test_each_ishares_fund_is_read_as_the_shape_it_serves():
    """The shape is the fund's, not the file's: IAK carries ``Type``, the five sector funds do not. A file that
    changes shape is a failed file for that fund, not a quietly different read."""
    typed = {name: shape[2] for name, shape in etf.ISHARES_PRODUCTS.items()}
    assert typed == {"IAK": True, "SOXX": False, "IBB": False, "ITA": False, "ITB": False, "IGV": False}
    iak = next(f for f in etf.FILES if f.etf == "IAK")
    soxx = next(f for f in etf.FILES if f.etf == "SOXX")
    assert len(iak.parse(fx.ishares_csv(fx.SP500))) == len(fx.SP500)
    assert len(soxx.parse(fx.ishares_csv(fx.SP500, typed=False))) == len(fx.SP500)
    with pytest.raises(base.SourceError, match="Type"):
        iak.parse(fx.ishares_csv(fx.SP500, typed=False))


def test_the_ishares_funds_are_their_product_pages_latest_holdings_csv():
    urls = {f.etf: f.url() for f in etf.FILES if f.provider == "ishares"}
    assert urls == {
        "SOXX": "https://www.ishares.com/us/products/239705/ishares-semiconductor-etf/latest-holdings.csv",
        "IBB": "https://www.ishares.com/us/products/239699/ishares-biotechnology-etf/latest-holdings.csv",
        "ITA": "https://www.ishares.com/us/products/239502/ishares-u-s-aerospace-defense-etf/"
               "latest-holdings.csv",
        "ITB": "https://www.ishares.com/us/products/239512/ishares-u-s-home-construction-etf/"
               "latest-holdings.csv",
        "IGV": "https://www.ishares.com/us/products/239771/ishares-expanded-tech-software-sector-etf/"
               "latest-holdings.csv",
        "IAK": "https://www.ishares.com/us/products/239515/ishares-u-s-insurance-etf/latest-holdings.csv"}


def test_the_uw_funds_are_asked_for_their_whole_holdings():
    uw = {f.etf: f.url() for f in etf.FILES if f.provider == "uw"}
    assert set(uw) == {"SMH", "PAVE", "AIQ", "LIT", "URA", "NLR", "AIRR", "SKYY", "WCLD", "ARKQ", "ROBO", "JETS",
                       "TAN", "PBW"}
    assert uw["SMH"].endswith("/etfs/SMH/holdings")


# ── UW's answer ──────────────────────────────────────────────────────────────────────────────────────────────

def test_the_uw_answer_is_read_whole_not_capped_at_ten():
    """The top-10 cap is the Rotation board's own (``module_sector_rotation/etf_holdings.py``); this dataset keeps
    every line, by weight, and drops a line with no ticker."""
    many = [(f"T{i}", None, f"NAME {i}") for i in range(12)]
    rows = etf.parse_uw(fx.uw_holdings_json(many))
    assert len(rows) == 12
    assert [r["ticker_raw"] for r in rows[:3]] == ["T0", "T1", "T2"] and rows[0]["cusip"] is None
    assert rows[0]["weight"] == 10.0 and rows[0]["name"] == "NAME 0"


def test_a_repeated_uw_ticker_keeps_the_newer_row():
    rows = etf.parse_uw(fx.uw_holdings_json(fx.SP500, repeat=("MSFT",)))
    msft = [r for r in rows if r["ticker_raw"] == "MSFT"]
    assert len(msft) == 1 and msft[0]["weight"] == 0.001


def test_a_uw_row_with_a_cusip_keeps_it_normalised_and_one_without_has_none():
    """The live answer carries ``cusip`` (SMH, 1 Nov 2026) though the schema does not name it: kept when it is a
    CUSIP, as the issuer parsers keep theirs, so the peer sets can match by CUSIP; None when absent or no CUSIP."""
    rows = etf.parse_uw(fx.uw_holdings_json(fx.SP500, cusips=True))
    assert {(r["ticker_raw"], r["cusip"]) for r in rows} == {(t, c) for t, c, _n in fx.SP500}
    assert {r["cusip"] for r in etf.parse_uw(fx.uw_holdings_json(fx.SP500))} == {None}
    odd = b'{"data": [{"ticker": "A", "cusip": "N/A", "weight": "1"}, {"ticker": "B", "cusip": null, "weight": "1"}]}'
    assert {r["cusip"] for r in etf.parse_uw(odd)} == {None}


@pytest.mark.parametrize("body", [b"{}", b'{"data": {"holdings": []}}', b"not json"])
def test_a_uw_answer_of_another_shape_is_a_source_error(body):
    with pytest.raises(base.SourceError):
        etf.parse_uw(body)


# ── ARK's own holdings CSV (ARKQ; the issuer file of #339, read 1 Nov 2026) ─────────────────────────────────────

ARK_FILE = Path(__file__).parent / "fixtures" / "ark_arkq_holdings.csv"


def test_the_ark_file_is_the_issuers_own_csv():
    ark = etf.EtfFile("ARKQ", "ark", "csv")
    assert ark.url() == ("https://assets.ark-funds.com/fund-documents/funds-etf-csv/"
                         "ARK_AUTONOMOUS_TECH._&_ROBOTICS_ETF_ARKQ_HOLDINGS.csv")
    assert ark.raw_name() == "ark-ARKQ.csv"


def test_the_real_ark_file_reads_its_stock_lines_with_their_cusips():
    """A trimmed copy of the real file of 30 Oct 2026: the equity lines with ticker, CUSIP and weight; the
    money-market line (no ticker, the made-up identifier ``X9USDGSFT``) and the disclaimer line are no holding."""
    rows = etf.parse_ark(ARK_FILE.read_bytes(), "ARKQ")
    assert [r["ticker_raw"] for r in rows] == ["TSLA", "NVDA", "SYM", "KMTUY", "ESLT", "BYDDY"]
    assert rows[0] == {"ticker_raw": "TSLA", "cusip": "88160R101", "name": "TESLA INC", "weight": 12.55}
    assert rows[4]["cusip"] == "M3760D101", "a CINS CUSIP (Israel) is kept"
    assert not any(r["cusip"] == "X9USDGSFT" for r in rows)


def test_a_us_bloomberg_exchange_code_is_dropped_and_nothing_else_is_guessed():
    """``SYM UQ`` is Symbotic on Nasdaq: the real file of 30 Oct 2026 wrote it so. Without the rule the shared
    ``norm_ticker`` reads it as the share class ``SYM-UQ``. A foreign listing keeps what the file wrote in
    ``ticker_raw`` and gets **no** ticker, whether its code is a number (``6954 JT``) or letters (``KOG NO``,
    ``ABBN SE``, which ``norm_ticker`` alone would serve as the US share classes ``KOG-NO`` and ``ABBN-SE``)."""
    head = b"date,fund,company,ticker,cusip,shares,market value ($),weight (%)\n"
    body = (b'10/30/2026,ARKQ,SYMBOTIC INC,SYM UQ,87151X101,"1","$1.00",1.80%\n'
            b'10/30/2026,ARKQ,FANUC CORP,6954 JT,J13440102,"1","$1.00",1.00%\n'
            b'10/30/2026,ARKQ,KONGSBERG GRUPPEN,KOG NO,R3796D109,"1","$1.00",0.90%\n'
            b'10/30/2026,ARKQ,ABB LTD-REG,ABBN SE,H0010V101,"1","$1.00",0.80%\n'
            b'10/30/2026,ARKQ,DEERE & CO,DE UN,244199105,"1","$1.00",0.50%\n')
    rows = etf.parse_ark(head + body, "ARKQ")
    assert [r["ticker_raw"] for r in rows] == ["SYM", "6954 JT", "KOG NO", "ABBN SE", "DE"]
    assert [etf.served_ticker(r) for r in rows] == ["SYM", None, None, None, "DE"]
    assert [r["cusip"] for r in rows[1:4]] == ["J13440102", "R3796D109", "H0010V101"]


def test_a_foreign_listing_reaches_the_parquet_but_not_the_served_table(env, http_mock):
    _uw_empty(http_mock, "ARKQ")
    body = fx.ark_csv([("TSLA", "88160R101", "TESLA INC"), ("KOG NO", "R3796D109", "KONGSBERG GRUPPEN")])
    http_mock.routes.insert(0, ("_ARKQ_HOLDINGS.csv", lambda r: (200, body, {}), 200, {}))
    base.run_request({"dataset": etf.NAME, "params": {"as_of": AS_OF}})
    assert [r[3] for r in _served("etf = 'ARKQ'")] == ["TSLA"]
    import duckdb
    part = etf.parquet_dir() / f"date={AS_OF}" / "part-0.parquet"
    kept = duckdb.connect().execute(f"SELECT ticker, ticker_raw, cusip FROM read_parquet('{part.as_posix()}') "
                                    "WHERE etf = 'ARKQ' AND cusip = 'R3796D109'").fetchall()
    assert kept == [(None, "KOG NO", "R3796D109")]


@pytest.mark.parametrize("body, match", [
    (b"<html><body>Not found</body></html>", "header"),
    (b"date,fund,company,ticker,weight (%)\n10/30/2026,ARKQ,TESLA INC,TSLA,12.55%\n", "cusip"),
    (ARK_FILE.read_bytes().replace(b",ARKQ,", b",ARKK,"), "ARKK"),
])
def test_an_ark_file_of_another_shape_or_fund_is_a_source_error(body, match):
    """A moved URL answers a page, a changed file loses a column, and a file of another fund is no holding of
    this one: each fails the fund loudly instead of being read some other way."""
    with pytest.raises(base.SourceError, match=match):
        etf.parse_ark(body, "ARKQ")


# ── A run ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_run_reads_every_fund_once_and_serves_its_whole_holdings(env):
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": AS_OF}})
    assert row["result"] == "changed" and row["calls"] == 38 and row["coverage"] == 1.0
    assert len(env.calls()) == 38, "one request per fund, no retry"
    served = _served()
    assert len(served) == 38 * len(fx.SP500)
    assert {r[2] for r in served} == {"ssga", "ishares", "uw"}
    xlk = _served("etf = 'XLK'")
    assert {(r[3], r[4]) for r in xlk} == {(t, c) for t, c, _n in fx.SP500}, "the SPDR file's CUSIPs are kept"
    assert str(xlk[0][0]) == AS_OF
    smh = _served("etf = 'SMH'")                               # UW: by weight; these rows carry no CUSIP key
    assert [r[3] for r in smh] == [t for t, _c, _n in fx.SP500] and {r[4] for r in smh} == {None}
    assert smh[0][6] == 10.0 and smh[-1][6] == 6.0


def test_the_cash_and_futures_lines_of_a_file_are_not_holdings(env):
    base.run_request({"dataset": etf.NAME, "params": {"as_of": AS_OF}})
    tickers = {r[3] for r in _served()}
    assert not tickers & {"USD", "XTSLA", "WFFUT", "IXTZ6", "CASH_USD"}


def test_a_moved_issuer_url_is_a_source_failed_issue_and_the_other_funds_go_on(monkeypatch, env):
    monkeypatch.setattr(etf, "ISHARES_PRODUCTS", {**etf.ISHARES_PRODUCTS, "IGV": ("999999", "moved")})
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": AS_OF}})
    assert row["result"] == "changed" and row["coverage"] == round(37 / 38, 4)
    n, issues = ops.open_issues(etf.NAME)
    assert n == 1 and [i["key"] for i in issues] == ["ishares-IGV.csv"] and "failed files" in row["note"]
    assert not _served("etf = 'IGV'") and len(_served("etf = 'IBB'")) == len(fx.SP500)


def test_every_fund_failing_fails_the_run(env, http_mock):
    http_mock.routes[:] = [r for r in http_mock.routes if "company_tickers" in r[0]]
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": AS_OF}})
    assert row["result"] == "failed" and "every fund file failed" in row["note"]


def test_a_fund_already_fetched_is_not_fetched_again(env):
    base.run_request({"dataset": etf.NAME, "params": {"as_of": AS_OF}})
    env.requests.clear()
    base.run_request({"dataset": etf.NAME, "params": {"as_of": AS_OF}})
    assert env.requests == []


def test_an_answer_with_no_holding_in_it_is_a_failed_file_not_the_funds_whole_day(env, http_mock):
    """The raw file is the resume marker, so an empty answer kept once would be that fund's day: UW answers
    ``{"data": []}`` for a ticker it does not know, and a header with no equity line is the same nothing."""
    http_mock.routes.insert(0, ("/etfs/SMH/holdings", lambda r: (200, b'{"data": []}', {}), 200, {}))
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": AS_OF}})
    assert row["result"] == "changed" and row["coverage"] == round(37 / 38, 4)
    n, issues = ops.open_issues(etf.NAME)
    assert n == 1 and [i["key"] for i in issues] == ["uw-SMH.json"]
    assert "no holding line" in (issues[0]["detail"] or "") and not _served("etf = 'SMH'")


def test_a_failed_fund_is_fetched_again_in_the_same_day_and_joins_the_others(monkeypatch, env, http_mock):
    """The day's real pattern: one fund fails, the run is repeated, the 37 raw files are resumed and only the
    missing one is asked. Its rows join the day's partition instead of replacing it."""
    good = etf.ISHARES_PRODUCTS
    monkeypatch.setattr(etf, "ISHARES_PRODUCTS", {**good, "IGV": ("999999", "moved", False)})
    base.run_request({"dataset": etf.NAME, "params": {"as_of": AS_OF}})
    assert len(_served()) == 37 * len(fx.SP500)
    monkeypatch.setattr(etf, "ISHARES_PRODUCTS", good)      # not monkeypatch.undo(): it would undo the env too
    http_mock.requests.clear()
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": AS_OF}})
    assert len(http_mock.requests) == 1, "only the fund that failed is asked again"
    assert len(_served()) == 38 * len(fx.SP500) and len(_served("etf = 'IGV'")) == len(fx.SP500)
    assert row["coverage"] == 1.0 and row["result"] == "changed"


def test_a_second_day_is_a_second_partition_and_both_are_served(env):
    base.run_request({"dataset": etf.NAME, "params": {"as_of": AS_OF}})
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": "2026-10-28"}})
    days = {str(r[0]) for r in _served()}
    assert days == {AS_OF, "2026-10-28"} and len(_served()) == 2 * 38 * len(fx.SP500)
    assert row["rows"] == 38 * len(fx.SP500), "the check row counts the newest day"


def test_without_a_uw_key_the_issuer_files_are_read_and_the_missing_key_is_an_issue(monkeypatch, env):
    """The 24 issuer files, and ARKQ from ARK's own file: a fund with an issuer file needs no UW key."""
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY")
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": AS_OF}})
    assert row["result"] == "changed" and row["calls"] == 25
    assert row["coverage"] == round(25 / 38, 4)
    n, issues = ops.open_issues(etf.NAME)
    assert n == 1 and issues[0]["kind"] == "missing_key"
    assert len(_served()) == 25 * len(fx.SP500) and not _served("provider = 'uw'")
    assert {r[2] for r in _served("etf = 'ARKQ'")} == {"ark"} and "ARKQ served by ark-ARKQ.csv" in row["note"]


# ── The source rule: UW first, the issuer file when UW holds nothing (#339) ─────────────────────────────────

def _uw_empty(http_mock, fund: str) -> None:
    http_mock.routes.insert(0, (f"/etfs/{fund}/holdings", lambda r: (200, b'{"data": []}', {}), 200, {}))


def test_the_funds_with_an_issuer_file_behind_uw_are_arkq_only():
    """Step 0 of #339: ARKQ has ARK's CSV; AIRR and SKYY (First Trust), WCLD (WisdomTree) and ROBO (ETC) serve
    their holdings in HTML only, so they have no fallback and stay UW's alone."""
    assert {etf_name: f.raw_name() for etf_name, f in etf.FALLBACKS.items()} == {"ARKQ": "ark-ARKQ.csv"}
    assert {f.etf for f in etf.FILES if f.provider == "uw"} >= set(etf.FALLBACKS)
    assert "ark" in etf.PROVIDERS


def test_uw_with_holdings_serves_the_day_and_the_issuer_file_is_not_asked(env):
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": AS_OF}})
    assert row["calls"] == 38 and not [r for r in env.requests if "ark-funds" in str(r.url)]
    assert {r[2] for r in _served("etf = 'ARKQ'")} == {"uw"} and "ARKQ served by" not in (row["note"] or "")


def test_an_empty_uw_answer_falls_back_to_the_issuer_file_and_the_note_says_so(env, http_mock):
    """The day of build #40: UW answers ``{"data": []}`` for ARKQ. ARK's file serves the fund, the raw file's name
    says which source served the day, and no issue is opened (the fund is served)."""
    _uw_empty(http_mock, "ARKQ")
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": AS_OF}})
    assert row["result"] == "changed" and row["coverage"] == 1.0 and row["calls"] == 39
    assert ops.open_issues(etf.NAME)[0] == 0
    arkq = _served("etf = 'ARKQ'")
    assert {r[2] for r in arkq} == {"ark"} and {(r[3], r[4]) for r in arkq} == {(t, c) for t, c, _n in fx.SP500}
    day = etf.raw_dir() / AS_OF
    assert (day / "ark-ARKQ.csv").exists() and not (day / "uw-ARKQ.json").exists()
    assert "ARKQ served by ark-ARKQ.csv" in row["note"] and "no holding line" in row["note"]


def test_a_fund_the_issuer_file_served_is_resumed_without_asking_uw_again(env, http_mock):
    _uw_empty(http_mock, "ARKQ")
    base.run_request({"dataset": etf.NAME, "params": {"as_of": AS_OF}})
    http_mock.requests.clear()
    base.run_request({"dataset": etf.NAME, "params": {"as_of": AS_OF}})
    assert http_mock.requests == []


def test_both_sources_failing_is_one_source_failed_issue_naming_both(env, http_mock):
    _uw_empty(http_mock, "ARKQ")
    http_mock.routes.insert(0, ("_ARKQ_HOLDINGS.csv", lambda r: (200, b"<html>moved</html>", {}), 200, {}))
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": AS_OF}})
    assert row["coverage"] == round(37 / 38, 4) and not _served("etf = 'ARKQ'")
    n, issues = ops.open_issues(etf.NAME)
    assert n == 1 and issues[0]["kind"] == "source_failed" and issues[0]["key"] == "uw-ARKQ.json"
    assert "no holding line" in issues[0]["detail"] and "ark-ARKQ.csv" in issues[0]["detail"]
    assert "failed files: uw-ARKQ.json" in row["note"]


def test_a_fund_with_no_issuer_file_still_fails_alone_on_an_empty_uw_answer(env, http_mock):
    _uw_empty(http_mock, "WCLD")
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": AS_OF}})
    assert row["calls"] == 38 and row["coverage"] == round(37 / 38, 4)
    assert [i["key"] for i in ops.open_issues(etf.NAME)[1]] == ["uw-WCLD.json"]


def test_the_issuer_file_host_has_a_day_cap_and_board_words():
    from RevenueChaser.rcdata import caps
    from RevenueChaser.rcdata.needs import screen
    assert caps.DAY_CAPS["ark"] > 0 and screen.PROVIDERS["ark"].name == "ARK"
