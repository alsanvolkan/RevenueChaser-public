"""``funds/etf_holdings``: the rotation board's ETFs read whole — the issuer's own file where there is one (the
SPDR workbooks and the iShares CSVs, both parsers already the index job's), UW's holdings answer for the rest, no
top-10 cap and no membership vote."""
from __future__ import annotations

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
    seven the relatedness peer sets need that the board does not run."""
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


@pytest.mark.parametrize("body", [b"{}", b'{"data": {"holdings": []}}', b"not json"])
def test_a_uw_answer_of_another_shape_is_a_source_error(body):
    with pytest.raises(base.SourceError):
        etf.parse_uw(body)


# ── A run ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_run_reads_every_fund_once_and_serves_its_whole_holdings(env):
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": AS_OF}})
    assert row["result"] == "changed" and row["calls"] == 38 and row["coverage"] == 1.0
    assert len(env.calls()) == 38, "one request per fund, no retry"
    served = _served()
    assert len(served) == 38 * len(fx.SP500)
    assert {r[2] for r in served} == {"ssga", "ishares", "uw"}
    xlk = _served("etf = 'XLK'")
    assert [(r[3], r[4]) for r in xlk] == [("MSFT", "594918104"), ("ETN", "G29183103"), ("FDX", "31428X106"),
                                           ("NVR", "62944T105"), ("WFC", "949746101")]
    assert [r[3] for r in _served("etf = 'SMH'")] == ["MSFT", "ETN", "FDX", "NVR", "WFC"]   # UW, by weight
    assert _served("etf = 'SMH'")[0][6] == 10.0 and str(xlk[0][0]) == AS_OF


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


def test_without_a_uw_key_the_issuer_files_are_read_and_the_missing_key_is_an_issue(monkeypatch, env):
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY")
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": AS_OF}})
    assert row["result"] == "changed" and row["calls"] == 24
    assert row["coverage"] == round(24 / 38, 4)
    n, issues = ops.open_issues(etf.NAME)
    assert n == 1 and issues[0]["kind"] == "missing_key"
    assert len(_served()) == 24 * len(fx.SP500) and not _served("provider = 'uw'")
