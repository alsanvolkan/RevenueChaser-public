"""PR 2 step 3: the queue and schedule routes. They only enqueue and read ``ops.sqlite`` (F7 A, D1); the builder is
the one process that runs a request. Every write goes through ``_local_only`` and needs the panel header."""
from __future__ import annotations

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
    with ops.connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM build_requests ORDER BY id")]


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


def test_an_attended_dataset_is_refused_with_the_command_to_run_instead(client):
    """PR 5: the WRDS datasets need a person at a terminal (login and Duo): a page cannot queue them."""
    r = client.post("/api/build/request/wrds/sec_idhist")
    assert r.status_code == 409 and "CLI" in r.get_json()["error"]
    assert not ops.ops_path().exists() or _rows() == []        # nothing queued (the route did not even open ops)


def test_the_panel_header_cannot_claim_to_be_the_cli(client):
    """Review nit: `requested_by` "cli" is what lets an attended dataset run; a header must not produce it."""
    r = client.post("/api/build/request/market_series", headers={"X-RC-Panel": "CLI "})
    assert r.status_code == 202
    assert _rows()[-1]["requested_by"] == "panel:cli"
