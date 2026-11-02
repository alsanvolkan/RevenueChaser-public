"""``funds/etf_holdings``, the sweep (#358, avo 1-2 Nov 2026): every ETF of the spine's ``etf`` table that is no ETN
and no CEF, from UW, once every 30 days; the 38 daily funds unchanged. Retention keeps the last 30 days and, older,
each month's last day and each sweep day, and deletes nothing unless ``RC_ETF_HOLDINGS_PRUNE=1``."""
from __future__ import annotations

import pytest

from RevenueChaser.core.providers import sec_edgar, unusual_whales
from RevenueChaser.rcdata import caps, ops, serving
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


def test_a_first_run_sweeps_every_etf_but_the_etns_and_the_daily_funds_once_from_uw(monkeypatch, env):
    monkeypatch.setattr(etf, "TYPE_CHUNK", 3)                 # the day's partition is merged chunk by chunk
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": DAY}})
    asked = _uw_asked(env)
    assert sorted(set(asked) - set(etf.UW_FUNDS)) == OTHERS, "the sweep asks each other ETF, and no ETN"
    assert len(asked) == len(set(asked)), "one request per ETF"
    assert _served_etfs() >= set(OTHERS) | {f.etf for f in etf.FILES}
    assert "sweep" in row["note"]
    import duckdb
    part = etf.parquet_dir() / f"date={DAY}" / "part-0.parquet"
    cols = [d[0] for d in duckdb.sql(f"SELECT * FROM read_parquet('{part}', hive_partitioning = false)").description]
    assert cols == [c for c, _ in etf.SCHEMA], "a merged partition holds the schema's columns, no hive date column"


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
        row = base.run_request({"dataset": etf.NAME, "params": {"as_of": d, "allow_shrink": True}})
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


def test_pruning_needs_the_requests_allow_shrink_as_well(monkeypatch, env):
    """The shrink guard would refuse the swap after the files are gone: without ``allow_shrink`` it stays a dry run."""
    monkeypatch.setenv("RC_ETF_HOLDINGS_PRUNE", "1")
    for d in RUN_DAYS:
        row = base.run_request({"dataset": etf.NAME, "params": {"as_of": d}})
    assert "retention (dry run, the request has no allow_shrink): 1 day past 30" in row["note"]
    assert _raw_days() == set(RUN_DAYS)


def test_a_stray_folder_beside_the_days_is_no_day_and_fails_nothing(env):
    (etf.raw_dir() / "scratch").mkdir(parents=True)
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": DAY}})
    assert row["result"] in ("changed", "unchanged"), row["note"]


class Killed(BaseException):
    """A kill mid-run (the unit stopped, the box restarted): nothing after it runs, no ``except Exception`` sees it."""


def test_a_sweep_killed_mid_run_goes_on_under_its_day_and_types_what_it_held(monkeypatch, env):
    real, calls = etf.EtfFile.download, []

    def download(self, ctx):
        if self.etf not in etf.DAILY:
            calls.append(self.etf)
            if len(calls) == 4:
                raise Killed
        return real(self, ctx)
    monkeypatch.setattr(etf.EtfFile, "download", download)
    with pytest.raises(Killed):
        base.run_request({"dataset": etf.NAME, "params": {"as_of": DAY}})
    monkeypatch.setattr(etf.EtfFile, "download", real)
    env.requests.clear()
    base.run_request({"dataset": etf.NAME, "params": {"as_of": "2026-11-03"}})
    assert _sweep_calls(env) == OTHERS[3:], "the next day goes on with the same sweep, not a new one"
    assert set(OTHERS) <= _served_etfs(DAY), "the three held before the kill are typed too"


def test_held_sweep_raws_a_failed_typing_left_behind_are_typed_by_the_next_run(monkeypatch, env):
    real = etf.EtfHoldings.to_parquet_all

    def full(self, ctx, raws):
        raise OSError("no space left on device")
    monkeypatch.setattr(etf.EtfHoldings, "to_parquet_all", full)
    assert base.run_request({"dataset": etf.NAME, "params": {"as_of": DAY}})["result"] == "failed"
    monkeypatch.setattr(etf.EtfHoldings, "to_parquet_all", real)
    env.requests.clear()
    base.run_request({"dataset": etf.NAME, "params": {"as_of": "2026-11-03"}})
    assert _sweep_calls(env) == [] and set(OTHERS) <= _served_etfs(DAY)


def _answer(status: int):
    return lambda r: (status, b'{"message": "refused"}', {})


