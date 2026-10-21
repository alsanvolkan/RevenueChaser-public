"""SPDR Select Sector holdings (SSGA daily xlsx): the sector *check* adapter. It never sets a value.

The fund's sector is stored under UW's Morningstar sector names (#265), the same 11 names the master uses."""
from __future__ import annotations

import io

import httpx
import openpyxl

from RevenueChaser.core.identity import gics
from RevenueChaser.core.identity.sources import spdr

HEADER = ["Name", "Ticker", "Identifier", "SEDOL", "Weight", "Sector", "Shares Held", "Local Currency"]


def xlsx(rows, header=HEADER) -> bytes:
    """A workbook shaped like the SSGA file: 4 preamble rows, the header on row 5, a footer text row."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Fund Name:", "Select Sector SPDR Fund"])
    ws.append(["Ticker Symbol:", "XLK"])
    ws.append(["Holdings:", "As of 29-Sep-2026"])
    ws.append([])
    ws.append(header)
    for r in rows:
        ws.append(list(r))
    ws.append([])
    ws.append(["Past performance is not a reliable indicator of future performance."])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def hold(ticker, name=None, weight=1.0):
    return [name or f"{ticker} Inc", ticker, "123456789", "2046251", weight, "-", 100, "USD"]


def route_all(http, per_fund=None):
    """Route every fund file; ``per_fund`` maps a fund to its rows (a default filler row otherwise)."""
    per_fund = per_fund or {}
    for fund in spdr.FUNDS:
        rows = per_fund.get(fund, [hold(f"F{fund.upper()}")])
        http.add(f"holdings-daily-us-en-{fund}.xlsx", xlsx(rows))


# ── parse ─────────────────────────────────────────────────────────────────────────────────────
def test_parse_keeps_holdings_and_drops_cash_futures_footer_and_vylr():
    content = xlsx([
        hold("AAPL", "Apple Inc.", 14.5),
        hold("BRK.B", "Berkshire Hathaway Inc. Class B", 0.7),
        ["E-MINI S&P FUTURE DEC 26", "IXAZ6", None, None, 0.2, None, 5, "USD"],      # 5 chars: the regex alone keeps it
        ["US DOLLAR", "CASH_USD", None, None, 0.1, None, None, "USD"],
        ["Blank ticker", None, None, None, 0.1, None, None, "USD"],
        ["Dash ticker", "-", None, None, 0.1, None, None, "USD"],
        ["Vylr Corp", "VYLR", None, None, 0.1, None, None, "USD"],
    ])
    rows = spdr.parse(content, "xlk")
    assert [r["ticker"] for r in rows] == ["AAPL", "BRK-B"], "cash, futures, blank, VYLR and the footer are dropped"
    aapl = rows[0]
    assert aapl == {"ticker": "AAPL", "name": "Apple Inc.", "fund": "XLK", "sector": "Technology",
                    "weight": 14.5}
    assert rows[1]["name"].startswith("Berkshire") and rows[1]["weight"] == 0.7


def test_parse_futures_are_dropped_by_name_even_when_the_code_looks_like_a_ticker():
    rows = spdr.parse(xlsx([hold("AAPL"), ["e-mini future", "ESZ6", None, None, 1.0, None, 1, "USD"]]), "xlk")
    assert [r["ticker"] for r in rows] == ["AAPL"]


def test_parse_normalises_the_ticker_cell_and_a_missing_weight_is_none():
    rows = spdr.parse(xlsx([["Apple", " aapl ", None, None, None, None, None, "USD"]]), "xlk")
    assert rows == [{"ticker": "AAPL", "name": "Apple", "fund": "XLK", "sector": "Technology",
                     "weight": None}]


def test_every_fund_maps_to_one_morningstar_sector():
    assert list(spdr.FUNDS) == ["xlb", "xlc", "xle", "xlf", "xli", "xlk", "xlp", "xlre", "xlu", "xlv", "xly"]
    assert spdr.FUNDS == {"xlb": "Basic Materials", "xlc": "Communication Services", "xle": "Energy",
                          "xlf": "Financial Services", "xli": "Industrials", "xlk": "Technology",
                          "xlp": "Consumer Defensive", "xlre": "Real Estate", "xlu": "Utilities",
                          "xlv": "Healthcare", "xly": "Consumer Cyclical"}
    assert set(spdr.FUNDS.values()) == set(gics.MORNINGSTAR_SECTORS)


def test_parse_without_the_ticker_or_name_column_raises():
    import pytest
    with pytest.raises(ValueError):
        spdr.parse(xlsx([["x", 1]], header=["Name", "Identifier"]), "xlk")
    with pytest.raises(ValueError):
        spdr.parse(xlsx([["AAPL", 1]], header=["Symbol", "Ticker Name"]), "xlk")


# ── holdings ──────────────────────────────────────────────────────────────────────────────────
def test_holdings_all_funds_ok_dedupes_and_the_first_fund_wins(http):
    route_all(http, {"xlb": [hold("LIN", "Linde")], "xlk": [hold("AAPL", "Apple"), hold("LIN", "Linde (dup)")],
                     "xly": [hold("AMZN", "Amazon")]})
    res = spdr.holdings()
    assert res.source == "spdr" and res.status == "ok" and res.error is None
    by = {r["ticker"]: r for r in res.rows}
    assert by["LIN"]["fund"] == "XLB" and by["LIN"]["sector"] == "Basic Materials", "xlb comes before xlk: first fund wins"
    assert by["AAPL"]["sector"] == "Technology" and by["AMZN"]["sector"] == "Consumer Cyclical"
    assert len([c for c in http.calls if "holdings-daily-us-en-" in c["url"]]) == 11
    assert len(res.rows) == len(by), "one row per ticker"


def test_holdings_fetches_the_eleven_funds_in_order_from_the_default_ssga_base(http):
    route_all(http)
    spdr.holdings()
    urls = [c["url"] for c in http.calls]
    assert urls[0] == ("https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/"
                       "holdings-daily-us-en-xlb.xlsx")
    assert [u.rsplit("-", 1)[1] for u in urls] == [f"{f}.xlsx" for f in spdr.FUNDS]


def test_a_non_200_fund_makes_the_whole_source_an_error_with_no_rows(http):
    route_all(http)
    http.routes.insert(0, ("holdings-daily-us-en-xle.xlsx", {}, 500, {}))
    res = spdr.holdings()
    assert res.status == "error" and res.rows == []
    assert "SPDR xle: HTTP 500" in res.error


def test_a_fund_that_is_not_an_xlsx_is_an_error(http):
    route_all(http)
    http.routes.insert(0, ("holdings-daily-us-en-xlv.xlsx", b"<html>blocked</html>", 200, {}))
    res = spdr.holdings()
    assert res.status == "error" and res.rows == [] and "xlv" in res.error


def test_a_fund_with_zero_holdings_rows_is_an_error(http):
    route_all(http)
    http.routes.insert(0, ("holdings-daily-us-en-xlu.xlsx",
                           xlsx([["US DOLLAR", "CASH_USD", None, None, 1.0, None, None, "USD"]]), 200, {}))
    res = spdr.holdings()
    assert res.status == "error" and res.rows == [] and "xlu" in res.error and "no holdings" in res.error


def test_a_network_exception_is_an_error_not_a_raise(http):
    http.add("ssga.com", httpx.ReadTimeout("slow"))
    res = spdr.holdings()
    assert res.status == "error" and res.rows == []


def test_env_override_redirects_the_url(http, monkeypatch):
    route_all(http)
    monkeypatch.setenv("RC_SPDR_BASE", "http://127.0.0.1:9/spdr/")
    spdr.holdings()
    assert http.calls[0]["url"] == "http://127.0.0.1:9/spdr/holdings-daily-us-en-xlb.xlsx"
