"""Backend plan S1–S4 (.hermes/plans_claude/archive/2026-09-29_backend.md).

S1 local-only guard · S2 stale-server kill only for our own dashboard ·
S3 queue never drops queued jobs, one worker · S4 UW client only for UW jobs.
"""
from __future__ import annotations

import threading

import pytest

from RevenueChaser.app import dashboard, jobs
from RevenueChaser.core.tests.open_issues_shell import strip_open_issues_data
from RevenueChaser.core.tests.panel_client import panel_test_client


@pytest.fixture
def client():
    return panel_test_client(dashboard.app)


# ── S1: B1 — only the local machine, only same-origin writes ────────────────
# ``client`` is the panel-header client (core/tests/panel_client.py), so these writes
# fail on the rule they name and not on the "panel header required" 403 (#231); each
# one asserts the error text for that reason.
class TestLocalOnly:
    def test_a_cross_site_post_is_refused(self, client, monkeypatch):
        monkeypatch.setattr(dashboard.jobs, "submit", lambda *a, **k: "should-not-run")
        r = client.post("/run/module_sec_13f-check", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403 and r.get_json()["error"] == "cross-site request refused"

    def test_sec_fetch_site_cross_site_is_refused(self, client):
        r = client.post("/api/watchlist/toggle/PWNED", headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403 and r.get_json()["error"] == "cross-site request refused"

    def test_an_opaque_null_origin_is_refused(self, client):
        r = client.post("/api/watchlist/toggle/X", headers={"Origin": "null"})
        assert r.status_code == 403 and r.get_json()["error"] == "cross-site request refused"

    def test_a_foreign_host_is_refused_even_for_reads(self, client):
        assert client.get("/api/jobs", headers={"Host": "attacker.example:8000"}).status_code == 403

    @pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "localhost", "[::1]:8000"])
    def test_loopback_hosts_pass(self, client, host):
        assert client.get("/api/jobs", headers={"Host": host}).status_code == 200

    def test_same_origin_post_passes(self, client, monkeypatch, rawstore_tmp_db):
        r = client.post("/api/watchlist/toggle/AAPL",
                        headers={"Origin": "http://127.0.0.1:8000", "Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 200

    def test_a_post_without_origin_but_with_the_panel_header_passes(self, client, rawstore_tmp_db):
        """Scripts / the e2e harness send no Origin; browsers always send it cross-site."""
        assert client.post("/api/watchlist/toggle/AAPL",
                           headers={"X-RC-Panel": "cli"}).status_code == 200


# ── S2: B2 — never kill a program that isn't our dashboard ──────────────────
class TestStaleServerKill:
    def _listener(self, monkeypatch, pid=4242):
        out = f"  TCP    127.0.0.1:8000    0.0.0.0:0    LISTENING    {pid}\n"
        monkeypatch.setattr(dashboard.sys, "platform", "win32")
        monkeypatch.setattr(dashboard.subprocess, "check_output", lambda *a, **k: out)
        killed = []
        monkeypatch.setattr(dashboard.os, "kill", lambda p, s: killed.append(p))
        return killed

    def test_a_foreign_program_is_left_running(self, monkeypatch):
        killed = self._listener(monkeypatch)
        monkeypatch.setattr(dashboard, "_is_revenuechaser", lambda port: False)
        with pytest.raises(dashboard.PortInUseError, match="another program"):
            dashboard._kill_stale_server(8000)
        assert killed == []

    def test_a_stale_revenuechaser_dashboard_is_killed(self, monkeypatch):
        killed = self._listener(monkeypatch)
        monkeypatch.setattr(dashboard, "_is_revenuechaser", lambda port: True)
        dashboard._kill_stale_server(8000)
        assert killed == [4242]

    def test_manifest_probe_recognises_the_dashboard(self, monkeypatch):
        import io, json, urllib.request

        class _Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        body = json.dumps({"short_name": "REVENUE CHASER"}).encode()
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp(body))
        assert dashboard._is_revenuechaser(8000) is True
        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("refused")))
        assert dashboard._is_revenuechaser(8000) is False


