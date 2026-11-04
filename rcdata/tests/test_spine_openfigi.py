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
    """The CUSIP lane's requests only (the ticker lane has ``_ticker_jobs``)."""
    return [r for r in http_mock.calls("figi.test") if json.loads(r.content)[0]["idType"] != of.ID_TICKER]


def _jobs(http_mock) -> list[list[str]]:
    return [[j["idValue"] for j in json.loads(r.content)] for r in _cusip_calls(http_mock)]


def _run(day="2026-10-27", **kw):
    return base.run_request({"dataset": of.NAME, "params": {"as_of": day}}, **kw)


def test_every_active_cusip_of_an_sec_cik_is_asked_once_with_the_key_in_its_header(env):
    row = _run()
    # Two requests: the CUSIP lane's one, and the ticker lane's one for the Tier A names that have no FIGI yet
    # (the lane asks by ticker whenever the FIGI is missing, with or without a CUSIP).
    assert row["result"] == "changed" and row["calls"] == 2
    (jobs,) = _jobs(env)
    assert set(jobs) == {s["cusip"] for s in fx.SECURITY + fx.NEW_SECURITY if s["secstat"] == "A"}
    req = _cusip_calls(env)[0]
    assert req.headers["X-OPENFIGI-APIKEY"] == KEY and KEY not in str(req.url)
    assert json.loads(req.content)[0] == {"idType": "ID_CUSIP", "idValue": jobs[0], "exchCode": "US"}
    with serving.open_read("spine") as con:
        assert con.execute("SELECT figi, figi_ticker FROM security WHERE ticker = 'HEI-A'").fetchone() == (
            "BBG000BL3Y00", "HEI-A")
        # NVR's CUSIP is one OpenFIGI does not know, so the CUSIP lane leaves it empty and the ticker lane fills it.
        assert con.execute("SELECT figi, figi_source FROM security WHERE ticker = 'NVR'").fetchone() == (
            "BBG000BW5N90", of.FIGI_BY_TICKER)


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
    # 26 CUSIP requests of 100 jobs each, plus the ticker lane's one for the Tier A names still without a FIGI.
    assert row["calls"] == 27 and all(len(j) == 100 for j in _jobs(env))
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

def _spine_env(http_mock, monkeypatch, *, unlink_nvr: bool) -> None:
    """The whole spine's sources behind the mock, set up before anything runs. With ``unlink_nvr`` NVR's
    ``comp.security`` row is dropped, so its CIK finds no security: a Tier A name with no CUSIP, the one case an
    ``ID_CUSIP`` job can never answer for. Its own env, not the module's, because the patch has to be in place
    before the WRDS backfill writes its Parquet."""
    from RevenueChaser.rcdata.build.spine import funds as fu
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setenv("RC_OPENFIGI_BASE", "https://figi.test")
    monkeypatch.setenv("OPENFIGI_API_KEY", KEY)
    if unlink_nvr:
        monkeypatch.setattr(fx, "SECURITY", [r for r in fx.SECURITY if r["tic"] != "NVR"])
    monkeypatch.setattr(fu, "FLOOR", {"sp500": 4, "sp400": 2, "sp600": 2, "ndx": 1})
    monkeypatch.setattr(wrds, "_factory", lambda: fx.FakeWrds())
    wrds.reset()
    http_mock.add("company_tickers_exchange.json", fx.SEC_FILE)
    http_mock.add("company_tickers_mf.json", fx.SEC_MF_FILE)
    http_mock.add("figi.test/v3/mapping", fx.figi_answer)
    fx.add_fund_routes(http_mock)


def _spine(day="2026-10-27"):
    """Every spine dataset in order, so the live ``security`` table is there for ``wanted_tickers``."""
    from RevenueChaser.rcdata.build.spine import funds as fu, sec_tickers_mf as mf, security
    for name in (sec_tickers.NAME, mf.NAME, wrds.SEC_IDHIST.name, wrds.CO_HGIC.name, wrds.IDXCST_HIS.name,
                 of.NAME, fu.NAME, security.NAME):
        params = {"mode": "backfill", "to": day} if name.startswith("wrds/") else {"as_of": day}
        base.run_request({"dataset": name, "requested_by": "cli", "params": params})


@pytest.fixture
def spine(http_mock, monkeypatch):
    _spine_env(http_mock, monkeypatch, unlink_nvr=True)
    _spine()
    yield http_mock
    wrds.reset()