def test_a_rejected_key_stops_the_sweep_unrecorded_and_the_next_run_asks_again(env):
    env.routes.insert(0, ("/etfs/CCOI/holdings", _answer(401), 401, {}))
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": DAY}})
    assert _sweep_calls(env) == ["CCOI"] and "stopped" in row["note"], "a dead key is no reason to ask 3,000 more"
    env.routes.pop(0)
    env.requests.clear()
    base.run_request({"dataset": etf.NAME, "params": {"as_of": "2026-11-03"}})
    assert _sweep_calls(env) == OTHERS and set(OTHERS) <= _served_etfs(DAY)
    assert [i["key"] for i in ops.open_issues(etf.NAME)[1]] == [f"sweep-{DAY}-stopped"], "the stop, no ETF failure"


def test_a_uw_outage_opens_the_breaker_and_records_no_etf_as_failed(monkeypatch, env):
    monkeypatch.setattr(unusual_whales, "_sleep", lambda s: None)
    for t in OTHERS:
        env.routes.insert(0, (f"/etfs/{t}/holdings", _answer(503), 503, {}))
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": DAY}})
    assert sorted(set(_sweep_calls(env))) == OTHERS[:unusual_whales.BREAKER_THRESHOLD]
    assert "0 failed, 8 left" in row["note"], "the outage's ETFs are left, none failed"
    del env.routes[:len(OTHERS)]
    env.requests.clear()
    base.run_request({"dataset": etf.NAME, "params": {"as_of": "2026-11-03"}})
    assert _sweep_calls(env) == OTHERS and set(OTHERS) <= _served_etfs(DAY)
    assert [i["key"] for i in ops.open_issues(etf.NAME)[1]] == [f"sweep-{DAY}-stopped"], "the stop was one issue"


def test_a_sweep_stopped_on_the_same_etfs_run_after_run_records_them_and_goes_on(monkeypatch, env):
    """Five ETFs side by side that UW answers 503 every time open the breaker on every run. Un-recording them each
    time would stall the sweep for good; after ``SWEEP_STOPS`` stops they are recorded and the sweep moves past them."""
    monkeypatch.setattr(unusual_whales, "_sleep", lambda s: None)
    bad = OTHERS[:unusual_whales.BREAKER_THRESHOLD]
    for t in bad:
        env.routes.insert(0, (f"/etfs/{t}/holdings", _answer(503), 503, {}))
    days = ["2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"][:etf.SWEEP_STOPS]
    for d in [DAY] + days[:-1]:
        env.requests.clear()
        base.run_request({"dataset": etf.NAME, "params": {"as_of": d}})
        assert sorted(set(_sweep_calls(env))) == bad
    env.requests.clear()
    base.run_request({"dataset": etf.NAME, "params": {"as_of": days[-1]}})
    assert _sweep_calls(env) == OTHERS[len(bad):], "the recorded five are not asked again"
    rows = ops.open_issues(etf.NAME, limit=200)[1]
    issues = {i["key"]: i["detail"] for i in rows}
    assert all(t in issues[f"sweep-{DAY}"] for t in bad)
    assert [i["key"] for i in rows].count(f"sweep-{DAY}-stopped") == 1, "one issue for the stops at one place"


def test_a_spine_without_etfs_starts_no_sweep_that_would_wait_30_days(monkeypatch, env):
    real = etf.sweep_etfs
    monkeypatch.setattr(etf, "sweep_etfs", lambda: [])
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": DAY}})
    assert not (etf.raw_dir() / etf.SWEEP_STATE).exists() and "no sweep" in row["note"]
    monkeypatch.setattr(etf, "sweep_etfs", real)
    env.requests.clear()
    base.run_request({"dataset": etf.NAME, "params": {"as_of": "2026-11-03"}})
    assert _sweep_calls(env) == OTHERS


def test_a_held_raw_without_its_meta_file_fails_no_later_run(monkeypatch, env):
    monkeypatch.setattr(etf, "SWEEP_PER_RUN", 3)
    real = etf.EtfHoldings.to_parquet_all
    monkeypatch.setattr(etf.EtfHoldings, "to_parquet_all", lambda self, ctx, raws: [])   # held, never typed
    base.run_request({"dataset": etf.NAME, "params": {"as_of": DAY}})
    etf._meta(etf.raw_dir() / DAY / "uw-CCOI.json").unlink()
    monkeypatch.setattr(etf.EtfHoldings, "to_parquet_all", real)
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": "2026-11-03"}})
    assert row["result"] in ("changed", "unchanged"), row["note"]
    assert {"GLD", "LEVX"} <= _served_etfs(DAY) and "CCOI" not in _served_etfs(DAY)


def test_a_404_is_that_etfs_failure_named_apart_from_the_empty_answers(env):
    env.routes.insert(0, ("/etfs/LEVX/holdings", _answer(404), 404, {}))
    env.routes.insert(0, ("/etfs/GLD/holdings", lambda r: (200, b'{"data": []}', {}), 200, {}))
    base.run_request({"dataset": etf.NAME, "params": {"as_of": DAY}})
    issues = {i["kind"]: i for i in ops.open_issues(etf.NAME)[1]}
    assert "GLD" in issues["uw_empty"]["detail"] and "LEVX" not in issues["uw_empty"]["detail"]
    assert "LEVX" in issues["source_failed"]["detail"]


