"""``alpaca/bars``: the key rules (New York session date, complete sessions only), the window read again, the
revisions, the resume, and the request Alpaca gets, with Alpaca mocked at the HTTP boundary."""
from __future__ import annotations

import gzip
import json
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.prices import bars, stage
from RevenueChaser.rcdata.build.sources import alpaca

from .prices_fixture import BarsAnswer, bar, write_spine

TUE = date(2026, 10, 27)
WINDOW = [date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 26), TUE]
ASKED = ["AAPL", "BRK.B", "FLG", "GAPX", "SPY"]


def _clock(monkeypatch, ny: datetime) -> None:
    monkeypatch.setattr(calendar, "_now", lambda: ny.astimezone(timezone.utc))


@pytest.fixture(autouse=True)
def alpaca_env(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "test-secret")
    monkeypatch.setenv("RC_ALPACA_BASE", "https://alpaca.test")
    _clock(monkeypatch, datetime(2026, 10, 27, 17, 0, tzinfo=calendar.NY))
    # The scope: the five spine names, the benchmarks and ETFs cut to SPY, no watch list.
    monkeypatch.setattr("RevenueChaser.rcdata.build.prices.ROTATION", ())
    monkeypatch.setattr("RevenueChaser.rcdata.build.prices.BENCHMARKS", ("SPY",))
    monkeypatch.setattr("RevenueChaser.rcdata.read.watched", lambda kind: set())
    write_spine()


def _series(days, close=100.0, step=1.0):
    return [bar(d, close + i * step) for i, d in enumerate(days)]


def _answer(extra_raw=None, all_closes=None, **kw):
    days = [date(2026, 10, 20)] + WINDOW
    raw = {s: _series(days) for s in ASKED}
    raw.update(extra_raw or {})
    every = {s: _series(days, close=50.0, step=0.5) for s in ASKED}
    every.update(all_closes or {})
    return BarsAnswer({"raw": raw, "all": every}, **kw)


def _run(ctx_id="r1", as_of=TUE):
    ctx = base.RunContext(run_id=ctx_id, dataset=bars.NAME, sleep=lambda s: None)
    out = bars.DATASET.to_parquet_all(ctx, bars.DATASET.fetch(ctx, as_of))
    return ctx, out


def _parquet(session: date) -> dict:
    return {r["symbol"]: r for r in stage.read_rows(bars.partition_path(session))}


def test_window_and_the_new_york_session_of_a_bar():
    assert bars.window(TUE) == WINDOW
    assert bars.session_of("2026-10-27T04:00:00Z") == TUE                  # midnight New York in EDT
    assert bars.session_of("2026-11-03T05:00:00Z") == date(2026, 11, 3)    # and in EST
    assert bars.session_of("2026-10-28T02:00:00Z") == TUE                  # the UTC date is the next day


def test_one_request_per_chunk_and_adjustment_with_the_key_headers(http_mock, monkeypatch):
    monkeypatch.setattr(bars, "CHUNK", 2)
    http_mock.add("/v2/stocks/bars", _answer())
    ctx, _ = _run()
    reqs = http_mock.calls("/v2/stocks/bars")
    assert len(reqs) == 6                                                 # 5 names in chunks of 2, twice
    q = [{k: v[0] for k, v in parse_qs(urlparse(str(r.url)).query).items()} for r in reqs]
    assert [x["adjustment"] for x in q] == ["raw"] * 3 + ["all"] * 3
    assert sorted(",".join(x["symbols"] for x in q[:3]).split(",")) == sorted(ASKED)
    assert {x["timeframe"] for x in q} == {"1Day"} and {x["feed"] for x in q} == {"sip"}
    assert q[0]["start"] == "2026-10-21T04:00:00Z" and q[3]["start"] == "2026-10-20T04:00:00Z"
    assert q[0]["end"] == "2026-10-27T20:44:00Z"                          # 16 minutes before now, not later
    assert reqs[0].headers["APCA-API-KEY-ID"] == "test-key-id" and "test-secret" not in str(reqs[0].url)
    assert ctx.calls["alpaca"][0] == 6