def _ticker_jobs(http_mock) -> list[list[tuple[str, str]]]:
    return [[(j["idType"], j["idValue"]) for j in json.loads(r.content) if j["idType"] == of.ID_TICKER]
            for r in http_mock.calls("figi.test")]


def _ticker_row(ticker: str) -> tuple:
    return of._query(f"SELECT status, figi FROM read_parquet({base.sql_path(of.ticker_partition())}) "
                     f"WHERE asked_ticker = '{ticker}'")[0]


def _asked_by_ticker(http_mock) -> set[str]:
    """Every ticker asked by a ``TICKER`` job, across the run's requests."""
    return {value for jobs in _ticker_jobs(http_mock) for _, value in jobs}


def test_a_tier_a_name_with_no_cusip_is_asked_by_ticker_and_typed_from_that_answer(spine):
    assert "NVR" in _asked_by_ticker(spine)
    assert of._query(f"SELECT status, figi, ticker FROM read_parquet({base.sql_path(of.ticker_partition())}) "
                     "WHERE asked_ticker = 'NVR'") == [("present", "BBG000BW5N90", "NVR")]
    with serving.open_read("spine") as con:
        assert con.execute("SELECT tier, cusip, figi, share_class_figi, figi_ticker, security_type, figi_source "
                           "FROM security WHERE ticker = 'NVR'").fetchone() == (
            "A", None, "BBG000BW5N90", "BBG001S5TNC0", "NVR", "common", of.FIGI_BY_TICKER)
    # It stays in scope so its answer can expire (``test_a_name_whose_figi_came_from_the_ticker_lane_stays_in_the_ask``),
    # but inside the retry window it is not asked again.
    spine.requests.clear()
    _spine("2026-11-03")
    assert "NVR" not in _asked_by_ticker(spine)


def test_a_name_that_already_has_a_figi_is_never_asked_by_ticker(spine):
    """HEI-A's CUSIP is one OpenFIGI knows, so the CUSIP lane gave it a FIGI and the ticker lane leaves it alone."""
    with serving.open_read("spine") as con:
        assert con.execute("SELECT figi FROM security WHERE ticker = 'HEI-A'").fetchone() == ("BBG000BL3Y00",)
    assert "HEI-A" not in of.wanted_tickers()
    assert "HEI-A" not in _asked_by_ticker(spine)


def test_only_tier_a_is_asked_by_ticker(spine):
    """``TICKER_TIERS`` is Tier A alone: those are the names the boards screen, and a ``TICKER`` job spends the key's
    budget. SPY and WFC-PC are Tier B, OTCX Tier C and NOEX Tier D, and none of them holds a FIGI in the fixture, so
    only the tier rule keeps them out of the ask."""
    with serving.open_read("spine") as con:
        others = con.execute("SELECT ticker, tier FROM security WHERE tier <> 'A' AND figi IS NULL").fetchall()
    assert others, "the fixture must hold a non-Tier-A name with no FIGI for this test to mean anything"
    asked = _asked_by_ticker(spine)
    assert [t for t, _ in others if t in asked] == []
    assert not {t for t, _ in others} & set(of.wanted_tickers())


def test_a_cusip_openfigi_does_not_know_is_still_asked_by_ticker(http_mock, monkeypatch):
    """The 129 Tier A names of the 2026-11-02 live spine that hold a CUSIP and still no FIGI: an 'absent'
    ``ID_CUSIP`` answer is no answer, so the ticker lane asks those names too (measured 2026-11-04: 321 Tier A
    rows with no FIGI, 192 without a CUSIP and 129 with one OpenFIGI did not answer for)."""
    _spine_env(http_mock, monkeypatch, unlink_nvr=False)           # NVR keeps its Compustat link, so it has a CUSIP
    _spine()
    with serving.open_read("spine") as con:
        row = con.execute("SELECT cusip, figi, figi_source FROM security WHERE ticker = 'NVR'").fetchone()
    assert row == ("62944T105", "BBG000BW5N90", of.FIGI_BY_TICKER)  # the CUSIP is there; the FIGI came by ticker
    assert "NVR" in _asked_by_ticker(http_mock)
    wrds.reset()


