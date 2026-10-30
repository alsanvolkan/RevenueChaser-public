"""``funds/index_holdings``: the four file formats parse (cash and futures lines dropped), one request per file, a
failed file is an issue while the others go on, and a short file does not count."""
from __future__ import annotations

import pytest

from RevenueChaser.rcdata import caps, ops
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.spine import funds

from . import spine_fixture as fx


@pytest.fixture
def env(monkeypatch, http_mock):
    from RevenueChaser.core.providers import sec_edgar
    from RevenueChaser.rcdata.build.spine import sec_tickers
    monkeypatch.setattr(funds, "FLOOR", {"sp500": 4, "sp400": 2, "sp600": 2, "ndx": 1})
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    http_mock.add("company_tickers_exchange.json", fx.SEC_FILE)
    base.run_request({"dataset": sec_tickers.NAME, "params": {"as_of": "2026-10-27"}})
    http_mock.requests.clear()
    fx.add_fund_routes(http_mock)
    return http_mock


def test_ssga_drops_the_cash_line_and_reads_the_cusip():
    rows = funds.parse_ssga(fx.ssga_xlsx(fx.SP500))
    assert [r["ticker_raw"] for r in rows] == ["MSFT", "ETN", "FDX", "NVR", "WFC"]
    assert rows[0]["cusip"] == "594918104" and rows[0]["weight"] == 1.0


def test_ishares_keeps_stock_lines_only_and_reads_the_market_weight():
    """A swap and a warrant are filed under Asset Class Equity: only ``Type`` EQUITY is a holding of the stock."""
    rows = funds.parse_ishares(fx.ishares_csv(fx.SP400, drop_dash=True))
    assert [r["ticker_raw"] for r in rows] == ["HEI", "HEIA"] and rows[0]["cusip"] is None
    assert rows[0]["weight"] == 1.0


def test_the_sector_funds_ishares_file_has_no_type_column_and_is_read_as_that_shape():
    """iShares serves two shapes (both measured 30 Oct 2026): the core funds (IVV, IJH, IJR, IAK) carry ``Type``
    and ``Market Weight``, the sector and industry funds (SOXX, IBB, ITA, ITB, IGV) carry neither — their holdings
    are the ``Asset Class`` Equity lines and the weight is ``Weight (%)``. The caller names the shape."""
    rows = funds.parse_ishares(fx.ishares_csv(fx.SP500, typed=False), typed=False)
    assert [r["ticker_raw"] for r in rows] == [t for t, _c, _n in fx.SP500]
    assert rows[0]["weight"] == 1.0 and rows[0]["cusip"] is None
    assert not {"XTSLA", "WFFUT", "IXTZ6", "USD"} & {r["ticker_raw"] for r in rows}


def test_a_core_funds_file_that_lost_its_type_column_is_a_source_error_not_the_other_shape():
    """IVV, IJH and IJR are read as the typed shape, so a file of theirs without ``Type`` fails loudly: read the
    other way, a swap or a warrant filed under Asset Class Equity would become a holdings line and could vote."""
    with pytest.raises(base.SourceError, match="Type"):
        funds.parse_ishares(fx.ishares_csv(fx.SP500, typed=False))


def test_a_sector_funds_file_that_gained_a_type_column_is_a_source_error_too():
    """The guard runs both ways: the untyped shape needs ``Weight (%)``, which the typed file does not have."""
    with pytest.raises(base.SourceError, match=r"Weight \(%\)"):
        funds.parse_ishares(fx.ishares_csv(fx.SP500), typed=False)


def test_the_ishares_file_is_the_product_pages_latest_holdings_csv():
    """The old ``1467271812596.ajax?fileType=csv`` link answers the HTML product page since Oct 2026."""
    url = next(f for f in funds.FILES if f.fund == "IVV").url()
    assert url == "https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf/latest-holdings.csv"


def test_the_vanguard_file_is_the_daily_creation_basket():
    """The old ``profile/api/<ticker>/portfolio-holding/stock`` answers 404 since Oct 2026."""
    urls = {f.fund: f.url() for f in funds.FILES if f.provider == "vanguard"}
    assert urls == {"VOO": "https://etfportal.vanguard.com/extrapi/v2/us/standard/01S990737/CRRD/holdings.json",
                    "IVOO": "https://etfportal.vanguard.com/extrapi/v2/us/standard/00S991588/CRRD/holdings.json",
                    "VIOO": "https://etfportal.vanguard.com/extrapi/v2/us/standard/00S991612/CRRD/holdings.json"}