def test_no_key_fails_before_any_request(http_mock, monkeypatch):
    monkeypatch.delenv("ALPACA_API_SECRET_KEY")
    http_mock.add("/v2/stocks/bars", _answer())
    with pytest.raises(base.SourceError, match="ALPACA_API_SECRET_KEY") as e:
        _run()
    assert "ALPACA_API_KEY_ID" not in str(e.value) and http_mock.calls() == []


def test_a_session_not_complete_yet_is_not_fetched(http_mock, monkeypatch):
    _clock(monkeypatch, datetime(2026, 10, 27, 16, 10, tzinfo=calendar.NY))
    http_mock.add("/v2/stocks/bars", _answer())
    assert bars.DATASET.default_as_of() == date(2026, 10, 26)
    with pytest.raises(base.SourceError, match="not complete"):
        _run(as_of=TUE)
    assert http_mock.calls() == []


def test_bars_keyed_on_their_new_york_session_inside_the_window_only(http_mock):
    saturday, wednesday = date(2026, 10, 24), date(2026, 10, 28)
    extra = {"AAPL": _series([date(2026, 10, 20)] + WINDOW) + [bar(saturday, 999.0), bar(wednesday, 888.0)]}
    http_mock.add("/v2/stocks/bars", _answer(extra_raw=extra, unfiltered=True))
    ctx, out = _run()
    assert sorted(p.parent.name for p in out) == [f"date={d}" for d in WINDOW]
    assert not bars.partition_path(date(2026, 10, 20)).exists()            # the window's day before: all only
    assert not bars.partition_path(saturday).exists() and not bars.partition_path(wednesday).exists()
    row = _parquet(TUE)["AAPL"]
    assert row["session_date"] == TUE and row["close"] == 105.0 and row["ticker"] == "AAPL"
    assert row["security_id"] == "001690:01" and row["cik"] == "320193" and row["source_as_of"] == TUE
    assert _parquet(TUE)["BRK.B"]["ticker"] == "BRK-B"
    issues = [i for i in ops.open_issues(bars.NAME, limit=10)[1] if i["kind"] == "not_a_session"]
    assert [i["key"] for i in issues] == ["AAPL:2026-10-24"]
    assert any("outside the window" in n for n in ctx.notes)


def test_a_bar_not_complete_when_it_was_fetched_is_dropped(http_mock, monkeypatch):
    http_mock.add("/v2/stocks/bars", _answer())
    ctx = base.RunContext(run_id="r1", dataset=bars.NAME, sleep=lambda s: None)
    raws = bars.DATASET.fetch(ctx, TUE)
    for p in raws:                                     # as if the file had been written at 16:10 New York
        payload = json.loads(gzip.decompress(p.read_bytes()))
        payload["fetched_at"] = datetime(2026, 10, 27, 16, 10, tzinfo=calendar.NY).timestamp()
        p.write_bytes(gzip.compress(json.dumps(payload).encode()))
    bars.DATASET.to_parquet_all(ctx, raws)
    assert not bars.partition_path(TUE).exists() and "AAPL" in _parquet(date(2026, 10, 26))
    assert any("not complete when fetched" in n for n in ctx.notes)


def test_alpaca_day_return_from_one_answer(http_mock):
    http_mock.add("/v2/stocks/bars", _answer())
    _run()
    first, last = _parquet(WINDOW[0])["AAPL"], _parquet(TUE)["AAPL"]
    assert first["close_all"] == 50.5 and first["all_return"] == pytest.approx(50.5 / 50.0 - 1)
    assert last["close_all"] == 52.5 and last["all_return"] == pytest.approx(52.5 / 52.0 - 1)


