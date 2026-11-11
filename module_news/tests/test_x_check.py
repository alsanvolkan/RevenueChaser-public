"""The X lane's option tweets checked against UW's prints of the contract (avo, 11 Nov 2026: "we need to show that
option flow from our sources"; card choice "Canlı UW kontrat akışı")."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from RevenueChaser.core import rawstore
from RevenueChaser.core.providers.result import ProviderResult
from RevenueChaser.module_news import report, x_check, x_feed

TWEET = x_feed.status_time(2120251362529419593)          # @kiantrades, 10 Nov 2026 21:24 Amsterdam (15:24 New York)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _print(at, size, price, bid=1.70, ask=1.80, canceled=False):
    return {"executed_at": _iso(at), "size": size, "price": str(price), "nbbo_bid": str(bid), "nbbo_ask": str(ask),
            "premium": str(size * price * 100), "canceled": canceled}


class FakeUW:
    def __init__(self, prints, chains=None, fail=False):
        self.prints, self.chains, self.fail, self.calls, self.closed = prints, chains or [], fail, [], False

    def option_contract_flow_result(self, occ, date=None, min_premium=None):
        self.calls.append(("flow", occ, date, min_premium))
        if self.fail:
            return ProviderResult("failed", [], "HTTP 500", "flow")
        return ProviderResult("ok", self.prints, None, "flow")

    def option_contract_historic_result(self, occ):
        self.calls.append(("historic", occ))
        return ProviderResult("ok", self.chains, None, "historic")

    def close(self):
        self.closed = True


def test_a_tweets_contract_becomes_its_occ_symbol():
    c = x_check.contract("SPY 759C 11/20exp $1.8M premium yön belirsiz", TWEET)
    assert c == {"ticker": "SPY", "strike": 759.0, "right": "C", "expiry": "2026-11-20", "occ": "SPY261120C00759000"}
    assert x_check.contract("$SPX 6800P 1/16", TWEET)["occ"] == "SPXW270116P06800000"      # past date: next year
    assert x_check.contract("NVDA 192.5c 12/19/26", TWEET)["occ"] == "NVDA261219C00192500"
    assert x_check.contract("SPY calls lots of them", TWEET) is None                      # no strike, no expiry
    assert x_check.contract("SPY 759C", TWEET) is None


def test_prints_around_the_tweet_give_size_premium_and_side():
    prints = [_print(TWEET - 120, 5000, 1.80), _print(TWEET - 60, 1000, 1.70), _print(TWEET - 30, 200, 1.75),
              _print(TWEET - 5 * 3600, 9999, 1.80), _print(TWEET - 10, 50, 1.80, canceled=True)]
    s = x_check.prints_summary(prints, TWEET)
    assert (s["trades"], s["contracts"], s["ask"], s["bid"], s["mid"]) == (3, 6200, 5000, 1000, 200)
    assert s["premium"] == 5000 * 180 + 1000 * 170 + 200 * 175
    assert (s["first"], s["last"]) == (TWEET - 120, TWEET - 30)
    line = x_check.facts({**s, "status": "seen", "oi_change": 4100})
    assert line.startswith("UW teyidi: 3 işlem · 6.200 kontrat · $1,10M · %81 ask (alım) · 21:22–21:24")
    assert line.endswith("ertesi gün OI +4.100 (açılış)")


def test_open_interest_change_is_the_next_day_less_the_session():
    chains = [{"date": "2026-11-09", "open_interest": 100}, {"date": "2026-11-10", "open_interest": 1200},
              {"date": "2026-11-11", "open_interest": 5300}]
    assert x_check.oi_change(chains, "2026-11-10") == 4100
    assert x_check.oi_change(chains[:2], "2026-11-10") is None                          # next day not there yet


def _store(**over):
    row = {"headline_id": "x:2120251362529419593", "provider": "grok-x", "published_at": TWEET,
           "headline": "$SPY 759C 11/20exp $1.8M premium", "source": "@kiantrades", "tickers": '["SPY"]',
           "url": "https://x.com/kiantrades/status/2120251362529419593", "kind": "option",
           "option": "SPY 759C 11/20exp $1.8M premium yön belirsiz", "fetched_at": TWEET, "run_id": "r",
           "run_at": TWEET, **over}
    rawstore.write_rows("news_headlines", [row])
    return row


def _state(hid="x:2120251362529419593"):
    r = rawstore.query("SELECT x_check FROM news_headlines WHERE headline_id = ?", [hid], db="news")[0]
    return json.loads(r["x_check"]) if r["x_check"] else None


def test_run_checks_the_print_then_the_next_days_open_interest_and_then_stops():
    _store()
    _store(headline_id="x:2120263144924692750", kind="news", option="")              # not an option: never asked
    uw = FakeUW([_print(TWEET - 120, 5000, 1.80)],
                [{"date": "2026-11-10", "open_interest": 1200}, {"date": "2026-11-11", "open_interest": 5300}])
    assert x_check.run(lambda: uw, now=TWEET + 600) == {"checked": 1, "calls": 1}
    assert uw.calls == [("flow", "SPY261120C00759000", "2026-11-10", x_check.MIN_PREMIUM)] and uw.closed
    assert _state()["status"] == "seen" and _state()["oi_change"] is None
    assert x_check.run(lambda: uw, now=TWEET + 1200) == {"checked": 0, "calls": 0}      # same session: nothing to ask
    next_day = TWEET + 20 * 3600
    assert x_check.run(lambda: uw, now=next_day) == {"checked": 1, "calls": 1}
    assert uw.calls[-1] == ("historic", "SPY261120C00759000") and _state()["oi_change"] == 4100
    assert x_check.run(lambda: pytest_fail(), now=next_day + 60) == {"checked": 0, "calls": 0}   # done: no client


def pytest_fail():
    raise AssertionError("no UW client should be built when nothing waits")


def test_a_failing_uw_is_asked_again_then_given_up():
    _store()
    uw = FakeUW([], fail=True)
    for _ in range(x_check.MAX_TRIES):
        x_check.run(lambda: uw, now=TWEET + 600)
    assert _state()["status"] == "failed" and len(uw.calls) == x_check.MAX_TRIES
    assert x_check.facts(_state()) == "UW teyidi alınamadı"


def test_the_board_shows_the_check_and_one_card_for_a_print_two_accounts_tweeted(monkeypatch):
    seen = {"ticker": "SPY", "strike": 759.0, "right": "C", "expiry": "2026-11-20", "occ": "SPY261120C00759000",
            "session": "2026-11-10", "status": "seen", "trades": 3, "contracts": 6200, "premium": 1080000.0,
            "ask": 5000, "bid": 1000, "mid": 200, "first": TWEET - 120, "last": TWEET - 30}
    _store(x_check=json.dumps(seen))
    _store(headline_id="x:2120251362529419999", published_at=TWEET + 60, source="@CheddarFlow",
           x_check=json.dumps(seen))
    _store(headline_id="x:2120251362529410000", kind="other", headline="gm everyone", option="")
    page = report.render_html(now=TWEET + 3600)
    assert page.count('class="xo ok"') == 1 and "UW teyidi: 3 işlem · 6.200 kontrat" in page
    assert "@CheddarFlow, @kiantrades" in page and '<span class="tk">SPY 759C 20.11</span>' in page
    assert "gm everyone" not in page
    monkeypatch.setattr(report.time, "time", lambda: TWEET + 3600)
    polled = {r["headline_id"]: r for r in report.api_rows(0)}
    assert "x:2120251362529410000" not in polled
    assert polled["x:2120251362529419593"]["option"].startswith("UW teyidi:")
    assert polled["x:2120251362529419593"]["tickers"] == ["SPY 759C 20.11"]
