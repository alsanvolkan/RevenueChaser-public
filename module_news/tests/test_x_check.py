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


def _bar(at, close, mt="r"):
    return {"start_time": _iso(at), "close": str(close), "market_time": mt}


class FakeUW:
    def __init__(self, prints, chains=None, fail=False, bars=None):
        self.prints, self.chains, self.fail, self.calls, self.closed = prints, chains or [], fail, [], False
        self.bars = bars or []

    def intraday_bars_result(self, ticker, day, candle_size="5m"):
        self.calls.append(("bars", ticker, day))
        if self.fail:
            return ProviderResult("failed", [], "HTTP 500", "bars")
        return ProviderResult("ok", self.bars, None, "bars")

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
    _store(headline_id="x:2120263144924692750", kind="news", option="")              # not an option: price only
    uw = FakeUW([_print(TWEET - 120, 5000, 1.80)],
                [{"date": "2026-11-10", "open_interest": 1200}, {"date": "2026-11-11", "open_interest": 5300}],
                bars=[_bar(TWEET - 300, 712.4), _bar(TWEET, 713.0)])
    assert x_check.run(lambda: uw, now=TWEET + 600) == {"checked": 2, "calls": 2}     # one bars call for both rows
    assert uw.calls == [("flow", "SPY261120C00759000", "2026-11-10", x_check.MIN_PREMIUM),
                        ("bars", "SPY", "2026-11-10")] and uw.closed
    assert _state()["status"] == "seen" and _state().get("oi_change") is None
    assert _state("x:2120263144924692750")["status"] == "x_only"
    assert _state("x:2120263144924692750")["px"] == [[round(TWEET - 300), 712.4], [round(TWEET), 713.0]]
    assert x_check.run(lambda: uw, now=TWEET + 1200) == {"checked": 0, "calls": 0}      # within the hour: nothing
    next_day = TWEET + 20 * 3600
    assert x_check.run(lambda: uw, now=next_day) == {"checked": 2, "calls": 2}          # OI, and the closed session
    assert ("historic", "SPY261120C00759000") in uw.calls and _state()["oi_change"] == 4100
    assert x_check.run(lambda: pytest_fail(), now=next_day + 60) == {"checked": 0, "calls": 0}   # done: no client


def pytest_fail():
    raise AssertionError("no UW client should be built when nothing waits")


def test_a_failing_uw_is_asked_again_then_given_up():
    _store()
    uw = FakeUW([], fail=True)
    for _ in range(x_check.MAX_TRIES):
        x_check.run(lambda: uw, now=TWEET + 600)
    assert _state()["status"] == "failed" and sum(c[0] == "flow" for c in uw.calls) == x_check.MAX_TRIES
    assert _state()["px_tries"] == x_check.MAX_TRIES and x_check.run(lambda: pytest_fail(), now=TWEET + 900)["calls"] == 0
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


def test_every_x_card_has_a_badge_and_each_ticker_a_group_with_its_price_line():
    bars = [[int(TWEET) - 3600 + i * 300, 710.0 + i] for i in range(16)]      # a bar starts at the tweet's second
    seen = {"ticker": "SPY", "strike": 759.0, "right": "C", "expiry": "2026-11-20", "occ": "SPY261120C00759000",
            "session": "2026-11-10", "status": "seen", "trades": 1, "contracts": 5000, "premium": 900000.0,
            "ask": 5000, "bid": 0, "mid": 0, "first": TWEET - 120, "last": TWEET - 120,
            "pts": [[TWEET - 120, 5000, 900000, "ask"]], "px": bars, "px_tk": "SPY"}
    _store(x_check=json.dumps(seen))
    _store(headline_id="x:2120245787716354207", published_at=TWEET - 1500, kind="level", option="",
           headline="Nice $SPY rejection", x_check=json.dumps({"status": "x_only", "px": bars, "px_tk": "SPY"}))
    _store(headline_id="x:2120217953778208847", published_at=TWEET - 7800, kind="option", tickers='["EL"]',
           headline="$EL 150C 6/17exp $450k", option="", x_check=json.dumps({"status": "not_seen", "ticker": "EL"}))
    page = report.render_html(now=TWEET + 3600)
    assert page.count('class="xg"') == 2 and page.index('data-grp="SPY"') < page.index('data-grp="EL"')
    assert page.count('<figure class="xp">') == 1 and page.count('<i class="tw" style=') == 2   # both SPY tweets
    assert "UW teyitli" in page and "UW&#x27;de yok" in page and "X kimliği" in page
    assert all(f'class="ya" style="top:{t}px">{v}<' in page for t, v in (("8.0", "725,00"), ("48.0", "717,50"),
                                                                           ("88.0", "710,00")))   # price y axis
    assert '<span class="ya" style="top:3.0px">$900 bin</span>' in page                         # prints y axis
    assert '<div id="pg-x" role="tabpanel" hidden>' in page and 'aria-controls="pg-x"' in page   # own page tab
    assert page.count('<details class="xd"><summary>') == 2                     # both charts closed until clicked
    assert "1/1 UW teyitli" in page and "fiyat 722,00 → 725,00 (+0,42%)" in page
    assert x_check.badge(None)[2] == "X kimliği" and x_check.badge({"status": "pending"})[1] == "◷"


def test_a_pass_never_asks_uw_more_than_the_cap():
    # 21 calls on the first live backfill (11 Nov 2026): the cap was checked before a row, which can take 2-3 calls
    _store(headline_id="x:1", published_at=TWEET + 60, kind="level", option="", tickers='["QQQ"]')   # bars: 1 call
    for i, tk in enumerate("ABCDEFGHIJKL"):                                                   # flow + bars: 2 each
        _store(headline_id=f"x:o{i}", published_at=TWEET - i, tickers=f'["{tk}"]',
               headline=f"${tk} 50C 11/20exp $1M", option=f"{tk} 50C 11/20exp")
    uw = FakeUW([_print(TWEET - 120, 5000, 1.80)], bars=[_bar(TWEET, 50.0)])
    assert x_check.run(lambda: uw, now=TWEET + 600)["calls"] <= x_check.MAX_CALLS == 20
    assert len(uw.calls) <= x_check.MAX_CALLS


def test_the_pages_updated_time_is_its_newest_write():
    _store(run_at=TWEET + 1800, kind="news", option="")
    assert "Veri 21:54'de güncellendi" in report.render_html(now=TWEET + 3600)    # no calendar run: the headline's
