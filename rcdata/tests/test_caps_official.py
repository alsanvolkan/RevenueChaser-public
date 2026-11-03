"""Master plan Order 5, rule 3: ``rcdata/caps.py`` holds only the providers' published limits, each with its source.

Day caps are only the quotas published per day (UW, Alpha Vantage); every other published limit is a request window,
enforced by ``base.Pacer``. A provider with no published limit gets no number at all.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from RevenueChaser.rcdata import caps
from RevenueChaser.rcdata.build import base

OWNER_MARK = "owner-marked avo 2026-11-02"


def _fake_time():
    now = [0.0]

    def sleep(s):
        now[0] += s
    return now, (lambda: now[0]), sleep


def test_day_caps_hold_only_the_quotas_published_per_day():
    assert caps.DAY_CAPS == {"uw": 100_000, "alphavantage": 25}


def test_the_request_windows_are_the_official_ones():
    assert caps.WINDOWS == {
        "sec": (10, 1.0),
        "alpaca": (200, 60.0),
        "openfigi": (250, 60.0),
        "openfigi_search": (20, 60.0),
        "finra": (1_200, 60.0),
        "yfinance": (2_000, 3_600.0),
    }


def test_every_number_in_caps_names_its_source():
    text = Path(caps.__file__).read_text(encoding="utf-8")
    entries = re.findall(r'^\s+"([a-z_]+)": [^\n]*$', text, flags=re.M)
    assert set(entries) == set(caps.DAY_CAPS) | set(caps.WINDOWS)
    for line in re.findall(r'^\s+"[a-z_]+": [^\n]*$', text, flags=re.M):
        assert "https://" in line or OWNER_MARK in line, line


def test_the_pacer_keeps_a_one_second_window():
    now, clock, sleep = _fake_time()
    pacer, sent = base.Pacer(10, 1.0, clock=clock, sleep=sleep), []
    for _ in range(55):
        pacer.wait()
        sent.append(now[0])
        now[0] += 0.001
    for i, t in enumerate(sent):
        assert sum(1 for u in sent[i:] if u - t < 1.0) <= 10
    assert 4.0 < sent[-1] < 6.0


def test_the_pacer_keeps_a_one_hour_window():
    now, clock, sleep = _fake_time()
    pacer, sent = base.Pacer(2_000, 3_600.0, clock=clock, sleep=sleep), []
    for _ in range(4_100):
        pacer.wait()
        sent.append(now[0])
        now[0] += 0.01
    first_hour = [t for t in sent if t < 3_600.0]
    assert len(first_hour) == 2_000
    assert 7_200.0 <= sent[-1] < 7_300.0


def test_run_pacer_is_one_per_provider_and_run_with_its_official_window():
    ctx = base.RunContext(run_id="r", dataset="d", sleep=lambda s: None)
    p = base.run_pacer(ctx, "yfinance")
    assert (p.limit, p.window_s) == caps.WINDOWS["yfinance"]
    assert base.run_pacer(ctx, "yfinance") is p
    assert base.run_pacer(ctx, "finra") is not p


def test_each_provider_paces_at_its_official_window():
    from RevenueChaser.core import sec_throttle
    from RevenueChaser.rcdata.build.sources import alpaca
    ctx = base.RunContext(run_id="r", dataset="d", sleep=lambda s: None)
    assert sec_throttle.LIMIT == caps.WINDOWS["sec"][0]     # the SEC paths: see the fallback test below
    a = alpaca.pacer(ctx)
    assert (a.limit, a.window_s) == caps.WINDOWS["alpaca"]


def test_finra_pages_go_through_the_finra_window(monkeypatch, tmp_path):
    from datetime import date
    from RevenueChaser.rcdata.build.ownership import finra
    waits = []
    ctx = base.RunContext(run_id="r", dataset="d", sleep=lambda s: None)
    monkeypatch.setattr(base.run_pacer(ctx, "finra"), "wait", lambda: waits.append(1))

    class R:
        content, headers = b"[]", {"record-total": "0"}

        def json(self):
            return []
    monkeypatch.setattr(base, "http_post", lambda *a, **k: R())
    monkeypatch.setattr(finra, "raw_dir", lambda: tmp_path)
    with pytest.raises(base.SourceError):           # an empty page: refused, after one paced request
        finra.fetch_date(ctx, date(2026, 10, 15))
    assert waits == [1]


def test_the_spx_history_pull_reads_the_uw_day_cap():
    from RevenueChaser.app import dashboard
    assert dashboard._SPX_HISTORY_MAX_CALLS == caps.DAY_CAPS["uw"]


def test_module_financials_holds_no_uw_limit_of_its_own():
    from RevenueChaser.module_financials import config
    assert not hasattr(config, "UW_RATE_LIMIT_PER_MIN") and not hasattr(config, "UW_RATE_LIMIT_PER_DAY")


@pytest.fixture
def yf_waits(monkeypatch):
    """Counts the waits on the run's yfinance pacer (``base.run_pacer``), whatever reader asks Yahoo."""
    waits = []
    real = base.run_pacer

    def run_pacer(ctx, provider):
        p = real(ctx, provider)
        if provider == "yfinance":
            monkeypatch.setattr(p, "wait", lambda: waits.append(provider))
        return p
    monkeypatch.setattr(base, "run_pacer", run_pacer)
    return waits