# ── S3: B3/B11 — queue integrity ────────────────────────────────────────────
@pytest.fixture
def fresh_queue(monkeypatch):
    import queue
    monkeypatch.setattr(jobs, "_JOB_QUEUE", queue.Queue())
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_SEQ", {})
    monkeypatch.setattr(jobs, "_WORKER_STARTED", False)
    import types
    started = []
    # Replace only jobs' view of the threading module, so the test's own threads stay real.
    monkeypatch.setattr(jobs, "threading", types.SimpleNamespace(
        Thread=lambda *a, **k: type("T", (), {"start": lambda self: started.append(1)})(),
        Lock=threading.Lock))
    return started


class TestQueueIntegrity:
    def test_queued_jobs_are_never_evicted(self, fresh_queue, monkeypatch):
        monkeypatch.setattr(jobs, "MAX_HISTORY", 5)
        jobs.register("t_mark", lambda uw, **k: None, [], requires_uw=False)
        ids = [jobs.submit("t_mark") for _ in range(12)]
        assert all(jobs.get(i) is not None for i in ids)

    def test_finished_jobs_are_trimmed_oldest_first(self, fresh_queue, monkeypatch):
        monkeypatch.setattr(jobs, "MAX_HISTORY", 3)
        jobs.register("t_mark", lambda uw, **k: None, [], requires_uw=False)
        ids = [jobs.submit("t_mark") for _ in range(5)]
        for i in ids:
            jobs.execute(i)
        jobs.submit("t_mark")  # triggers a trim
        remaining = [i for i in ids if jobs.get(i) is not None]
        assert remaining == ids[-2:]

    def test_concurrent_first_submits_start_one_worker(self, fresh_queue):
        jobs.register("t_mark", lambda uw, **k: None, [], requires_uw=False)
        barrier = threading.Barrier(8)

        def go():
            barrier.wait()
            jobs.submit("t_mark")

        threads = [threading.Thread(target=go) for _ in range(8)]  # real threads (captured before patch)
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(fresh_queue) == 1

    def test_a_job_runs_at_most_once(self, fresh_queue):
        runs = []
        jobs.register("t_once", lambda uw, **k: runs.append(1), [], requires_uw=False)
        jid = jobs.submit("t_once")
        jobs.execute(jid)
        jobs.execute(jid)  # e.g. the worker picks up an id the harness already ran
        assert runs == [1]


# ── S4: B5 — UW client only when needed, closed afterwards ──────────────────
class TestUWClientUse:
    def test_non_uw_job_builds_no_client(self, fresh_queue, monkeypatch):
        built = []
        monkeypatch.setattr(jobs, "UWClient", lambda: built.append(1))
        seen = []
        jobs.register("t_nouw", lambda uw, **k: seen.append(uw), [], requires_uw=False)
        jobs.execute(jobs.submit("t_nouw"))
        assert built == [] and seen == [None]

    def test_uw_job_client_is_closed(self, fresh_queue, monkeypatch):
        closed = []

        class _UW:
            def verify(self): return True, None
            def close(self): closed.append(1)

        monkeypatch.setattr(jobs, "UWClient", _UW)
        jobs.register("t_uw", lambda uw, **k: None, [], requires_uw=True)
        jobs.execute(jobs.submit("t_uw"))
        assert closed == [1]

    def test_a_successful_verify_is_reused_across_clients(self, monkeypatch):
        from RevenueChaser.core.providers import unusual_whales as uwmod
        calls = []

        class _Resp:
            status_code = 200
            text = ""

        monkeypatch.setattr(uwmod, "_VERIFIED_OK", {})
        a = uwmod.UWClient(key="k1")
        monkeypatch.setattr(a._client, "get", lambda *x, **k: calls.append(1) or _Resp())
        assert a.verify() == (True, None)
        b = uwmod.UWClient(key="k1")
        monkeypatch.setattr(b._client, "get", lambda *x, **k: calls.append(1) or _Resp())
        assert b.verify() == (True, None)
        assert calls == [1]
        a.close(); b.close()

    def test_a_rejected_key_is_never_cached(self, monkeypatch):
        from RevenueChaser.core.providers import unusual_whales as uwmod

        class _Resp:
            status_code = 401
            text = "no"

        monkeypatch.setattr(uwmod, "_VERIFIED_OK", {})
        c = uwmod.UWClient(key="bad")
        monkeypatch.setattr(c._client, "get", lambda *x, **k: _Resp())
        assert c.verify()[0] is False
        assert "bad" not in uwmod._VERIFIED_OK
        c.close()


