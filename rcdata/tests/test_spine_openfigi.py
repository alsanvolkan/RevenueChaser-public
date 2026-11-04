"""``openfigi/map``: only the CUSIPs not mapped yet are asked, at most 100 a request and under 25 requests a minute,
the key only in its header, no key asks nothing, an 'absent' answer is asked again after 90 days, and a day's
batches already on disk are not asked again. A CINS is asked as ``ID_CINS``; one the map asked as ``ID_CUSIP`` before
that is asked again. A Tier A name with no CUSIP is asked by ticker instead, and the answer types it on the next
spine build (the our-universe plan of 2026-11-04, item 1)."""
from __future__ import annotations

import json

import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata import caps, serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.spine import openfigi_map as of, sec_tickers, wrds

from . import spine_fixture as fx

KEY = "figi-test-key-123"


@pytest.fixture
def env(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setenv("RC_OPENFIGI_BASE", "https://figi.test")
    monkeypatch.setenv("OPENFIGI_API_KEY", KEY)
    http_mock.add("company_tickers_exchange.json", fx.SEC_FILE)
    http_mock.add("figi.test/v3/mapping", fx.figi_answer)
    monkeypatch.setattr(wrds, "_factory", lambda: fx.FakeWrds())
    wrds.reset()
    base.run_request({"dataset": sec_tickers.NAME, "params": {"as_of": "2026-10-27"}})
    base.run_request({"dataset": wrds.SEC_IDHIST.name, "requested_by": "cli", "params": {"mode": "backfill",
                                                                                          "to": "2026-10-27"}})
    yield http_mock
    wrds.reset()


def _cusip_calls(http_mock) -> list:
    """The CUSIP lane's requests only: the run also asks a ``TICKER`` job for a Tier A name with no CUSIP (SOLO)."""
    return [r for r in http_mock.calls("figi.test") if json.loads(r.content)[0]["idType"] != of.ID_TICKER]


def _jobs(http_mock) -> list[list[str]]:
    return [[j["idValue"] for j in json.loads(r.content)] for r in _cusip_calls(http_mock)]


def _run(day="2026-10-27", **kw):
    return base.run_request({"dataset": of.NAME, "params": {"as_of": day}}, **kw)


def test_every_active_cusip_of_an_sec_cik_is_asked_once_with_the_key_in_its_header(env):
    row = _run()
    # Two requests: one of CUSIP jobs, and the ticker lane's one job for SOLO (Tier A, no CUSIP).
    assert row["result"] == "changed" and row["calls"] == 2
    (jobs,) = _jobs(env)
    assert set(jobs) == {s["cusip"] for s in fx.SECURITY + fx.NEW_SECURITY if s["secstat"] == "A"}
    req = _cusip_calls(env)[0]
    assert req.headers["X-OPENFIGI-APIKEY"] == KEY and KEY not in str(req.url)
    assert json.loads(req.content)[0] == {"idType": "ID_CUSIP", "idValue": jobs[0], "exchCode": "US"}
    with serving.open_read("spine") as con:
        assert con.execute("SELECT figi, figi_ticker FROM security WHERE ticker = 'HEI-A'").fetchone() == (
            "BBG000BL3Y00", "HEI-A")
        assert con.execute("SELECT figi FROM security WHERE ticker = 'NVR'").fetchone() == (None,)


def test_a_second_run_asks_nothing_new(env):
    _run()
    env.requests.clear()
    row = _run("2026-11-03")
    assert _jobs(env) == [] and row["calls"] == 0


def test_an_absent_answer_is_asked_again_after_90_days(env):
    _run()
    env.requests.clear()
    _run("2027-01-24")                                      # 89 days on: still inside the window
    assert _jobs(env) == []
    _run("2027-01-26")
    assert _jobs(env) == [["62944T105"]]                    # NVR's, the one 'absent'


def _map_row(cusip: str) -> tuple:
    return of._query(f"SELECT status, figi FROM read_parquet({base.sql_path(of.partition())}) "
                     f"WHERE cusip = '{cusip}'")[0]


def test_a_cins_is_asked_as_id_cins_and_gets_its_figi(env):
    _run()
    (req,) = _cusip_calls(env)
    asked = {j["idValue"]: j["idType"] for j in json.loads(req.content)}
    assert asked.pop("G29183103") == "ID_CINS" and set(asked.values()) == {"ID_CUSIP"}       # Eaton: a CINS
    assert _map_row("G29183103") == ("present", "BBG000BJ3PD2")
    (raw,) = (of.raw_dir() / "2026-10-27").glob("batch-*.json.gz")
    body = base.read_raw_json(raw)
    assert dict(zip(body["cusips"], body["id_types"]))["G29183103"] == "ID_CINS"


def test_a_cins_the_map_asked_as_id_cusip_is_asked_again_inside_the_90_days(env, monkeypatch):
    own = of.id_type
    monkeypatch.setattr(of, "id_type", lambda code: of.ID_CUSIP)          # how the map asked until 2026-10-28
    _run()
    assert _map_row("G29183103") == ("absent", None)
    monkeypatch.setattr(of, "id_type", own)
    env.requests.clear()
    _run("2026-11-03")                                                     # a week on: an ID_CUSIP 'absent' is no answer
    assert _jobs(env) == [["G29183103"]]
    assert _map_row("G29183103") == ("present", "BBG000BJ3PD2")
    env.requests.clear()
    _run("2026-11-10")
    assert _jobs(env) == []


def test_a_cins_in_a_days_batch_written_before_the_id_types_is_asked_again_that_day(env):
    raw = of.raw_dir() / "2026-10-27" / "batch-00000.json.gz"
    base.write_raw_json(of._meta(raw), {"as_of": "2026-10-27", "fetched_at": 1.0, "run_id": "r0", "jobs": 2})
    base.write_raw_json(raw, {"cusips": ["G29183103", "594918104"],
                              "answers": [{"warning": "No identifier found."}, {"data": [fx.FIGI["594918104"]]}]})
    _run()
    (jobs,) = _jobs(env)
    assert "G29183103" in jobs and "594918104" not in jobs
    assert _map_row("G29183103") == ("present", "BBG000BJ3PD2")           # the newer answer wins the CUSIP


def test_an_absent_cins_asked_as_id_cins_waits_the_90_days(env, monkeypatch):
    monkeypatch.delitem(fx.FIGI, "G29183103")                              # OpenFIGI does not know it, even as a CINS
    _run()
    env.requests.clear()
    _run("2027-01-24")
    assert _jobs(env) == []
    _run("2027-01-26")
    assert sorted(_jobs(env)[0]) == ["62944T105", "G29183103"]


def test_an_absent_cusip_waits_the_90_days_without_its_raw_batches(env):
    _run()
    for raw in (of.raw_dir() / "2026-10-27").glob("batch-*"):
        raw.unlink()
    env.requests.clear()
    _run("2026-11-03")
    assert _jobs(env) == []                                                # only a CINS needs its batch to say how it was asked


def test_the_id_type_of_a_code():
    assert of.id_type("G29183103") == of.ID_CINS and of.id_type("594918104") == of.ID_CUSIP
    assert of.id_type("00206R102") == of.ID_CUSIP                          # a letter inside a CUSIP is no CINS


def test_no_key_fails_and_asks_nothing(env, monkeypatch):
    monkeypatch.delenv("OPENFIGI_API_KEY")
    env.requests.clear()
    row = _run()
    assert row["result"] == "failed" and "OPENFIGI_API_KEY" in row["note"] and _jobs(env) == []


def test_without_the_key_bucket_a_run_keeps_half_the_window_and_holds_100_jobs(env, monkeypatch):
    monkeypatch.setattr(of.figi_client, "pace", lambda: False)    # the shared bucket cannot be used
    monkeypatch.setitem(caps.WINDOWS, "openfigi", (50, 60.0))     # the window, made small enough to fill: 25 a run
    many = [f"{i:08d}{i % 10}" for i in range(2600)]       # 26 requests of 100
    monkeypatch.setattr(of, "wanted_cusips", lambda: many)
    now, slept = [0.0], []

    def sleep(s):
        slept.append(s)
        now[0] += s
    row = _run(sleep=sleep, clock=lambda: now[0])
    assert row["calls"] == 27 and all(len(j) == 100 for j in _jobs(env))   # 26 CUSIP + 1 ticker (SOLO)
    assert now[0] >= 60.0                                   # the 25th request waited for the window


def test_a_days_batches_already_on_disk_are_not_asked_again(env, monkeypatch):
    raw = of.raw_dir() / "2026-10-27" / "batch-00000.json.gz"
    base.write_raw_json(of._meta(raw), {"as_of": "2026-10-27", "fetched_at": 1.0, "run_id": "r0", "jobs": 1})
    base.write_raw_json(raw, {"cusips": ["594918104"], "answers": [{"data": [fx.FIGI["594918104"]]}]})
    _run()
    (jobs,) = _jobs(env)
    assert "594918104" not in jobs and (of.raw_dir() / "2026-10-27" / "batch-00001.json.gz").exists()


def test_a_job_error_is_not_stored_and_is_asked_next_run():
    assert of.answer("X", {"error": "Invalid idValue"}) is None
    assert of.answer("X", {"warning": "No identifier found."}) == {"cusip": "X", "status": "absent"}
    assert of.valid_cusip("594918104") and not of.valid_cusip("5949181") and not of.valid_cusip(None)


# ── Asking by ticker: the Tier A names with no CUSIP (plan 2026-11-04, item 1) ────────────────────────────────

def _spine(day="2026-10-27"):
    """Every spine dataset in order, so the live ``security`` table is there for ``wanted_tickers``."""
    from RevenueChaser.rcdata.build.spine import funds as fu, sec_tickers_mf as mf, security
    for name in (sec_tickers.NAME, mf.NAME, wrds.SEC_IDHIST.name, wrds.CO_HGIC.name, wrds.IDXCST_HIS.name,
                 of.NAME, fu.NAME, security.NAME):
        params = {"mode": "backfill", "to": day} if name.startswith("wrds/") else {"as_of": day}
        base.run_request({"dataset": name, "requested_by": "cli", "params": params})


@pytest.fixture
def spine(env, monkeypatch):
    from RevenueChaser.rcdata.build.spine import funds as fu
    monkeypatch.setattr(fu, "FLOOR", {"sp500": 4, "sp400": 2, "sp600": 2, "ndx": 1})
    from RevenueChaser.core.providers import sec_edgar as se
    env.add("company_tickers_mf.json", fx.SEC_MF_FILE)
    fx.add_fund_routes(env)
    assert se.SEC_WWW_BASE == "https://sec.test"
    _spine()
    return env


def _ticker_jobs(http_mock) -> list[list[tuple[str, str]]]:
    return [[(j["idType"], j["idValue"]) for j in json.loads(r.content) if j["idType"] == of.ID_TICKER]
            for r in http_mock.calls("figi.test")]


def test_a_tier_a_name_with_no_cusip_is_asked_by_ticker_and_typed_from_that_answer(spine):
    assert [j for j in _ticker_jobs(spine) if j] == [[(of.ID_TICKER, "SOLO")]]
    assert of._query(f"SELECT status, figi, ticker FROM read_parquet({base.sql_path(of.ticker_partition())})") == [
        ("present", "BBG0SOLO0000", "SOLO")]
    with serving.open_read("spine") as con:
        assert con.execute("SELECT tier, cusip, figi, share_class_figi, figi_ticker, security_type, figi_source "
                           "FROM security WHERE ticker = 'SOLO'").fetchone() == (
            "A", None, "BBG0SOLO0000", "BBG0SOLO0SC0", "SOLO", "common", of.FIGI_BY_TICKER)
    assert of.wanted_tickers() == []                       # it has a FIGI now: never asked again
    spine.requests.clear()
    _spine("2026-11-03")
    assert [j for j in _ticker_jobs(spine) if j] == []


def test_a_name_with_a_cusip_is_never_asked_by_ticker(spine):
    assert "MSFT" not in of.wanted_tickers() and "NVR" not in of.wanted_tickers()   # NVR: a CUSIP, absent FIGI


def test_a_ticker_already_answered_is_not_asked_again_until_the_retry_window(env, monkeypatch):
    monkeypatch.setattr(of, "wanted_tickers", lambda: ["SOLO", "NOPE"])
    monkeypatch.setattr(of, "wanted_cusips", lambda: [])
    _run()
    assert _ticker_jobs(env) == [[(of.ID_TICKER, "SOLO"), (of.ID_TICKER, "NOPE")]]
    env.requests.clear()
    _run("2027-01-24")                                      # 89 days on: still inside the window
    assert _ticker_jobs(env) == []
    _run("2027-01-26")
    assert _ticker_jobs(env) == [[(of.ID_TICKER, "NOPE")]]   # only the 'absent' one


def test_a_days_ticker_batches_already_on_disk_are_not_asked_again(env, monkeypatch):
    monkeypatch.setattr(of, "wanted_tickers", lambda: ["SOLO"])
    monkeypatch.setattr(of, "wanted_cusips", lambda: [])
    raw = of.ticker_raw_dir() / "2026-10-27" / "batch-00000.json.gz"
    base.write_raw_json(of._meta(raw), {"as_of": "2026-10-27", "fetched_at": 1.0, "run_id": "r0", "jobs": 1})
    base.write_raw_json(raw, {"tickers": ["SOLO"], "answers": [{"data": [fx.TICKER_FIGI["SOLO"]]}]})
    _run()
    assert _ticker_jobs(env) == []


def test_the_ticker_is_asked_in_openfigis_spelling_and_read_back_in_ours(env, monkeypatch):
    monkeypatch.setattr(of, "wanted_tickers", lambda: ["BRK-B"])
    monkeypatch.setattr(of, "wanted_cusips", lambda: [])
    monkeypatch.setitem(fx.TICKER_FIGI, "BRK/B", {"figi": "BBG0BRKB0000", "ticker": "BRK/B", "exchCode": "US",
                                                  "securityType": "Common Stock"})
    _run()
    assert _ticker_jobs(env) == [[(of.ID_TICKER, "BRK/B")]]
    assert of._query(f"SELECT asked_ticker, ticker FROM read_parquet({base.sql_path(of.ticker_partition())})") == [
        ("BRK-B", "BRK-B")]


def test_no_spine_file_asks_no_ticker(env, monkeypatch):
    def missing(kind):
        raise serving.ServingFileMissing(kind)
    monkeypatch.setattr(of.serving, "open_read", missing)
    assert of.wanted_tickers() == []


def test_a_ticker_job_error_is_not_stored_and_is_asked_next_run():
    assert of.ticker_answer("SOLO", {"error": "Invalid idValue"}) is None
    assert of.ticker_answer("SOLO", {"warning": "No identifier found."}) == {"asked_ticker": "SOLO",
                                                                            "status": "absent"}


def test_the_two_lanes_share_the_one_request_budget(env, monkeypatch):
    monkeypatch.setattr(of, "MAX_REQUESTS", 1)
    monkeypatch.setattr(of, "wanted_cusips", lambda: [f"{i:08d}{i % 10}" for i in range(200)])   # two requests
    monkeypatch.setattr(of, "wanted_tickers", lambda: ["SOLO"])
    row = _run()
    assert row["calls"] == 1 and _ticker_jobs(env) == [[]]
    assert "100 CUSIPs left for the next run" in row["note"] and "1 tickers left for the next run" in row["note"]


def test_a_ticker_openfigi_does_not_know_is_a_build_issue(env, monkeypatch):
    from RevenueChaser.rcdata import ops
    from RevenueChaser.rcdata.build.spine import funds as fu, security
    monkeypatch.delitem(fx.TICKER_FIGI, "SOLO")                     # OpenFIGI knows it by neither CUSIP nor ticker
    monkeypatch.setattr(fu, "FLOOR", {"sp500": 4, "sp400": 2, "sp600": 2, "ndx": 1})
    env.add("company_tickers_mf.json", fx.SEC_MF_FILE)
    fx.add_fund_routes(env)
    _spine()
    assert _ticker_jobs(env)[-1] == [(of.ID_TICKER, "SOLO")]
    issues = {(i["kind"], i["key"]) for i in ops.open_issues(security.NAME, limit=500)[1]}
    assert ("tier_a_no_figi_by_ticker", "SOLO") in issues
    assert not {k for k, key in issues if k == "tier_a_no_figi_by_ticker" and key != "SOLO"}
