"""PR 2 step 3: the queue and schedule routes. They only enqueue and read ``ops.sqlite`` (F7 A, D1); the builder is
the one process that runs a request. Every write goes through ``_local_only`` and needs the panel header."""
from __future__ import annotations

import json

import pytest

from RevenueChaser.core import calendar, config
from RevenueChaser.core.tests.panel_client import panel_test_client
from RevenueChaser.rcdata import ops


@pytest.fixture(autouse=True)
def _market(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MARKET_DIR", tmp_path / "market")
    monkeypatch.setattr(config, "MARKET_RAW_DIR", tmp_path / "market_raw")
    monkeypatch.setattr(ops, "read_log", ops.ReadLog(process="test"))


@pytest.fixture
def client():
    from RevenueChaser.app.dashboard import app
    return panel_test_client(app)


def _rows() -> list[dict]:
    return ops.rows("build_requests")


def test_a_request_enqueues_with_the_panel_header_as_requested_by(client):
    r = client.post("/api/build/request/market_series", headers={"X-RC-Panel": "settings"})
    assert r.status_code == 202, r.get_json()
    body = r.get_json()
    rows = _rows()
    assert body == {"ok": True, "id": rows[0]["id"], "dataset": "market_series"}
    assert [(x["dataset"], x["status"], x["requested_by"], x["params"]) for x in rows] == [
        ("market_series", "queued", "settings", None)]


def test_a_dataset_name_with_a_slash_is_one_dataset(client):
    assert client.post("/api/build/request/uw/spot_expiry_strike").status_code == 202
    assert _rows()[0]["dataset"] == "uw/spot_expiry_strike"


@pytest.mark.parametrize("dataset", ["alphavantage/listing_status", "sec/ncen"])
def test_the_etf_lists_datasets_take_a_fetch_and_a_backfill(client, dataset):
    """#346: Settings' Fetch and Backfill on the two halves of the ETF list go through the one route, unchanged."""
    assert client.post(f"/api/build/request/{dataset}").status_code == 202
    ops.next_request()                                         # the builder took the Fetch
    ops.finish_request(_rows()[0]["id"], "done")
    start = calendar.today_ny().replace(day=1).isoformat()
    assert client.post(f"/api/build/request/{dataset}", json={"backfill_from": start}).status_code == 202
    assert [(x["dataset"], x["params"]) for x in _rows()] == [
        (dataset, None), (dataset, '{"from": "%s", "mode": "backfill"}' % start)]


def test_a_request_without_the_panel_header_is_refused_and_enqueues_nothing():
    from RevenueChaser.app.dashboard import app
    r = app.test_client().post("/api/build/request/market_series")
    assert r.status_code == 403
    assert not ops.ops_path().exists() or _rows() == []


def test_a_second_request_while_one_is_queued_or_running_is_409(client):
    assert client.post("/api/build/request/market_series").status_code == 202
    r = client.post("/api/build/request/market_series")
    assert r.status_code == 409 and "already queued" in r.get_json()["error"]
    ops.next_request()                                         # the builder took it: running
    assert client.post("/api/build/request/market_series").status_code == 409
    assert client.post("/api/build/request/uw/spot_expiry_strike").status_code == 202   # another dataset
    assert len(_rows()) == 2


def test_an_unknown_dataset_is_400_and_enqueues_nothing(client):
    r = client.post("/api/build/request/nope")
    assert r.status_code == 400 and "unknown dataset" in r.get_json()["error"]
    assert client.post("/api/build/schedule/nope/pause").status_code == 400
    assert not ops.ops_path().exists() or _rows() == []


@pytest.mark.parametrize("value", ["2026-13-01", "yesterday", "2026-10-27T00:00", "", "2999-01-01"])
def test_backfill_from_must_be_a_past_iso_date(client, value):
    r = client.post("/api/build/request/market_series", json={"backfill_from": value})
    assert r.status_code == 400 and "backfill_from" in r.get_json()["error"]
    assert not ops.ops_path().exists() or _rows() == []


def test_backfill_from_queues_a_backfill_request(client):
    start = calendar.today_ny().replace(day=1).isoformat()
    r = client.post("/api/build/request/uw/spot_expiry_strike", json={"backfill_from": start})
    assert r.status_code == 202
    assert _rows()[0]["params"] == '{"from": "%s", "mode": "backfill"}' % start
    q = client.get("/api/build/queue").get_json()
    assert q["active"][0]["params"] == {"from": start, "mode": "backfill"}


def test_the_queue_lists_active_rows_and_the_last_20_finished(client):
    for _ in range(22):
        rid = ops.enqueue("market_series", None, by="t")
        ops.next_request()
        ops.finish_request(rid, "done", note="ok")
    queued = ops.enqueue("market_series", None, by="t")
    body = client.get("/api/build/queue").get_json()
    assert body["ok"] is True
    assert [r["id"] for r in body["active"]] == [queued]
    assert len(body["finished"]) == 20 and body["finished"][0]["id"] == queued - 1      # newest first


def test_pause_and_resume_write_the_schedule_row(client):
    r = client.post("/api/build/schedule/market_series/pause", headers={"X-RC-Panel": "settings"})
    assert r.status_code == 200 and r.get_json()["schedule"]["paused_by"] == "settings"
    row = ops.schedule_get("market_series")
    assert row["paused_at"] is not None and row["paused_by"] == "settings"
    r = client.post("/api/build/schedule/market_series/resume")
    assert r.status_code == 200
    row = ops.schedule_get("market_series")
    assert row["paused_at"] is None and row["paused_by"] is None


def test_an_unknown_schedule_action_is_404(client):
    assert client.post("/api/build/schedule/market_series/drop").status_code == 404


def test_a_paused_dataset_is_skipped_by_one_builder_pass_and_runs_again_after_resume(client, monkeypatch):
    from RevenueChaser.rcdata import build
    from RevenueChaser.rcdata.build import DATASETS
    from RevenueChaser.rcdata.build import __main__ as cli
    # The two market datasets only: the spine's (PR 5) would be queued as never built and call their providers.
    monkeypatch.setattr(build, "DATASETS", {k: DATASETS[k] for k in ("market_series", "uw/spot_expiry_strike")})
    assert client.post("/api/build/schedule/market_series/pause").status_code == 200
    # `run --once` also queues every never-built dataset (#223's due step): the other one is paused too, so this
    # pass sends no call at all.
    assert client.post("/api/build/schedule/uw/spot_expiry_strike/pause").status_code == 200
    assert client.post("/api/build/request/market_series").status_code == 202
    fetched = []
    monkeypatch.setattr(DATASETS["market_series"], "fetch", lambda ctx, as_of: fetched.append(as_of) or [])
    assert cli.main(["run", "--once"]) == 0
    check = ops.latest_check("market_series")
    assert check["result"] == "skipped" and check["note"].startswith("skipped: paused")
    assert check["calls"] == 0 and fetched == []
    assert _rows()[-1]["status"] == "skipped"
    assert client.post("/api/build/schedule/market_series/resume").status_code == 200
    assert client.post("/api/build/request/market_series").status_code == 202
    cli.main(["run", "--once"])
    assert fetched, "a resumed dataset runs"
    assert not (ops.latest_check("market_series")["note"] or "").startswith("skipped: paused")


def test_the_routes_never_open_a_duckdb_file(client, monkeypatch):
    import duckdb
    opened = []
    real = duckdb.connect
    monkeypatch.setattr(duckdb, "connect", lambda *a, **k: opened.append(a) or real(*a, **k))
    client.post("/api/build/request/market_series")
    client.get("/api/build/queue")
    client.post("/api/build/schedule/market_series/pause")
    assert [a for a in opened if a and "market" in str(a[0])] == []


def test_an_attended_dataset_is_queued_and_waits_for_the_runner_on_the_server(client):
    """PR 5 refused it with 409. It is queued now and waits: the WRDS login and Duo still happen at a terminal,
    so only the attended runner on the server takes it out of the queue.

    A press with no window is the dataset's own daily run (``params`` None), not a backfill: every WRDS dataset
    has a ``fetch`` of its own, and for ``wrds/prices`` that fetch is the monthly refresh of the last two years
    while its backfill only fills missing partitions (review MUST-1).
    """
    r = client.post("/api/build/request/wrds/sec_idhist")
    assert r.status_code == 202, r.get_json()
    body = r.get_json()
    assert body["ok"] is True and body["attended"] is True
    assert "build attended" in body["waits_for"]
    rows = _rows()
    assert [(x["dataset"], x["status"], x["requested_by"], x["params"]) for x in rows] == [
        ("wrds/sec_idhist", "queued", "test", None)]


def test_an_attended_request_keeps_the_window_the_press_asked_for(client):
    r = client.post("/api/build/request/wrds/sec_idhist?backfill_from=2026-09-01")
    assert r.status_code == 202, r.get_json()
    assert _rows()[-1]["params"] == '{"from": "2026-09-01", "mode": "backfill"}'


def test_the_panel_header_cannot_claim_to_be_the_cli(client):
    """Review nit: `requested_by` "cli" is what lets an attended dataset run; a header must not produce it."""
    r = client.post("/api/build/request/market_series", headers={"X-RC-Panel": "CLI "})
    assert r.status_code == 202
    assert _rows()[-1]["requested_by"] == "panel:cli"


# ── The scope a Fetch press may carry (#265) ───────────────────────────────────────────────────────────────────

def _params(client) -> dict:
    return client.get("/api/build/queue").get_json()["active"][0]["params"]


def test_the_route_carries_the_scope_and_the_date_range_into_the_queue_row(client):
    start = calendar.today_ny().replace(day=1)
    r = client.post("/api/build/request/alpaca/bars",
                    json={"tiers": ["A", "B"], "names": ["ko"], "backfill_from": start.isoformat(),
                          "backfill_to": start.isoformat()})
    assert r.status_code == 202, r.get_json()
    assert _params(client) == {"mode": "backfill", "from": start.isoformat(), "to": start.isoformat(),
                               "tiers": ["A", "B"], "names": ["KO"]}


def test_a_scope_with_no_date_range_is_a_daily_request(client):
    assert client.post("/api/build/request/alpaca/bars", json={"tiers": ["B"]}).status_code == 202
    assert _params(client) == {"tiers": ["B"]}


def test_a_comma_separated_scope_is_the_same_as_a_list(client):
    """What a link in the page can carry: ``?tiers=a,b&names=ko``. The builder's own reader normalises both."""
    assert client.post("/api/build/request/alpaca/bars?tiers=a,b&names=ko,ko").status_code == 202
    assert _params(client) == {"tiers": ["A", "B"], "names": ["KO"]}


def test_the_extra_groups_are_carried_too(client):
    """Karar 82: a narrowed fill asks only what it names, so the benchmarks, the watch list and the cover classes
    are groups a request names for itself."""
    assert client.post("/api/build/request/alpaca/bars", json={"include": ["Anchors"]}).status_code == 202
    assert _params(client) == {"include": ["anchors"]}


def test_an_unknown_group_is_refused_at_the_route(client):
    r = client.post("/api/build/request/alpaca/bars", json={"include": ["etfs"]})
    assert r.status_code == 400 and "include" in r.get_json()["error"]
    assert not ops.ops_path().exists() or _rows() == []


@pytest.mark.parametrize("body, word", [
    ({"tiers": ["Z"]}, "tiers"),
    ({"tiers": {"A": 1}}, "tiers"),
    ({"names": ["KO", ""]}, "names"),
    ({"names": 7}, "names"),
    ({"names": [f"T{i}" for i in range(20_001)]}, "names"),
    ({"backfill_to": "2026-10-10"}, "backfill_from"),
    ({"backfill_from": "2026-10-10", "backfill_to": "2026-10-01"}, "backfill_to"),
])
def test_a_request_the_builder_could_not_run_is_refused_at_the_route(client, body, word):
    r = client.post("/api/build/request/alpaca/bars", json=body)
    assert r.status_code == 400 and word in r.get_json()["error"]
    assert not ops.ops_path().exists() or _rows() == [], "nothing was queued"


# ── The cadence and the hold (the Fetch section's schedule fields) ─────────────────────────────────────────────
#
# ``ops.schedule_set`` and ``due.py`` have read the cadence and ``next_due`` since PR 2, but nothing called the
# setter: a dataset's frequency could only be changed by editing the code. These are the route behind the two
# fields on the dataset card.

def _sched(dataset: str = "market_series") -> dict:
    return ops.schedule_get(dataset) or {}


def test_the_cadence_action_sets_the_cadence_and_the_hold(client):
    r = client.post("/api/build/schedule/market_series/cadence",
                    json={"cadence": "weekly", "next_due": "2026-11-10"},
                    headers={"X-RC-Panel": "settings"})
    assert r.status_code == 200, r.get_json()
    row = _sched()
    assert row.get("cadence") == "weekly" and str(row.get("next_due"))[:10] == "2026-11-10"
    assert r.get_json()["schedule"]["cadence"] == "weekly"


def test_a_cadence_the_builder_does_not_know_is_refused_before_the_store_is_touched(client):
    r = client.post("/api/build/schedule/market_series/cadence", json={"cadence": "yearly"},
                    headers={"X-RC-Panel": "settings"})
    assert r.status_code == 400 and "cadence" in r.get_json()["error"]
    assert _sched().get("cadence") is None, "a refused cadence writes nothing"


def test_a_hold_that_is_not_a_date_is_refused(client):
    r = client.post("/api/build/schedule/market_series/cadence",
                    json={"cadence": "daily", "next_due": "soon"}, headers={"X-RC-Panel": "settings"})
    assert r.status_code == 400 and "next_due" in r.get_json()["error"]
    assert _sched().get("cadence") is None


def test_an_empty_hold_clears_the_one_that_was_there(client):
    client.post("/api/build/schedule/market_series/cadence",
                json={"cadence": "monthly", "next_due": "2026-12-01"}, headers={"X-RC-Panel": "settings"})
    r = client.post("/api/build/schedule/market_series/cadence", json={"cadence": "monthly", "next_due": ""},
                    headers={"X-RC-Panel": "settings"})
    assert r.status_code == 200, r.get_json()
    row = _sched()
    assert row.get("cadence") == "monthly" and row.get("next_due") is None


def test_the_cadence_action_needs_a_dataset_the_builder_knows(client):
    r = client.post("/api/build/schedule/not/a/dataset/cadence", json={"cadence": "daily"},
                    headers={"X-RC-Panel": "settings"})
    assert r.status_code == 400 and "unknown dataset" in r.get_json()["error"]


def test_the_cadence_action_keeps_a_pause_as_it_is(client):
    """Pause and the cadence are separate: setting a frequency must not resume a dataset a person paused."""
    client.post("/api/build/schedule/market_series/pause", headers={"X-RC-Panel": "settings"})
    client.post("/api/build/schedule/market_series/cadence", json={"cadence": "weekly"},
                headers={"X-RC-Panel": "settings"})
    row = _sched()
    assert row.get("paused_at") is not None and row.get("cadence") == "weekly"


def test_pause_cancels_the_queued_request_and_says_how_many(client):
    """#492 PR (ii): a queued request of a paused dataset never runs."""
    from RevenueChaser.rcdata import ops
    rid = client.post("/api/build/request/market_series", headers={"X-RC-Panel": "settings"}).get_json()["id"]
    r = client.post("/api/build/schedule/market_series/pause", headers={"X-RC-Panel": "settings"})
    assert r.status_code == 200 and r.get_json()["cancelled"] == 1
    row = next(x for x in ops.rows("build_requests") if x["id"] == rid)
    assert row["status"] == "skipped" and row["note"].startswith("cancelled: paused by settings at ")
    again = client.post("/api/build/schedule/market_series/pause", headers={"X-RC-Panel": "settings"})
    assert again.get_json()["cancelled"] == 0
    assert client.post("/api/build/schedule/market_series/resume",
                       headers={"X-RC-Panel": "settings"}).get_json()["cancelled"] == 0


@pytest.mark.parametrize("stage,params", [("raw", {"no_parquet": True}), ("parquet", {"no_duckdb": True}),
                                          ("full", None)])
def test_a_fetch_stage_becomes_the_builders_switch(client, stage, params):
    """#492 PR (ii): Fetch raw, Fetch raw + Parquet, or the whole run."""
    r = client.post("/api/build/request/market_series", json={"stage": stage}, headers={"X-RC-Panel": "settings"})
    assert r.status_code == 202, r.get_json()
    row = next(x for x in _rows() if x["id"] == r.get_json()["id"])
    assert (json.loads(row["params"]) if row["params"] else None) == params


def test_an_unknown_stage_is_refused_and_queues_nothing(client):
    r = client.post("/api/build/request/market_series", json={"stage": "duckdb-only"},
                    headers={"X-RC-Panel": "settings"})
    assert r.status_code == 400 and "unknown stage" in r.get_json()["error"] and _rows() == []


def test_automation_sets_a_kinds_mode_and_refuses_an_unknown_kind_or_mode(client):
    r = client.post("/api/build/automation/market", json={"mode": "raw"}, headers={"X-RC-Panel": "settings"})
    assert r.status_code == 200 and r.get_json()["automation"]["mode"] == "raw"
    assert ops.kind_mode_get("market")["set_by"] == "settings"
    assert client.post("/api/build/automation/nope", json={"mode": "raw"}).status_code == 400
    assert client.post("/api/build/automation/market", json={"mode": "everything"}).status_code == 400
    assert ops.kind_mode("market") == "raw"


def test_switch_turns_fetch_or_backup_off_and_on_and_refuses_anything_else(client):
    """#527: the board's two switches. The panel header is who turned it off."""
    h = {"X-RC-Panel": "settings"}
    r = client.post("/api/build/switch/fetch", json={"on": False}, headers=h)
    assert r.status_code == 200 and r.get_json()["switch"]["off_by"] == "settings"
    assert ops.switch_off("fetch") is not None and ops.switch_off("backup") is None
    assert client.post("/api/build/switch/fetch", json={"on": True}, headers=h).status_code == 200
    assert ops.switch_off("fetch") is None
    assert client.post("/api/build/switch/everything", json={"on": False}, headers=h).status_code == 400
    assert client.post("/api/build/switch/backup", json={}, headers=h).status_code == 400, "on must be a boolean"


def test_a_sources_automation_is_set_or_dropped_and_refuses_an_unknown_dataset_or_mode(client):
    """#527: a source's own automation; ``file`` goes back to the file's."""
    h = {"X-RC-Panel": "settings"}
    r = client.post("/api/build/automation-ds/market_series", json={"mode": "raw"}, headers=h)
    assert r.status_code == 200 and r.get_json()["automation"]["mode"] == "raw"
    assert ops.read_dataset_modes()["market_series"]["set_by"] == "settings"
    assert client.post("/api/build/automation-ds/market_series", json={"mode": "file"}, headers=h).status_code == 200
    assert ops.read_dataset_modes() == {}
    assert client.post("/api/build/automation-ds/no/such", json={"mode": "raw"}, headers=h).status_code == 400
    assert client.post("/api/build/automation-ds/market_series", json={"mode": "x"}, headers=h).status_code == 400


def test_the_builder_timer_switch_runs_only_life_ctl_on_or_off(client, monkeypatch):
    """#527 step 11 (avo chose "Dashboard'a sudo izni"): the board's press runs exactly
    ``sudo -n /usr/local/sbin/life-ctl life-rc-builder.timer on|off`` and nothing else; a refusal is the answer."""
    import subprocess
    from RevenueChaser.app import dashboard
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "timer on\n", "")
    monkeypatch.setattr(dashboard.subprocess, "run", fake_run)
    h = {"X-RC-Panel": "settings"}
    r = client.post("/api/build/timer", json={"on": True}, headers=h)
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert seen == [["sudo", "-n", "/usr/local/sbin/life-ctl", "life-rc-builder.timer", "on"]]
    client.post("/api/build/timer", json={"on": False}, headers=h)
    assert seen[-1][-1] == "off"
    assert client.post("/api/build/timer", json={"on": "yes"}, headers=h).status_code == 400
    assert len(seen) == 2, "a bad body runs nothing"

    def refused(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", 'sudo: The "no new privileges" flag is set')
    monkeypatch.setattr(dashboard.subprocess, "run", refused)
    r = client.post("/api/build/timer", json={"on": True}, headers=h)
    assert r.status_code == 502 and "no new privileges" in r.get_json()["error"]


def test_reading_the_queue_closes_a_request_whose_run_was_killed(client):
    """#602: a SIGKILLed or OOM-killed run left its request running; the board's read closes it."""
    import subprocess
    import sys
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    rid = ops.enqueue("market_series", None, by="t")
    ops.next_request()
    ops.start_run("killed", "market_series", "queue")
    [run] = [r for r in ops.rows("job_runs2") if r["run_id"] == "killed"]
    ops.put_rows("job_runs2", [{**run, "pid": p.pid}])
    body = client.get("/api/build/queue").get_json()
    assert body["active"] == []
    assert body["finished"][0]["id"] == rid and body["finished"][0]["status"] == "failed"