def test_vanguard_and_nasdaq():
    rows = funds.parse_vanguard(fx.vanguard_json(fx.SP500, cusip_only=("FDX",)))
    assert len(rows) == 5 and rows[2] == {"ticker_raw": "", "cusip": "31428X106", "name": "FEDEX CORP", "weight": 1.0}
    assert [r["ticker_raw"] for r in funds.parse_nasdaq(fx.nasdaq_json(["MSFT", "NVDA"]))] == ["MSFT", "NVDA"]


@pytest.mark.parametrize("parse, body", [(funds.parse_ssga, None), (funds.parse_ishares, b"Name,Weight\n"),
                                         (funds.parse_vanguard, b'{"fund": {"entity": []}}'), (funds.parse_nasdaq, b"{}")])
def test_a_file_of_another_shape_is_a_source_error(parse, body):
    if body is None:
        from openpyxl import Workbook
        import io
        wb, buf = Workbook(), io.BytesIO()
        wb.active.append(["nothing here"])
        wb.save(buf)
        body = buf.getvalue()
    with pytest.raises(base.SourceError):
        parse(body)


def test_invesco_keeps_stock_and_adr_lines_and_reads_the_cusip():
    rows = funds.parse_invesco(fx.invesco_json([("MSFT", "594918104", "MICROSOFT CORP")], adr=True))
    assert rows == [{"ticker_raw": "MSFT", "cusip": "594918104", "name": "MICROSOFT CORP", "weight": 1.0},
                    {"ticker_raw": "PDD", "cusip": "722304102", "name": "PDD Holdings Inc", "weight": 1.0},
                    {"ticker_raw": "ASML", "cusip": "N07059210", "name": "ASML Holding NV", "weight": 1.0}]
    with pytest.raises(base.SourceError):
        funds.parse_invesco(b'{"cusip": "QQQ"}')


def test_ndx_is_voted_by_the_nasdaq_list_and_invescos_qqq():
    """The Nasdaq list API times out on homesrv (28 Oct 2026); QQQ tracks the Nasdaq-100 and answers the same 101."""
    ndx = {(f.provider, f.fund, f.url()) for f in funds.FILES if f.index_slug == "ndx"}
    assert ndx == {("nasdaq", "NDX", "https://api.nasdaq.com/api/quote/list-type/nasdaq100"),
                   ("invesco", "QQQ", "https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/QQQ/holdings/"
                                      "fund?idType=ticker&productType=ETF")}
    assert caps.cap_for("invesco") == 50


def test_tickers_are_written_the_sec_way():
    assert funds.norm_ticker("BRK.B") == "BRK-B" and funds.norm_ticker("BRK/B") == "BRK-B"
    assert funds.norm_ticker("CASH_USD") is None and funds.norm_ticker("-") is None


def test_one_request_per_file_a_failed_file_is_an_issue_and_the_others_count(env):
    row = base.run_request({"dataset": funds.NAME, "params": {"as_of": "2026-10-27"}})
    assert row["result"] == "changed" and row["calls"] == 11            # the three SmallCap files 404: no retry
    n, issues = ops.open_issues(funds.NAME)
    assert n == 3 and {i["key"] for i in issues} == {"ssga-SPSM.xlsx", "ishares-IJR.csv", "vanguard-VIOO.json"}
    assert row["coverage"] == round(8 / 11, 4) and "failed files" in row["note"]
    assert row["calls"] == len(env.calls())


def test_every_file_failing_fails_the_run(env, http_mock):
    http_mock.routes[:] = [r for r in http_mock.routes if "company_tickers" in r[0]]
    row = base.run_request({"dataset": funds.NAME, "params": {"as_of": "2026-10-27"}})
    assert row["result"] == "failed" and "every fund file failed" in row["note"]


def test_a_day_already_fetched_is_not_fetched_again(env):
    base.run_request({"dataset": funds.NAME, "params": {"as_of": "2026-10-27"}})
    env.requests.clear()
    base.run_request({"dataset": funds.NAME, "params": {"as_of": "2026-10-27"}})
    assert {str(r.url).split("/")[2] for r in env.requests} <= {"www.ssga.com", "www.ishares.com",
                                                                 "etfportal.vanguard.com"}
    assert len(env.requests) == 3                          # only the three that failed are asked again
