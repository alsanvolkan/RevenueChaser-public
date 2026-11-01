"""``funds/etf_holdings``, the sweep (#358, avo 1-2 Nov 2026): every ETF of the spine's ``etf`` table that is no ETN
and no CEF, from UW, once every 30 days; the 38 daily funds unchanged. Retention keeps the last 30 days and, older,
each month's last day and each sweep day, and deletes nothing unless ``RC_ETF_HOLDINGS_PRUNE=1``."""
from __future__ import annotations

import pytest

from RevenueChaser.core.providers import sec_edgar, unusual_whales
from RevenueChaser.rcdata import ops, serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.spine import av_listing, etf_holdings as etf, ncen, sec_tickers

from . import spine_fixture as fx
from .test_spine_etf import AV, SEC_FILE

DAY = "2026-11-02"


@pytest.fixture
def env(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(unusual_whales, "BASE", "https://uw.test/api")
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "uw-test-key")
    monkeypatch.setenv("RC_HTTP_CONTACT", "ops@example.org")
    monkeypatch.setenv("RC_ALPHAVANTAGE_BASE", "https://av.test")
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "av-test-key")
    monkeypatch.setattr(av_listing, "MIN_ETFS", 1)
    http_mock.add("company_tickers_exchange.json", SEC_FILE)
    fx.add_ncen_routes(http_mock)
    http_mock.add("state=delisted", "{}")
    http_mock.add("av.test/query", AV)
    base.run_request({"dataset": ncen.NAME, "params": {"mode": "backfill", "to": DAY}})
    base.run_request({"dataset": av_listing.NAME, "params": {"as_of": DAY}})
    base.run_request({"dataset": sec_tickers.NAME, "params": {"as_of": DAY}})
    fx.add_etf_routes(http_mock)
    http_mock.add("/etfs/", fx.uw_holdings_json(fx.SP500[:3]))      # every other ETF UW is asked for
    http_mock.requests.clear()
    return http_mock



OTHERS = ["CCOI", "GLD", "LEVX", "NEWE", "QYLD", "SOXL", "SPY", "TAPR"]   # the fixture's etf rows, less ETNs, XLK, SOXX



def _uw_asked(env) -> list[str]:
    return sorted(p.split("/etfs/")[1].split("/")[0] for p in (str(r.url) for r in env.requests) if "uw.test" in p and "/etfs/" in p)


def _served_etfs(day: str = DAY) -> set[str]:
    with serving.open_read("spine") as con:
        return {r[0] for r in con.execute(f"SELECT DISTINCT etf FROM {etf.TABLE} WHERE as_of = DATE '{day}'").fetchall()}


def test_a_first_run_sweeps_every_etf_but_the_etns_and_the_daily_funds_once_from_uw(env):
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": DAY}})
    asked = _uw_asked(env)
    assert sorted(set(asked) - set(etf.UW_FUNDS)) == OTHERS, "the sweep asks each other ETF, and no ETN"
    assert len(asked) == len(set(asked)), "one request per ETF"
    assert _served_etfs() >= set(OTHERS) | {f.etf for f in etf.FILES}
    assert "sweep" in row["note"]


def _sweep_calls(env) -> list[str]:
    return [t for t in _uw_asked(env) if t not in etf.UW_FUNDS]


@pytest.mark.parametrize("later, swept", [("2026-11-03", False), ("2026-12-01", False), ("2026-12-02", True)])
def test_the_next_sweep_waits_30_days_and_the_daily_funds_do_not(env, later, swept):
    base.run_request({"dataset": etf.NAME, "params": {"as_of": DAY}})
    env.requests.clear()
    base.run_request({"dataset": etf.NAME, "params": {"as_of": later}})
    assert (_sweep_calls(env) == OTHERS) is swept and (not _sweep_calls(env)) is (not swept)
    assert {f.etf for f in etf.FILES} <= _served_etfs(later), "the daily funds are asked every day"