def test_a_changed_value_on_the_reread_is_kept_as_a_revision(http_mock, monkeypatch):
    http_mock.add("/v2/stocks/bars", _answer())
    _clock(monkeypatch, datetime(2026, 10, 26, 17, 0, tzinfo=calendar.NY))
    _run("monday", as_of=date(2026, 10, 26))
    http_mock.routes.clear()
    changed = _series([date(2026, 10, 20)] + WINDOW)
    changed[4] = bar(date(2026, 10, 26), 104.5, volume=1_200_000)          # Monday's close and volume corrected
    http_mock.add("/v2/stocks/bars", _answer(extra_raw={"AAPL": changed}))
    _clock(monkeypatch, datetime(2026, 10, 27, 17, 0, tzinfo=calendar.NY))
    ctx, _ = _run("tuesday")
    assert _parquet(date(2026, 10, 26))["AAPL"]["close"] == 104.5
    rev = stage.read_rows(bars.revision_path(date(2026, 10, 26), "tuesday"))
    assert [(r["symbol"], r["close"], r["volume"], r["run_id"], r["revised_by"]) for r in rev] == \
        [("AAPL", 104.0, 1_000_000, "monday", "tuesday")]
    assert rev[0]["revised_at"] == _parquet(date(2026, 10, 26))["AAPL"]["fetched_at"]
    assert not bars.revision_path(date(2026, 10, 23), "tuesday").exists()  # unchanged days keep no revision
    assert any("1 bars revised" in n for n in ctx.notes)


def test_a_cut_off_run_asks_only_the_names_no_file_holds(http_mock, monkeypatch):
    monkeypatch.setattr(bars, "CHUNK", 2)
    http_mock.add("/v2/stocks/bars", _answer(fail_after=2))
    with pytest.raises(base.SourceError):
        _run()
    assert len(list(bars.raw_dir(TUE).glob("*.json.gz"))) == 2
    http_mock.routes.clear()
    http_mock.requests.clear()
    http_mock.add("/v2/stocks/bars", _answer())
    _run("again")
    asked = [parse_qs(urlparse(str(r.url)).query) for r in http_mock.calls("/v2/stocks/bars")]
    raw_asked = sorted(s for q in asked if q["adjustment"] == ["raw"] for s in q["symbols"][0].split(","))
    assert raw_asked == ["SPY"] and len(asked) == 4                       # one raw chunk left, all three all chunks
    assert set(_parquet(TUE)) == set(ASKED)


def test_a_refused_symbol_is_split_out_and_the_rest_kept(http_mock):
    http_mock.add("/v2/stocks/bars", _answer(refuse={"GAPX"}))
    _run()
    assert set(_parquet(TUE)) == set(ASKED) - {"GAPX"}
    keys = [i["key"] for i in ops.open_issues(bars.NAME, limit=10)[1] if i["kind"] == "symbol_refused"]
    assert keys == ["GAPX", "GAPX"]                                       # once per adjustment


def test_pages_are_followed_and_tier_a_coverage_counted(http_mock):
    http_mock.add("/v2/stocks/bars", _answer(page_size=7))
    ctx, _ = _run()
    assert len(http_mock.calls("/v2/stocks/bars")) > 2 and set(_parquet(TUE)) == set(ASKED)
    assert ctx.pending[bars.NAME] == {"as_of": "2026-10-27", "asked": 4, "with_bar": 4}


def test_alpaca_pages_without_an_end_are_not_stored(http_mock, monkeypatch):
    monkeypatch.setattr(alpaca, "MAX_PAGES", 3)
    http_mock.add("/v2/stocks/bars", {"bars": {}, "next_page_token": "again"})
    with pytest.raises(base.SourceError, match="3 pages without an end"):
        _run()
    assert not bars.raw_dir(TUE).exists() or not list(bars.raw_dir(TUE).glob("*.json.gz"))


def test_the_backfill_reads_one_window_per_five_sessions(http_mock, monkeypatch):
    seen = []
    monkeypatch.setattr(bars.AlpacaBars, "fetch", lambda self, ctx, as_of: seen.append(as_of) or [])
    ctx = base.RunContext(run_id="b", dataset=bars.NAME)
    bars.DATASET.backfill(ctx, date(2026, 10, 13), TUE)
    assert seen == [date(2026, 10, 13), date(2026, 10, 20), TUE]
