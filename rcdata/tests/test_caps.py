"""``rcdata/caps.py`` through the builder: a provider at its day cap is refused before the first call."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.rcdata import caps, ops
from RevenueChaser.rcdata.build import base, uw_spot_expiry_strike as uw


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 26, 21, 0, tzinfo=timezone.utc))


def test_a_dataset_whose_provider_is_at_its_cap_is_skipped_before_any_call(http_mock):
    ops.count_calls("uw", caps.DAY_CAPS["uw"])
    rid = ops.enqueue(uw.NAME, {"as_of": "2026-10-26"}, by="test")
    row = base.run_request(ops.next_request())
    assert http_mock.requests == []
    assert row["result"] == "skipped" and row["calls"] == 0
    assert row["note"] == f"day cap reached: uw {caps.DAY_CAPS['uw']}/{caps.DAY_CAPS['uw']}"
    with ops.connect() as con:
        assert con.execute("SELECT status FROM build_requests WHERE id = ?", (rid,)).fetchone()[0] == "skipped"
        assert con.execute("SELECT status FROM job_runs2").fetchone()[0] == "skipped"


def test_one_call_under_the_cap_still_runs(http_mock):
    from . import uw_fixture
    ops.count_calls("uw", caps.DAY_CAPS["uw"] - 1)
    http_mock.add("expiry-strike", uw_fixture.body())
    row = base.run_request({"dataset": uw.NAME, "params": {"as_of": "2026-10-26"}})
    assert row["result"] == "changed" and len(http_mock.requests) == 1


def test_a_provider_without_a_cap_is_never_refused():
    ops.count_calls("cboe", 10**9)
    assert caps.at_cap("cboe") == (False, 10**9, None)


# ── S1: the cap inside a run, the counter written as it goes ──

@pytest.fixture
def market(http_mock, fixture_text, monkeypatch):
    from RevenueChaser.rcdata.build.sources import bls, cboe, cftc, fed, fred, treasury
    monkeypatch.setenv("FRED_API_KEY", "k")
    for mod, attr, url in ((treasury, "BASE", "https://treasury.test"), (cboe, "BASE", "https://cboe.test"),
                           (fred, "BASE", "https://fred.test"), (cftc, "TFF_BASE", "https://cftc.test/udgc-27he.json"),
                           (cftc, "DISAGG_BASE", "https://cftc.test/72hh-3qpy.json"), (fed, "BASE", "https://fed.test"),
                           (bls, "BASE", "https://bls.test")):
        monkeypatch.setattr(mod, attr, url)
    for sub, name in (("treasury.test", "treasury_2026.csv"), ("cboe.test", "VIX_History.csv"),
                      ("fred.test", "fred_DGS10.json"), ("udgc-27he", "cftc_tff.json"),
                      ("72hh-3qpy", "cftc_disagg.json"), ("fed.test", "fomc.htm"), ("bls.test", "bls.ics")):
        http_mock.add(sub, fixture_text(name))
    return http_mock


def _series():
    from RevenueChaser.rcdata.build import market_series as ms
    return base.run_request({"dataset": ms.NAME, "params": {"as_of": "2026-10-27"}}, sleep=lambda s: None)


def test_fred_at_its_cap_does_not_skip_the_keyless_sources(market):
    ops.count_calls("fred", caps.DAY_CAPS["fred"])
    row = _series()
    assert row["result"] == "changed" and market.calls("fred.test") == []
    assert market.calls("cboe.test") and "fred" in row["note"]
    with ops.connect() as con:
        detail = con.execute("SELECT detail FROM build_issues WHERE kind = 'source_failed' AND key = 'fred'").fetchone()
    assert "day cap reached" in detail[0]


def test_the_cap_is_checked_before_every_call_not_only_at_the_start(market, monkeypatch):
    monkeypatch.setitem(caps.DAY_CAPS, "fred", 3)
    _series()
    assert len(market.calls("fred.test")) == 3
    assert ops.calls_today("fred") == 3


def test_the_counter_is_written_during_the_run_and_survives_a_kill(market, monkeypatch):
    from RevenueChaser.rcdata.build.sources import bls, cboe
    monkeypatch.setattr(base, "COUNT_FLUSH_EVERY", 5)
    written = []
    real = ops.count_calls
    monkeypatch.setattr(ops, "count_calls", lambda *a, **k: (written.append(a), real(*a, **k)))

    def killed(ctx, as_of):
        raise KeyboardInterrupt("SIGTERM")
    monkeypatch.setattr(bls, "fetch", killed)
    with pytest.raises(KeyboardInterrupt):
        _series()
    assert ops.calls_today("cboe") == len(cboe.SYMBOLS) and ops.calls_today("fred") == 13
    assert any(a[0] == "cboe" and a[1] == 5 for a in written), "the counter is written every 5 calls"
    with ops.connect() as con:
        assert con.execute("SELECT status FROM job_runs2").fetchone()[0] == "killed"


def test_a_uw_backfill_stops_at_the_cap_and_keeps_what_it_has(http_mock, monkeypatch):
    from urllib.parse import parse_qs

    from . import uw_fixture

    def answer(request):
        d = date.fromisoformat(parse_qs(request.url.query.decode())["date"][0])
        return 200, uw_fixture.body(d), {}
    http_mock.add("expiry-strike", answer)
    monkeypatch.setitem(caps.DAY_CAPS, "uw", 2)
    row = base.run_request({"dataset": uw.NAME, "params": {"mode": "backfill", "from": "2026-10-19",
                                                            "to": "2026-10-23"}})
    assert len(http_mock.requests) == 2 and row["rows"] == 2 * 391
    assert row["result"] == "changed" and "day cap reached" in row["note"]