def test_a_ticker_already_answered_is_not_asked_again_until_the_retry_window(env, monkeypatch):
    monkeypatch.setattr(of, "wanted_tickers", lambda: ["NVR", "NOPE"])
    monkeypatch.setattr(of, "wanted_cusips", lambda: [])
    _run()
    assert _ticker_jobs(env) == [[(of.ID_TICKER, "NVR"), (of.ID_TICKER, "NOPE")]]
    env.requests.clear()
    _run("2027-01-24")                                      # 89 days on: still inside the window
    assert _ticker_jobs(env) == []
    _run("2027-01-26")
    # Both expire: unlike a CUSIP, a 'present' ticker answer is checked again once the window is past, because the
    # symbol may belong to another issuer by then.
    assert _ticker_jobs(env) == [[(of.ID_TICKER, "NVR"), (of.ID_TICKER, "NOPE")]]


def test_a_present_ticker_answer_is_checked_again_after_the_retry_window(env, monkeypatch):
    """A ticker is not a stable key: a symbol can move to another issuer, and ``figi_ticker_differs`` cannot catch
    it because the asked and the answered ticker are equal. So a 'present' answer expires too, and the newer answer
    replaces the row (``upsert_partition`` by ``asked_ticker``)."""
    monkeypatch.setattr(of, "wanted_tickers", lambda: ["NVR"])
    monkeypatch.setattr(of, "wanted_cusips", lambda: [])
    _run()
    assert _ticker_jobs(env) == [[(of.ID_TICKER, "NVR")]]
    assert _ticker_row("NVR") == ("present", "BBG000BW5N90")
    env.requests.clear()
    _run("2027-01-24")                                      # 89 days on: still inside the window
    assert _ticker_jobs(env) == []
    monkeypatch.setitem(fx.TICKER_FIGI, "NVR", {"figi": "BBG000NEWOWNER", "ticker": "NVR", "exchCode": "US",
                                                "securityType": "Common Stock", "name": "SOMEONE ELSE INC"})
    _run("2027-01-26")                                      # 91 days on: asked again
    assert _ticker_jobs(env) == [[(of.ID_TICKER, "NVR")]]
    assert _ticker_row("NVR") == ("present", "BBG000NEWOWNER")


def test_a_name_whose_figi_came_from_the_ticker_lane_stays_in_the_ask(spine):
    """``wanted_tickers`` keeps a row this lane filled, so the answer can expire. The row has a FIGI, so the old
    'no FIGI' rule alone would have dropped it from the ask for ever."""
    with serving.open_read("spine") as con:
        row = con.execute("SELECT figi, figi_source FROM security WHERE ticker = 'NVR'").fetchone()
    assert row == ("BBG000BW5N90", of.FIGI_BY_TICKER)
    assert "NVR" in of.wanted_tickers()                      # in scope, though it holds a FIGI
    spine.requests.clear()
    _spine("2026-11-03")                                     # a week on: inside the window, so not asked
    assert "NVR" not in _asked_by_ticker(spine)


def test_a_days_ticker_batches_already_on_disk_are_not_asked_again(env, monkeypatch):
    monkeypatch.setattr(of, "wanted_tickers", lambda: ["NVR"])
    monkeypatch.setattr(of, "wanted_cusips", lambda: [])
    raw = of.ticker_raw_dir() / "2026-10-27" / "batch-00000.json.gz"
    base.write_raw_json(of._meta(raw), {"as_of": "2026-10-27", "fetched_at": 1.0, "run_id": "r0", "jobs": 1})
    base.write_raw_json(raw, {"tickers": ["NVR"], "answers": [{"data": [fx.TICKER_FIGI["NVR"]]}]})
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


def test_the_us_listing_of_the_asked_symbol_is_the_one_stored(env, monkeypatch):
    """A ``TICKER`` job answers with every listing OpenFIGI knows for the symbol, so the entry is chosen, not taken
    first: the one whose ticker is the symbol asked AND whose ``exchCode`` is ``US``. Here the foreign listing comes
    first and carries the same ticker, so neither 'take the first' nor 'take the first with a ticker' would do."""
    monkeypatch.setattr(of, "wanted_tickers", lambda: ["NVR"])
    monkeypatch.setattr(of, "wanted_cusips", lambda: [])
    monkeypatch.setitem(fx.TICKER_FIGI, "NVR", [
        {"figi": "BBG000FOREIGN", "ticker": "NVR", "exchCode": "LN", "securityType": "Common Stock",
         "name": "NVR INC (LONDON)"},
        {"figi": "BBG000BW5N90", "ticker": "NVR", "exchCode": "US", "securityType": "Common Stock",
         "name": "NVR INC"},
    ])
    _run()
    assert of._query(f"SELECT figi, exch_code FROM read_parquet({base.sql_path(of.ticker_partition())}) "
                     "WHERE asked_ticker = 'NVR'") == [("BBG000BW5N90", "US")]