def test_the_yahoo_market_series_waits_for_the_yfinance_window(yf_waits, tmp_path):
    from datetime import date
    from RevenueChaser.rcdata.build.sources import yahoo
    ctx = base.RunContext(run_id="r", dataset="d", sleep=lambda s: None)
    yahoo.fetch(ctx, date(2026, 10, 27), download=lambda symbol: [["2026-10-27", 1.0, 1.0, 1.0, 1.0]])
    assert len(yf_waits) == len(yahoo.SYMBOLS)


# ── Review of PR #380: the box as a whole stays inside each shared limit ─────────────────────────────────────────

def test_every_sec_fallback_takes_half_the_limit_so_two_processes_stay_under_it():
    """The dashboard and the builder both ask SEC. When the shared bucket cannot be used each paces itself alone,
    so each in-process spacing is at most half of the SEC's 10 a second."""
    from RevenueChaser.core import sec_throttle
    from RevenueChaser.core.providers import sec_edgar
    from RevenueChaser.rcdata.build.fundamentals import common
    from RevenueChaser.rcdata.build.ownership import sec
    from RevenueChaser.rcdata.build.prices import cover_shares
    assert sec_throttle.LIMIT == caps.WINDOWS["sec"][0]
    assert 2 * sec_throttle.FALLBACK_RATE <= sec_throttle.LIMIT
    assert sec.PER_SECOND <= sec_throttle.FALLBACK_RATE
    for interval in (sec_edgar._THROTTLE_INTERVAL, cover_shares.SEC_INTERVAL_S, common.SEC_INTERVAL_S):
        assert 1 / interval <= sec_throttle.FALLBACK_RATE + 1e-9


def test_openfigi_shares_one_bucket_per_key_inside_its_window():
    """Review of #380, finding 3: the builder and the dashboard map CUSIPs on one key. Both take tokens of one box-wide
    bucket whose burst plus a minute's refill fits the 250, and each falls back to half the window."""
    from RevenueChaser.core.providers import openfigi
    limit, window = caps.WINDOWS["openfigi"]
    assert openfigi.BUCKET_BURST + openfigi.BUCKET_RATE * window <= limit
    assert 2 * (window / openfigi.REQUEST_GAP_S) <= limit


def test_the_shared_bucket_serves_another_name_with_its_own_rate(monkeypatch):
    from RevenueChaser.core import sec_throttle
    slept = []
    monkeypatch.setattr(sec_throttle.time, "sleep", slept.append)
    assert all(sec_throttle.shared_pace(".test_bucket.json", rate=1.0, burst=2.0) for _ in range(3))
    assert len(slept) == 1 and 0.9 < slept[0] <= 1.0          # two from the burst, the third waits a token
    assert sec_throttle.bucket_path(".test_bucket.json").exists()


def test_every_openfigi_map_request_takes_a_token_of_the_key_bucket(monkeypatch):
    from RevenueChaser.core.providers import openfigi
    from RevenueChaser.rcdata.build.spine import openfigi_map
    tokens = []
    monkeypatch.setattr(openfigi, "pace", lambda: tokens.append(1) or True)
    ctx = base.RunContext(run_id="r", dataset="d", sleep=lambda s: None)
    pacer = openfigi_map.key_pacer(ctx)
    pacer.wait()
    pacer.wait()
    assert tokens == [1, 1]
    monkeypatch.setattr(openfigi, "pace", lambda: False)
    assert pacer._fallback.limit == caps.WINDOWS["openfigi"][0] // 2
