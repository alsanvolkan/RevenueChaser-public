"""News headlines from the IBKR Gateway (avo, 9 Nov 20:54 Amsterdam: "Ibkr kullanalim"): Dow Jones headlines per
ticker through ``reqHistoricalNews``, against a fake gateway. The client asks for contract ids and news only: never an
account, a position or an order.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.core.providers import ibkr_tws as ibkr
from RevenueChaser.module_news import headlines
from RevenueChaser.rcdata.tests.ibkr_fixture import ACCOUNT_PLACEHOLDER, FakeGateway, frame

NOW = datetime(2026, 11, 9, 19, 0, tzinfo=timezone.utc).timestamp()
ALLOWED = {"71", "9", "86"}          # startApi, reqContractDetails, reqHistoricalNews


class NewsGateway(FakeGateway):
    def __init__(self):
        super().__init__(last_bar=date(2026, 11, 6))
        self.con_ids = {"AAPL": 265598, "NVDA": 4815747, "CRWV": 7001}
        self.news: dict[int, list[tuple]] = {}
        self.unknown: set[str] = set()

    def _answer(self, fields):
        msg = fields[0]
        if msg == "9":                                    # reqContractDetails: [9, 8, reqId, conId, symbol, ...]
            req_id, symbol = fields[2], fields[4]
            if symbol in self.unknown or symbol not in self.con_ids:
                self._out += frame(4, 2, req_id, 200, "No security definition has been found", "")
                return
            self._out += frame(10, req_id, symbol, "STK", "", 0, "", "SMART", "USD", symbol, "NMS", symbol,
                               self.con_ids[symbol], 0.01, "", "ACTIVETIM", "SMART,NASDAQ")
            self._out += frame(52, 1, req_id)
        elif msg == "86":                                 # [86, reqId, conId, providers, start, end, total, options]
            req_id, con_id = fields[1], int(fields[2])
            for row in self.news.get(con_id, []):
                self._out += frame(86, req_id, *row)
            self._out += frame(87, req_id, 0)
        else:
            super()._answer(fields)


@pytest.fixture
def gw(monkeypatch):
    g = NewsGateway()
    monkeypatch.setattr(ibkr, "CONNECT", g.connect)
    g.news[265598] = [("2026-11-09 18:20:00.0", "DJ-N", "DJ-N$1a2b", "{A:800015:L:en:K:n/a:C:0.97}!Apple Expands AI Deal"),
                      ("2026-11-09 17:00:00.0", "DJ-RT", "DJ-RT$9", "Tech Stocks Rally as Yields Slip")]
    g.news[4815747] = [("2026-11-09 18:25:00.0", "DJ-N", "DJ-N$3c", "Nvidia, Apple Lead Gains"),
                       ("2026-11-09 17:00:00.0", "DJ-N", "DJ-N$dup", "Tech Stocks Rally as Yields Slip")]
    return g


def _rows():
    return {r["headline"]: r for r in rawstore.query("SELECT * FROM news_headlines", db="news")}


def test_the_client_asks_for_contract_ids_and_news_only(gw):
    with ibkr.TWSClient("127.0.0.1", 4001, client_id=headlines.CLIENT_ID) as c:
        cid = c.con_id(1, ibkr.contract_for("AAPL"))
        got = c.news(2, cid, "DJ-N+DJ-RT", "", "", 10)
        assert ACCOUNT_PLACEHOLDER not in repr(vars(c))
    assert cid == 265598 and len(got) == 2 and got[0]["article_id"] == "DJ-N$1a2b"
    assert set(gw.message_ids()) <= ALLOWED
    assert gw.sent[0] == ["71", "2", str(headlines.CLIENT_ID), ""]


def test_an_unknown_ticker_raises_a_request_error(gw):
    gw.unknown.add("ZZZZ")
    with ibkr.TWSClient("127.0.0.1", 4001) as c:
        with pytest.raises(ibkr.HistoryError) as e:
            c.con_id(1, ibkr.contract_for("ZZZZ"))
    assert e.value.code == 200


def test_fetch_stores_clean_headlines_with_their_tickers_once(gw):
    out = headlines.fetch(["AAPL", "NVDA"], now=NOW, run_id="r1")
    rows = _rows()
    assert "Apple Expands AI Deal" in rows                              # the {…}! metadata prefix is cut
    assert rows["Apple Expands AI Deal"]["published_at"] == datetime(2026, 11, 9, 18, 20,
                                                                     tzinfo=timezone.utc).timestamp()
    assert rows["Apple Expands AI Deal"]["provider"] == "ibkr" and rows["Apple Expands AI Deal"]["source"] == "Dow Jones"
    same = rows["Tech Stocks Rally as Yields Slip"]                     # one story, two tickers, two article ids
    assert sorted(json.loads(same["tickers"])) == ["AAPL", "NVDA"]
    assert out == {"tickers": 2, "stored": 3, "skipped": [], "error": None}


def test_a_name_the_gateway_does_not_know_is_skipped_not_fatal(gw):
    gw.unknown.add("CRWV")
    out = headlines.fetch(["CRWV", "AAPL"], now=NOW, run_id="r1")
    assert out["skipped"] == ["CRWV"] and out["stored"] == 2


def test_a_gateway_that_is_down_writes_nothing_and_says_so(gw):
    gw.down = True
    out = headlines.fetch(["AAPL"], now=NOW, run_id="r1")
    assert out["stored"] == 0 and "gateway" in out["error"] and _rows() == {}


def test_a_second_fetch_asks_only_since_the_last_one(gw):
    headlines.fetch(["AAPL"], now=NOW, run_id="r1")
    headlines.fetch(["AAPL"], now=NOW + 3600, run_id="r2")
    starts = [f[4] for f in gw.sent if f[0] == "86"]
    assert starts[0] == "2026-11-08 19:00:00.0" and starts[1] == "2026-11-09 18:50:00.0"   # 24 h, then mark - 10 min


def test_the_ticker_list_puts_the_watch_list_first_and_is_capped():
    names = headlines.tickers({"OKLO", "AAPL", "ZZZ"})
    assert names[:3] == ["AAPL", "OKLO", "ZZZ"] and len(names) <= headlines.MAX_TICKERS
    assert "NVDA" in names and len(set(names)) == len(names)