def test_a_sweep_cut_by_the_runs_own_limit_goes_on_under_its_first_day(monkeypatch, env):
    monkeypatch.setattr(etf, "SWEEP_PER_RUN", 3)
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": DAY}})
    assert len(_sweep_calls(env)) == 3 and "5 left" in row["note"]
    env.requests.clear()
    base.run_request({"dataset": etf.NAME, "params": {"as_of": "2026-11-03"}})
    assert len(_sweep_calls(env)) == 3, "the next run asks the next three, not the first three again"
    env.requests.clear()
    base.run_request({"dataset": etf.NAME, "params": {"as_of": "2026-11-04"}})
    assert len(_sweep_calls(env)) == 2
    assert set(OTHERS) <= _served_etfs(DAY), "every swept ETF is served under the sweep's own day"
    env.requests.clear()
    base.run_request({"dataset": etf.NAME, "params": {"as_of": "2026-11-05"}})
    assert _sweep_calls(env) == [], "a finished sweep waits its 30 days"


def test_an_empty_uw_answer_is_one_issue_for_the_sweep_and_is_not_asked_again(monkeypatch, env):
    monkeypatch.setattr(etf, "SWEEP_PER_RUN", 3)              # CCOI, GLD, LEVX this run; the sweep is not done
    env.routes.insert(0, ("/etfs/GLD/holdings", lambda r: (200, b'{"data": []}', {}), 200, {}))
    base.run_request({"dataset": etf.NAME, "params": {"as_of": DAY}})
    assert "GLD" not in _served_etfs() and ops.open_issues(etf.NAME)[0] == 0, "the issue waits for the sweep's end"
    for later in ("2026-11-03", "2026-11-04"):
        env.requests.clear()
        base.run_request({"dataset": etf.NAME, "params": {"as_of": later}})
        assert "GLD" not in _sweep_calls(env), "an empty answer is not asked again within its sweep"
    n, issues = ops.open_issues(etf.NAME)
    assert [i["key"] for i in issues] == [f"sweep-{DAY}"] and "GLD" in issues[0]["detail"]


def test_without_a_uw_key_there_is_no_sweep(monkeypatch, env):
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY")
    base.run_request({"dataset": etf.NAME, "params": {"as_of": DAY}})
    assert _uw_asked(env) == [] and not (set(OTHERS) & _served_etfs())


RUN_DAYS = [DAY, "2026-11-15", "2026-11-30", "2026-12-15", "2027-01-05"]   # sweeps on 11-02 and 12-15


def _served_days() -> set[str]:
    with serving.open_read("spine") as con:
        return {str(r[0]) for r in con.execute(f"SELECT DISTINCT as_of FROM {etf.TABLE}").fetchall()}


def _raw_days() -> set[str]:
    return {p.name for p in etf.raw_dir().iterdir() if p.is_dir()}


def test_retention_is_a_dry_run_by_default_and_deletes_nothing(env):
    for d in RUN_DAYS:
        row = base.run_request({"dataset": etf.NAME, "params": {"as_of": d}})
    assert "retention (dry run): 1 day past 30 would be removed: 2026-11-15" in row["note"]
    assert _served_days() == set(RUN_DAYS) and _raw_days() == set(RUN_DAYS)


def test_retention_keeps_30_days_each_months_last_day_and_each_sweep_day(monkeypatch, env):
    monkeypatch.setenv("RC_ETF_HOLDINGS_PRUNE", "1")
    for d in RUN_DAYS:
        row = base.run_request({"dataset": etf.NAME, "params": {"as_of": d}})
    kept = {DAY, "2026-11-30", "2026-12-15", "2027-01-05"}    # 11-02 a sweep day, 11-30 November's last
    assert "retention: removed 1 day past 30: 2026-11-15" in row["note"]
    assert _served_days() == kept and _raw_days() == kept


def test_coverage_is_the_daily_funds_share_and_a_sweep_does_not_lift_it_past_one(env):
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": DAY}})
    assert row["coverage"] == 1.0


def test_a_request_naming_etfs_asks_only_those_and_starts_no_sweep(env):
    """The trial before the first sweep: a Fetch with ``names`` asks those ETFs (daily funds or not) and nothing else."""
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": DAY, "names": ["GLD", "SPY", "SMH"]}})
    assert _uw_asked(env) == ["GLD", "SMH", "SPY"] and len(env.requests) == 3
    assert {"GLD", "SPY", "SMH"} <= _served_etfs() and not (etf.raw_dir() / etf.SWEEP_STATE).exists()
    env.requests.clear()
    base.run_request({"dataset": etf.NAME, "params": {"as_of": DAY}})
    assert _sweep_calls(env) == [t for t in OTHERS if t not in ("GLD", "SPY")], "the sweep starts on the next run"