# ── S5: B4/B10/B16 — Refresh rewrites, atomic writes, explicit empty 13F ────
class TestArtifacts:
    def test_write_artifact_is_atomic_and_leaves_no_temp(self, tmp_path):
        from RevenueChaser.core.reporting_common import write_artifact
        p = tmp_path / "x" / "board_latest.html"
        write_artifact(p, "one")
        write_artifact(p, "two")
        assert p.read_text(encoding="utf-8") == "two"
        assert [q.name for q in p.parent.iterdir()] == ["board_latest.html"]

    def test_empty_13f_writes_an_empty_board_and_a_message(self, rawstore_tmp_db):
        from RevenueChaser.app import runners
        runners.SEC_13F_LATEST.unlink(missing_ok=True)
        with pytest.raises(jobs.JobMessage, match="No 13F totals"):
            runners.run_sec_13f_latest(None)
        assert runners.SEC_13F_LATEST.exists()

    def test_13f_refresh_rewrites_the_artifact(self, client, rawstore_tmp_db):
        from RevenueChaser.app import runners
        runners.SEC_13F_LATEST.write_text("<html>old</html>", encoding="utf-8")
        r = client.get("/view/module_sec_13f?refresh=1")
        assert r.status_code == 200
        assert runners.SEC_13F_LATEST.exists()
        assert "old" not in runners.SEC_13F_LATEST.read_text(encoding="utf-8")


# ── S7: B12/B13 ─────────────────────────────────────────────────────────────
class TestSmallRoutes:
    def test_static_files_are_cacheable(self, client):
        r = client.get("/static/icons/icon-192.png")
        assert r.status_code == 200
        assert "no-store" not in (r.headers.get("Cache-Control") or "")

    def test_html_views_stay_no_store(self, client):
        assert "no-store" in client.get("/").headers.get("Cache-Control", "")

    def test_bad_rotation_date_is_rejected_before_the_key_check(self, client, monkeypatch):
        monkeypatch.setattr(dashboard, "UWClient",
                            lambda: pytest.fail("the key must not be probed for bad input"))
        r = client.post("/run/rotation", data={"start": "not-a-date"})
        assert r.status_code == 400 and r.get_json()["error"] == "bad date"


# ── S8: B8 — two lanes and cancel ───────────────────────────────────────────
class TestLanesAndCancel:
    def test_long_jobs_use_the_long_lane(self):
        for m in ("module_sec_13f", "universe", "edgar_sync", "holdings"):
            assert jobs._LANE.get(m) == "long", m
        assert jobs._LANE.get("module_leaps") == "short"

    def test_a_long_job_does_not_block_a_short_one(self, monkeypatch):
        import queue, time
        monkeypatch.setattr(jobs, "_JOB_QUEUE", queue.Queue())
        monkeypatch.setattr(jobs, "_LONG_QUEUE", queue.Queue())
        monkeypatch.setattr(jobs, "_WORKER_STARTED", False)
        monkeypatch.setattr(jobs, "_LONG_STARTED", False)
        release = threading.Event()
        jobs.register("t_long", lambda uw, **k: release.wait(10), [], requires_uw=False, lane="long")
        jobs.register("t_short", lambda uw, **k: None, [], requires_uw=False)
        long_id = jobs.submit("t_long")
        short_id = jobs.submit("t_short")
        deadline = time.time() + 5
        while jobs.get(short_id)["status"] != "done" and time.time() < deadline:
            time.sleep(0.02)
        assert jobs.get(short_id)["status"] == "done"
        assert jobs.get(long_id)["status"] == "running"
        release.set()

    def test_cancel_a_queued_job(self, fresh_queue, client):
        ran = []
        jobs.register("t_c", lambda uw, **k: ran.append(1), [], requires_uw=False)
        jid = jobs.submit("t_c")
        r = client.post(f"/api/job/{jid}/cancel")
        assert r.status_code == 200 and r.get_json()["job"]["status"] == "cancelled"
        jobs.execute(jid)
        assert ran == []
        assert client.post(f"/api/job/{jid}/cancel").status_code == 409
        assert client.post("/api/job/nope/cancel").status_code == 404