def test_the_uw_day_cap_cuts_a_sweep_and_the_next_run_goes_on(monkeypatch, env):
    monkeypatch.setitem(caps.DAY_CAPS, "uw", len(etf.UW_FUNDS) + 3)
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": DAY}})
    assert len(_sweep_calls(env)) == 3 and "day cap" in row["note"] and "3 asked this run" in row["note"]
    monkeypatch.setitem(caps.DAY_CAPS, "uw", 150_000)
    env.requests.clear()
    base.run_request({"dataset": etf.NAME, "params": {"as_of": "2026-11-03"}})
    assert _sweep_calls(env) == OTHERS[3:]


def test_a_cef_in_the_etf_table_is_not_swept(env):
    import duckdb
    con = duckdb.connect(str(serving.live_path("spine")))
    try:
        con.execute("UPDATE etf SET is_cef = true WHERE ticker = 'TAPR'")
    finally:
        con.close()
    base.run_request({"dataset": etf.NAME, "params": {"as_of": DAY}})
    assert _sweep_calls(env) == [t for t in OTHERS if t != "TAPR"]


def test_a_request_scoped_by_tier_starts_no_sweep(env):
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": DAY, "tiers": ["A"]}})
    assert _sweep_calls(env) == [] and "no sweep" in row["note"]


def test_a_named_etf_that_is_no_ticker_is_not_asked(env):
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": DAY, "names": ["GLD", "A/B"]}})
    assert _uw_asked(env) == ["GLD"] and "A/B" in row["note"]


def test_coverage_reads_the_latest_day_that_has_the_daily_funds(env):
    base.run_request({"dataset": etf.NAME, "params": {"as_of": DAY}})
    row = base.run_request({"dataset": etf.NAME, "params": {"as_of": "2026-11-07", "names": ["GLD"]}})
    assert row["coverage"] == 1.0, "a weekend trial of one ETF is no day of the daily funds"


def test_a_daily_funds_403_does_not_stop_the_sweep_at_its_first_404(env):
    """Review 2: the client keeps a rejected call's ``auth_error`` for the run. Only a rejection of the ETF's own call
    stops the sweep; SMH's 403 is SMH's, and LEVX's 404 is LEVX's."""
    env.routes.insert(0, ("/etfs/SMH/holdings", _answer(403), 403, {}))
    env.routes.insert(0, ("/etfs/LEVX/holdings", _answer(404), 404, {}))
    base.run_request({"dataset": etf.NAME, "params": {"as_of": DAY}})
    assert _sweep_calls(env) == OTHERS and set(OTHERS) - {"LEVX"} <= _served_etfs()


@pytest.mark.parametrize("status", [503, 401])
def test_uw_down_or_a_dead_key_run_after_run_records_no_etf(monkeypatch, env, status):
    """Review 3: a whole UW outage (every call 503) or a rejected key stops the sweep on its first ETF every run. That
    is UW's state, not the ETF's: however many runs it lasts, nothing is recorded, one issue says so, and the sweep
    goes on whole once UW answers again."""
    monkeypatch.setattr(unusual_whales, "_sleep", lambda s: None)
    env.routes.insert(0, ("uw.test/api/etfs/", _answer(status), status, {}))      # UW only, not the SSGA files
    for d in [DAY, "2026-11-03", "2026-11-04", "2026-11-05"]:
        base.run_request({"dataset": etf.NAME, "params": {"as_of": d}})
    env.routes.pop(0)
    env.requests.clear()
    base.run_request({"dataset": etf.NAME, "params": {"as_of": "2026-11-06"}})
    assert _sweep_calls(env) == OTHERS and set(OTHERS) <= _served_etfs(DAY)
    keys = [i["key"] for i in ops.open_issues(etf.NAME, limit=200)[1] if i["key"].startswith("sweep-")]
    assert keys == [f"sweep-{DAY}-stopped"], keys


def test_a_breaker_stop_un_records_only_the_failures_since_uws_last_answer(monkeypatch, env):
    """CCOI's 404 is CCOI's; GLD answers; the next five open the breaker. Only those five are left for the next run."""
    monkeypatch.setattr(unusual_whales, "_sleep", lambda s: None)
    env.routes.insert(0, ("/etfs/CCOI/holdings", _answer(404), 404, {}))
    for t in OTHERS[2:7]:
        env.routes.insert(0, (f"/etfs/{t}/holdings", _answer(503), 503, {}))
    base.run_request({"dataset": etf.NAME, "params": {"as_of": DAY}})
    del env.routes[:5]
    env.requests.clear()
    base.run_request({"dataset": etf.NAME, "params": {"as_of": "2026-11-03"}})
    assert _sweep_calls(env) == OTHERS[2:], "CCOI stays recorded, GLD is held"