def test_a_ticker_answer_with_no_us_listing_falls_back_to_one_with_a_ticker(env, monkeypatch):
    """With no US listing the first entry that carries a ticker is stored, so a name is still typed rather than
    dropped; ``exch_code`` records what it really was."""
    monkeypatch.setattr(of, "wanted_tickers", lambda: ["NVR"])
    monkeypatch.setattr(of, "wanted_cusips", lambda: [])
    monkeypatch.setitem(fx.TICKER_FIGI, "NVR", [
        {"figi": "BBG000FOREIGN", "ticker": "NVR", "exchCode": "LN", "securityType": "Common Stock"},
    ])
    _run()
    assert of._query(f"SELECT figi, exch_code FROM read_parquet({base.sql_path(of.ticker_partition())}) "
                     "WHERE asked_ticker = 'NVR'") == [("BBG000FOREIGN", "LN")]


def test_no_spine_file_asks_no_ticker(env, monkeypatch):
    def missing(kind):
        raise serving.ServingFileMissing(kind)
    monkeypatch.setattr(of.serving, "open_read", missing)
    assert of.wanted_tickers() == []


def test_a_ticker_job_error_is_not_stored_and_is_asked_next_run():
    assert of.ticker_answer("NVR", {"error": "Invalid idValue"}) is None
    assert of.ticker_answer("NVR", {"warning": "No identifier found."}) == {"asked_ticker": "NVR",
                                                                           "status": "absent"}


def test_the_two_lanes_share_the_one_request_budget(env, monkeypatch):
    monkeypatch.setattr(of, "MAX_REQUESTS", 1)
    monkeypatch.setattr(of, "wanted_cusips", lambda: [f"{i:08d}{i % 10}" for i in range(200)])   # two requests
    monkeypatch.setattr(of, "wanted_tickers", lambda: ["NVR"])
    row = _run()
    assert row["calls"] == 1 and _ticker_jobs(env) == [[]]
    assert "100 CUSIPs left for the next run" in row["note"] and "1 tickers left for the next run" in row["note"]


def test_a_compustat_cusip_beside_a_ticker_sourced_figi_is_a_build_issue(http_mock, monkeypatch):
    """Review should-fix 4: for the 129 live Tier A rows whose CUSIP OpenFIGI does not know, the CUSIP (and the ISIN
    derived from it) comes from Compustat while the FIGI comes from a ``TICKER`` job, and nothing checks the two name
    the same security. A reorganised issuer is exactly that case, so the row is named on the board."""
    from RevenueChaser.rcdata import ops
    from RevenueChaser.rcdata.build.spine import security
    _spine_env(http_mock, monkeypatch, unlink_nvr=False)        # NVR keeps its Compustat link, so it has a CUSIP
    _spine()
    with serving.open_read("spine") as con:
        assert con.execute("SELECT cusip, figi_source FROM security WHERE ticker = 'NVR'").fetchone() == (
            "62944T105", of.FIGI_BY_TICKER)
    issues = {(i["kind"], i["key"]) for i in ops.open_issues(security.NAME, limit=500)[1]}
    assert ("figi_by_ticker_with_cusip", "NVR") in issues
    # A row whose FIGI came from its own CUSIP is not one of these.
    assert ("figi_by_ticker_with_cusip", "HEI-A") not in issues
    wrds.reset()


def test_a_ticker_openfigi_does_not_know_is_a_build_issue(http_mock, monkeypatch):
    from RevenueChaser.rcdata import ops
    from RevenueChaser.rcdata.build.spine import security
    monkeypatch.delitem(fx.TICKER_FIGI, "NVR")              # OpenFIGI knows it by neither CUSIP nor ticker
    _spine_env(http_mock, monkeypatch, unlink_nvr=True)
    _spine()
    wrds.reset()
    assert "NVR" in _asked_by_ticker(http_mock)
    issues = {(i["kind"], i["key"]) for i in ops.open_issues(security.NAME, limit=500)[1]}
    assert ("tier_a_no_figi_by_ticker", "NVR") in issues