# ── S9: B9 — holdings view is store-only ────────────────────────────────────
class TestHoldingsView:
    def test_no_network_and_no_key_needed(self, client, rawstore_tmp_db, monkeypatch):
        from RevenueChaser.core import rawstore
        monkeypatch.setattr(dashboard, "UWClient", lambda: pytest.fail("no UW in a GET view"))
        rawstore.write_rows("etf_holdings", [{"ticker": "SMH", "entry": [
            {"ticker": "NVDA", "name": "NVIDIA <b>", "weight": 18.0}]}])
        body = client.get("/view/module_sector_rotation/holdings?ticker=SMH").get_data(as_text=True)
        assert "NVDA" in body and "—" in body
        assert "NVIDIA <b>" not in body  # escaped

    def test_old_rs_rows_are_still_shown(self, client, rawstore_tmp_db):
        from RevenueChaser.core import rawstore
        rawstore.write_rows("rotation_holdings", [{"ticker": "SMH", "date": "2020-01-01", "rows": [
            {"ticker": "AMD", "name": "AMD", "weight": 5.0, "close": 100.0,
             "rs_1w": 1.0, "rs_1m": 2.0, "rs_3m": 3.0}]}])
        body = client.get("/view/module_sector_rotation/holdings?ticker=SMH").get_data(as_text=True)
        assert "AMD" in body and "+3.0%" in body

    def test_nothing_stored_says_fetch_holdings(self, client, rawstore_tmp_db):
        body = client.get("/view/module_sector_rotation/holdings?ticker=SMH").get_data(as_text=True)
        assert "Fetch holdings" in body


# ── S10: decisions D1–D4 ────────────────────────────────────────────────────
class TestDecisions:
    def test_relatedness_fetch_route_is_gone(self, client):
        assert "module_relatedness" not in jobs._RUNNERS
        # panel_test_client sends the header: without it the guard answers 403 before routing.
        assert client.post("/run/module_relatedness").status_code == 404

    def test_sec13f_fetch_is_the_ingest_job(self):
        from RevenueChaser.app import runners
        assert jobs._RUNNERS["module_sec_13f"] is runners.build_sec13f
        assert jobs._REQUIRES_UW["module_sec_13f"] is False  # #206: the SEC ingest needs no UW key

    def test_rotation_fetch_queues_money_flow(self, client, monkeypatch):
        class _OK:
            def verify(self): return True, None
        monkeypatch.setattr(dashboard, "UWClient", lambda: _OK())
        seen = []
        monkeypatch.setattr(dashboard.jobs, "submit", lambda m, p=None: seen.append(m) or f"id-{m}")
        r = client.post("/run/rotation")
        assert r.status_code == 202
        assert seen == ["rotation", "moneyflow"]

    def test_template_wiring(self, client):
        html = strip_open_issues_data(client.get("/").get_data(as_text=True))
        assert "key==='module_comovement'" in html          # D1 Fetch button
        assert "module_relatedness: '/run/module_relatedness'" not in html  # D2
        assert "btn-rot-holdings" in html                    # D3
        assert "rc-job-submitted" in html and "/cancel" in html  # B7, B8
        assert "t && !t.textContent ||" not in html          # B15
