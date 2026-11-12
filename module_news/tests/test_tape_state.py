"""The news listener's own record and the words the Settings card shows from it.

#727; avo, 12 Nov 2026: "Evet bir kart ekleyelim". BRFG and DJNL were refused with IBKR 10197 for a day and only the
journal said so. The listener now writes what it knows; nothing here opens a socket.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx

from RevenueChaser.core.providers import ibkr_tws as ibkr
from RevenueChaser.module_news import tape, tape_state
from RevenueChaser.module_news.tests.test_tape import FakeClient

T0 = datetime(2026, 11, 11, 9, 36, 54, tzinfo=timezone.utc).timestamp()


def _at(seconds: float) -> datetime:
    return datetime.fromtimestamp(T0 + seconds, timezone.utc)


def _rec(tmp_path, t):
    return tape_state.Recorder(path=tmp_path / "state.json", clock=lambda: t[0])


def _view(tmp_path, seconds, names=("BRFG", "DJNL")):
    return tape_state.collect(names=list(names), now=_at(seconds), path=tmp_path / "state.json")


def test_no_record_says_so_and_never_reads_as_a_refusal(tmp_path):
    v = _view(tmp_path, 0)
    assert v["state"] == "unknown" and v["feeds"] == [] and "No record yet" in v["written_text"]
    (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
    assert _view(tmp_path, 0)["state"] == "unknown"


def test_a_refused_feed_carries_the_code_the_words_the_streak_and_the_next_ask(tmp_path):
    t = [T0]
    rec = _rec(tmp_path, t)
    rec.connected("127.0.0.1:4001")
    rec.asked("BRFG"), rec.asked("DJNL")
    t[0] = T0 + 174
    rec.refused("DJNL", 10197, "No market data during competing live session", t[0] + 1800)
    t[0] = T0 + 174 + 1800
    rec.asked("DJNL")
    t[0] += 1
    rec.refused("DJNL", 10197, "No market data during competing live session", t[0] + 1800)
    djnl = [f for f in _view(tmp_path, 174 + 1801 + 30)["feeds"] if f["feed"] == "DJNL"][0]
    assert djnl["state"] == "refused" and djnl["code"] == 10197
    assert djnl["text"] == "Refused - IBKR error 10197: No market data during competing live session"
    assert djnl["since"].startswith("2026-11-11 09:39 UTC"), "the streak's start, not the last refusal"
    assert "Refused 2 times in a row." in djnl["detail"] and "Asked again at 10:39 UTC." in djnl["detail"]
    v = _view(tmp_path, 174 + 1801 + 30)
    assert "another session of this IBKR user" in v["hint"], "10197 is explained in plain words, once"
    assert "another session" not in djnl["detail"]


def test_an_ask_the_gateway_did_not_refuse_reads_subscribed_and_a_headline_makes_it_live(tmp_path):
    t = [T0]
    rec = _rec(tmp_path, t)
    rec.connected("127.0.0.1:4001")
    rec.asked("BRFG")
    assert _view(tmp_path, 120, ["BRFG"])["feeds"][0]["state"] == "subscribed"
    t[0] = T0 + 200
    rec.headline("BRFG")
    rec.beat()
    brfg = _view(tmp_path, 260, ["BRFG"])["feeds"][0]
    assert brfg["state"] == "live" and brfg["last_headline"] == "Last headline 1 minute ago"
    whole = _view(tmp_path, 260, ["BRFG"])
    assert whole["state"] == "live" and whole["hint"] == ""


def test_a_feed_that_comes_back_ends_its_streak(tmp_path):
    t = [T0]
    rec = _rec(tmp_path, t)
    rec.connected("127.0.0.1:4001")
    rec.asked("DJNL")
    rec.refused("DJNL", 10197, "No market data during competing live session", T0 + 1800)
    t[0] = T0 + 1800
    rec.asked("DJNL")                                   # the competing session has ended: no refusal this time
    assert _view(tmp_path, 1800 + 30, ["DJNL"])["feeds"][0]["state"] == "refused", "too soon to say"
    v = _view(tmp_path, 1800 + 90, ["DJNL"])["feeds"][0]
    assert v["state"] == "subscribed" and v["detail"] == ""
    t[0] = T0 + 3600
    rec.asked("DJNL")
    stored = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["feeds"]["DJNL"]
    assert stored["refusals"] == 0 and stored["refused_since"] is None


def test_a_gateway_that_is_away_makes_every_feed_wait_and_a_silent_listener_reads_stale(tmp_path):
    t = [T0]
    rec = _rec(tmp_path, t)
    rec.connected("127.0.0.1:4001")
    rec.asked("BRFG")
    t[0] = T0 + 60
    rec.down("127.0.0.1:4001", "connection refused")
    v = _view(tmp_path, 120)
    assert v["state"] == "waiting" and {f["state"] for f in v["feeds"]} == {"waiting"}
    assert v["gateway"].startswith("Not connected to 127.0.0.1:4001 since 2026-11-11 09:37 UTC")
    assert "connection refused" in v["gateway"] and not v["stale"]
    late = _view(tmp_path, 60 + 16 * 60)
    assert late["stale"] and "probably not running" in late["written_text"]


def test_a_restart_keeps_the_streak_and_a_feed_no_longer_asked_for_is_forgotten(tmp_path):
    t = [T0]
    rec = _rec(tmp_path, t)
    rec.asked("DJNL"), rec.asked("BRFUPDN")
    rec.refused("DJNL", 10197, "No market data during competing live session", T0 + 1800)
    again = _rec(tmp_path, [T0 + 600])
    again.keep(["BRFG", "DJNL"])
    again.connected("127.0.0.1:4001")
    stored = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["feeds"]
    assert set(stored) == {"DJNL"} and stored["DJNL"]["refusals"] == 1


def test_an_unwritable_record_never_stops_the_tape(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    rec = tape_state.Recorder(path=blocker / "state.json", clock=lambda: T0)
    rec.connected("127.0.0.1:4001"), rec.asked("BRFG"), rec.beat()        # no exception


def test_the_listener_tells_the_recorder_every_ask_refusal_and_headline(tmp_path):
    e = ibkr.HistoryError(10197, "No market data during competing live session")
    e.req_id = 2
    client = FakeClient([e, {"req_id": 1, "time_ms": "1794322800000", "provider": "BRFG", "article_id": "a",
                             "headline": "Stocks open higher"}, None])
    rec = _rec(tmp_path, [T0])
    rec.connected("127.0.0.1:4001")
    tape.listen(client, ["BRFG", "DJNL"], clock=lambda: T0, post=lambda *a, **k: httpx.Response(202),
                until=lambda: not client.script, rec=rec)
    rec.beat()
    v = {f["feed"]: f for f in _view(tmp_path, 90)["feeds"]}
    assert v["BRFG"]["state"] == "live" and v["DJNL"]["state"] == "refused" and v["DJNL"]["code"] == 10197
    assert _view(tmp_path, 90)["state"] == "refused", "one refused feed is what the card leads with"
    assert _view(tmp_path, 90)["lead"] == "1 of 2 feeds refused: DJNL"


def test_the_gateway_line_is_always_text_whatever_the_caller_hands_over(tmp_path):
    # The record is written inside an OSError guard only; a value json cannot write would stop the listener.
    rec = _rec(tmp_path, [T0])
    rec.down("127.0.0.1:4001", ConnectionRefusedError("refused"))
    assert tape_state.read(path=rec.path)["gateway"]["detail"] == "refused"
