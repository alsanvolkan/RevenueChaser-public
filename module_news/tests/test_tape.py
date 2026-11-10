"""The live listener (#673): broad-tape headlines from the IBKR Gateway into a JSON-lines tape, the 5-minute trigger
of the dashboard job, and the job's load of the tape into ``boards/news.duckdb``. No real socket, no real dashboard."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import httpx
import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.core.providers import ibkr_tws as ibkr
from RevenueChaser.module_news import headlines, tape
from RevenueChaser.rcdata.tests.ibkr_fixture import FakeGateway, frame

NOW = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc).timestamp()


class TapeGateway(FakeGateway):
    """Answers ``reqMktData`` on a news contract with its queued ticks, or refuses a feed the account lacks."""

    def __init__(self):
        super().__init__(last_bar=date(2026, 11, 6))
        self.ticks: dict[str, list[tuple]] = {}
        self.refused: set[str] = set()

    def _answer(self, fields):
        if fields[0] == "1":                   # [1, 11, reqId, conId, symbol, secType, ..., exchange, ...]
            req_id, symbol = fields[2], fields[4]
            if symbol in self.refused:
                self._out += frame(4, 2, req_id, 10197, "No market data during competing live session", "")
                return
            for t in self.ticks.get(symbol, []):
                self._out += frame(84, req_id, *t)
        else:
            super()._answer(fields)


def test_the_client_subscribes_to_a_news_contract_and_reads_its_headlines(monkeypatch):
    gw = TapeGateway()
    monkeypatch.setattr(ibkr, "CONNECT", gw.connect)
    gw.ticks["BRFG:BRFG_ALL"] = [("1794322800000", "BRFG", "BRFG$1", "Stocks open higher", "")]
    with ibkr.TWSClient("127.0.0.1", 4001, client_id=tape.CLIENT_ID) as c:
        c.subscribe_news(1, ibkr.news_contract("brfg"))
        got = c.next_news() or c.next_news()          # the farm notice after startApi reads as None
    sub = [f for f in gw.sent if f[0] == "1"][0]
    assert sub[4:6] == ["BRFG:BRFG_ALL", "NEWS"] and sub[10] == "BRFG" and "mdoff,292" in sub
    assert got == {"req_id": 1, "time_ms": "1794322800000", "provider": "BRFG", "article_id": "BRFG$1",
                   "headline": "Stocks open higher"}
    assert set(gw.message_ids()) <= {"71", "1"}           # never an account, a position or an order


def test_a_refused_feed_raises_with_its_request_id(monkeypatch):
    gw = TapeGateway()
    monkeypatch.setattr(ibkr, "CONNECT", gw.connect)
    gw.refused.add("DJNL:DJNL_ALL")
    with ibkr.TWSClient("127.0.0.1", 4001, client_id=tape.CLIENT_ID) as c:
        c.subscribe_news(3, ibkr.news_contract("DJNL"))
        with pytest.raises(ibkr.HistoryError) as e:
            c.next_news(), c.next_news()              # the farm notice first, then the refusal
    assert e.value.req_id == 3 and e.value.code == 10197


class FakeClient:
    """``subscribe_news`` / ``next_news`` from a script: an item, an error, or None (a quiet read)."""

    def __init__(self, script):
        self.script, self.subs = list(script), []

    def subscribe_news(self, req_id, contract):
        self.subs.append((req_id, contract.symbol))

    def cancel_news(self, req_id):
        self.subs.append((req_id, "cancel"))

    def next_news(self):
        step = self.script.pop(0) if self.script else None
        if isinstance(step, Exception):
            raise step
        return step


def _refused(req_id):
    e = ibkr.HistoryError(10197, "not subscribed")
    e.req_id = req_id
    return e


def test_listen_appends_each_headline_and_drops_a_refused_feed():
    script = [_refused(2), {"req_id": 1, "time_ms": "1794322800000", "provider": "BRFG", "article_id": "a",
                            "headline": "Stocks open higher"},
              {"req_id": 2, "time_ms": "1794322801000", "provider": "X", "article_id": "b", "headline": "late"}, None]
    client, posts = FakeClient(script), []
    tape.listen(client, ["BRFG", "BRFUPDN"], clock=lambda: NOW,
                post=lambda *a, **k: posts.append(a) or httpx.Response(202), until=lambda: not client.script)
    lines = [json.loads(x) for x in tape.day_file(NOW).read_text(encoding="utf-8").splitlines()]
    assert [x["headline"] for x in lines] == ["Stocks open higher"]       # the refused feed's tick is not kept
    assert lines[0]["feed"] == "BRFG"
    assert client.subs == [(1, "BRFG:BRFG_ALL"), (2, "BRFUPDN:BRFUPDN_ALL"), (2, "cancel")]
    assert len(posts) == 1 and posts[0][0].endswith("/run/news_headlines")


def test_the_trigger_fires_every_five_minutes_and_survives_a_down_dashboard():
    t = [NOW]
    client = FakeClient([None] * 4)

    def clock():
        t[0] += 160.0                       # every read moves the clock on
        return t[0]

    calls = []

    def post(*a, **k):
        calls.append(t[0])
        raise httpx.ConnectError("refused")

    tape.listen(client, [], clock=clock, post=post, until=lambda: not client.script)
    assert len(calls) == 2 and calls[1] - calls[0] >= tape.TRIGGER_S


def test_the_feed_list_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("RC_NEWS_TAPE_FEEDS", "djnl, brfg ,DJNL")
    assert tape.feeds() == ["DJNL", "BRFG"]
    monkeypatch.delenv("RC_NEWS_TAPE_FEEDS")
    assert tape.feeds() == list(tape.DEFAULT_FEEDS)


def _tape(lines, now=NOW, tail=""):
    p = tape.day_file(now)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write("".join(json.dumps(x) + "\n" for x in lines) + tail)


def _line(text, at=NOW - 60, provider="BRFG"):
    return {"received_at": at, "feed": provider, "time_ms": str(int(at * 1000)), "provider": provider,
            "article_id": "x", "headline": text}


def _heads():
    return {r["headline"]: r for r in rawstore.query("SELECT * FROM news_headlines", db="news")}


def test_load_tape_stores_market_headlines_once_and_moves_the_mark():
    _tape([_line("Fed holds rates"), _line("Fed holds rates"), _line("Oil slips", provider="DJ-N")])
    assert headlines.load_tape(now=NOW, run_id="r1") == {"read": 3, "stored": 2}
    rows = _heads()
    assert json.loads(rows["Fed holds rates"]["tickers"]) == [] and rows["Fed holds rates"]["source"] == "BRFG"
    assert rows["Oil slips"]["source"] == "Dow Jones" and rows["Oil slips"]["published_at"] == pytest.approx(NOW - 60)
    assert headlines.load_tape(now=NOW + 300, run_id="r2") == {"read": 0, "stored": 0}   # nothing new


def test_load_tape_leaves_a_half_written_line_for_the_next_pass():
    _tape([_line("First")], tail='{"headline": "Sec')
    assert headlines.load_tape(now=NOW)["read"] == 1
    with open(tape.day_file(NOW), "a", encoding="utf-8") as f:
        f.write('ond", "time_ms": "%d", "provider": "BRFG"}\n' % int(NOW * 1000))
    assert headlines.load_tape(now=NOW + 300) == {"read": 1, "stored": 1} and "Second" in _heads()


def test_load_tape_reads_into_the_next_day_and_keeps_a_ticker_already_stored():
    rawstore.write_rows("news_headlines", [{
        "headline_id": headlines.story_id("Apple beats"), "provider": "ibkr", "published_at": NOW - 120,
        "headline": "Apple beats", "source": "Dow Jones", "tickers": json.dumps(["AAPL"]), "url": None,
        "fetched_at": NOW, "run_id": "s", "run_at": NOW}])
    _tape([_line("Apple beats")])
    headlines.load_tape(now=NOW)
    tomorrow = NOW + 86400
    _tape([_line("Asia opens", at=tomorrow)], now=tomorrow)
    assert headlines.load_tape(now=tomorrow)["stored"] == 1
    rows = _heads()
    assert json.loads(rows["Apple beats"]["tickers"]) == ["AAPL"] and "Asia opens" in rows


def test_a_refused_feed_is_asked_again_and_an_unknown_request_error_is_ignored():
    t = [NOW]
    client = FakeClient([_refused(1), _refused(99), None, None])

    def clock():
        return t[0]

    def until():
        t[0] += tape.RESUB_S / 2                 # each loop moves half the wait on
        return not client.script

    tape.listen(client, ["BRFG"], clock=clock, post=lambda *a, **k: httpx.Response(202), until=until)
    # cancelled, then asked again under a new id, once
    assert client.subs == [(1, "BRFG:BRFG_ALL"), (1, "cancel"), (2, "BRFG:BRFG_ALL")]


def test_the_five_minute_schedule_carries_over_a_reconnect():
    state, posts = {}, []
    post = lambda *a, **k: posts.append(1) or httpx.Response(202)
    for _ in range(3):                          # three short connections inside one five-minute window
        client = FakeClient([None])
        tape.listen(client, [], clock=lambda: NOW, post=post, until=lambda: not client.script, state=state)
    assert len(posts) == 1


def test_data_lost_ends_the_connection_so_the_listener_resubscribes(monkeypatch):
    gw = TapeGateway()
    monkeypatch.setattr(ibkr, "CONNECT", gw.connect)
    gw.ticks["BRFG:BRFG_ALL"] = []
    with ibkr.TWSClient("127.0.0.1", 4001, client_id=tape.CLIENT_ID) as c:
        c.subscribe_news(1, ibkr.news_contract("BRFG"))
        gw._out += frame(4, 2, -1, 1101, "Connectivity between IB and TWS has been restored- data lost.", "")
        with pytest.raises(ibkr.TWSError) as e:
            c.next_news(), c.next_news()
    assert not isinstance(e.value, ibkr.HistoryError) and "1101" in str(e.value)
