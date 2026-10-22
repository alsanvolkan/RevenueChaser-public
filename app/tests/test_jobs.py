"""Tests for the in-process job queue (submit, sequential worker, statuses)."""
import threading
import time

import pytest

from RevenueChaser.app import jobs


class _FakeUW:
    @property
    def active(self):
        return True

    def verify(self):
        return True, None


@pytest.fixture(autouse=True)
def _keep_registrations(monkeypatch):
    """``_reset`` clears the runner registry; give it copies so later test files keep dashboard.py's jobs."""
    for name in ("_RUNNERS", "_PRODUCES", "_REQUIRES_UW", "_REQUIRES", "_LANE"):
        monkeypatch.setattr(jobs, name, dict(getattr(jobs, name)))


def _reset():
    jobs._JOB_QUEUE.queue.clear()
    with jobs._LOCK:
        jobs._JOBS.clear()
    jobs._RUNNERS.clear()
    jobs._PRODUCES.clear()


def _wait_until(job_id, statuses, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        j = jobs.get(job_id)
        if j and j["status"] in statuses:
            return j
        time.sleep(0.02)
    return jobs.get(job_id)


class TestJobQueue:
    def test_submit_and_complete(self, monkeypatch):
        _reset()
        monkeypatch.setattr(jobs, "UWClient", lambda: _FakeUW())
        results = []
        jobs.register("t", lambda uw, **p: results.append(p) or "x", ["tab"])
        jid = jobs.submit("t", {"a": 1})
        j = _wait_until(jid, ("done", "error"))
        assert j["status"] == "done"
        assert results == [{"a": 1}]
        assert j["produces"] == ["tab"]

    def test_error_status(self, monkeypatch):
        _reset()
        monkeypatch.setattr(jobs, "UWClient", lambda: _FakeUW())
        def boom(uw, **p):
            raise RuntimeError("kaput")
        jobs.register("e", boom, ["tab"])
        jid = jobs.submit("e")
        j = _wait_until(jid, ("done", "error"))
        assert j["status"] == "error"
        assert "kaput" in j["error"]

    def test_sequential_execution(self, monkeypatch):
        _reset()
        monkeypatch.setattr(jobs, "UWClient", lambda: _FakeUW())
        order = []
        def slow(uw, **p):
            time.sleep(0.1)
            order.append(p["n"])
            return None
        def fast(uw, **p):
            order.append(p["n"])
            return None
        jobs.register("slow", slow, ["s"])
        jobs.register("fast", fast, ["f"])
        j1 = jobs.submit("slow", {"n": 1})
        j2 = jobs.submit("fast", {"n": 2})
        _wait_until(j2, ("done", "error"))
        # fast only ran after slow completed -> order preserved
        assert order == [1, 2]

    def test_unknown_module_error(self):
        _reset()
        jid = jobs.submit("nope")
        j = _wait_until(jid, ("done", "error"))
        assert j["status"] == "error"
        assert "unknown module" in j["error"]

    def test_list_jobs_and_get(self, monkeypatch):
        _reset()
        monkeypatch.setattr(jobs, "UWClient", lambda: _FakeUW())
        jobs.register("t", lambda uw, **p: None, ["tab"])
        jid = jobs.submit("t")
        _wait_until(jid, ("done", "error"))
        assert jobs.get(jid)["module"] == "t"
        assert any(j["id"] == jid for j in jobs.list_jobs())
        assert jobs.get("missing") is None


class TestForecastFreezeNotes:
    """#128: a freeze reports through thread-local notes that ``execute`` puts in the job's own ``notes`` field.

    Not in ``message``: the dashboard opens a modal ``alert()`` for every finished job that has a message
    (``templates/dashboard.html``, ``poll``), and a freeze note is not news worth a popup after every Fetch.
    """

    @staticmethod
    def _sync(monkeypatch):
        _reset()
        monkeypatch.setattr(jobs, "_ensure_worker", lambda: None)   # the test thread runs execute()
        monkeypatch.setattr(jobs, "_ensure_long_worker", lambda: None)

    def test_current_job_id_is_the_running_job_and_none_outside(self, monkeypatch):
        self._sync(monkeypatch)
        seen = []
        jobs.register("t_id", lambda uw, **p: seen.append(jobs.current_job_id()), [], requires_uw=False)
        jid = jobs.submit("t_id")
        assert jobs.current_job_id() is None
        jobs.execute(jid)
        assert seen == [jid]
        assert jobs.current_job_id() is None

    def test_current_job_id_is_cleared_after_a_failing_job(self, monkeypatch):
        self._sync(monkeypatch)

        def boom(uw, **p):
            raise RuntimeError("kaput")

        jobs.register("t_boom", boom, [], requires_uw=False)
        jobs.execute(jobs.submit("t_boom"))
        assert jobs.current_job_id() is None

    def test_a_universe_style_job_notes_name_all_three_families(self, monkeypatch):
        from RevenueChaser.core import ledger
        self._sync(monkeypatch)

        def universe(uw, **p):
            ledger.add_note("Froze 4,251 AG rows for 2026-10-02.")
            ledger.add_note("Swing not frozen: the 2026-10-02 bars were cached before the close + 30 min.")
            ledger.add_note("Froze 200 Fundamentals rows for 2026-10-02.")

        jobs.register("t_uni", universe, [], requires_uw=False)
        jid = jobs.submit("t_uni")
        jobs.execute(jid)
        job = jobs.get(jid)
        assert job["status"] == "done"
        assert job["message"] is None            # a message is what the dashboard pops up
        assert job["notes"] == [
            "Froze 4,251 AG rows for 2026-10-02.",
            "Swing not frozen: the 2026-10-02 bars were cached before the close + 30 min.",
            "Froze 200 Fundamentals rows for 2026-10-02."]

    def test_a_note_left_by_an_earlier_job_does_not_leak_into_the_next(self, monkeypatch):
        from RevenueChaser.core import ledger
        self._sync(monkeypatch)
        ledger.add_note("stale note from a job that died")          # same thread, as a worker would be
        jobs.register("t_quiet", lambda uw, **p: None, [], requires_uw=False)
        jid = jobs.submit("t_quiet")
        jobs.execute(jid)
        assert jobs.get(jid)["message"] is None
        assert jobs.get(jid)["notes"] == []
        assert ledger.take_notes() == []

    def test_notes_stay_apart_from_a_job_message_signal(self, monkeypatch):
        from RevenueChaser.core import ledger
        self._sync(monkeypatch)

        def signal(uw, **p):
            ledger.add_note("Froze 200 Swing rows for 2026-10-02.")
            raise jobs.JobMessage("Nothing new to fetch.")

        jobs.register("t_sig", signal, [], requires_uw=False)
        jid = jobs.submit("t_sig")
        jobs.execute(jid)
        job = jobs.get(jid)
        assert job["status"] == "done"
        assert job["message"] == "Nothing new to fetch."
        assert job["notes"] == ["Froze 200 Swing rows for 2026-10-02."]

    def test_notes_stay_apart_from_the_provider_gap_message(self, monkeypatch):
        from RevenueChaser.core import ledger
        self._sync(monkeypatch)

        class _UW:
            def verify(self):
                return True, None

            def health(self):
                return {"failed": 1, "calls": 4, "failures": [{"endpoint": "/x", "status": 500}]}

        monkeypatch.setattr(jobs, "UWClient", _UW)
        jobs.register("t_gap", lambda uw, **p: ledger.add_note("Froze 1 Swing rows for 2026-10-02."), [])
        jid = jobs.submit("t_gap")
        jobs.execute(jid)
        job = jobs.get(jid)
        assert job["message"].startswith("Finished with gaps") and "Froze" not in job["message"]
        assert job["notes"] == ["Froze 1 Swing rows for 2026-10-02."]

    def test_a_job_without_notes_keeps_a_plain_done(self, monkeypatch):
        self._sync(monkeypatch)
        jobs.register("t_plain", lambda uw, **p: None, [], requires_uw=False)
        jid = jobs.submit("t_plain")
        jobs.execute(jid)
        assert jobs.get(jid)["status"] == "done" and jobs.get(jid)["message"] is None
        assert jobs.get(jid)["notes"] == []

    def test_notes_of_a_failed_job_do_not_leak_into_the_next(self, monkeypatch):
        from RevenueChaser.core import ledger
        self._sync(monkeypatch)

        def boom(uw, **p):
            ledger.add_note("Froze 200 Swing rows for 2026-10-02.")
            raise RuntimeError("later step failed")

        jobs.register("t_f", boom, [], requires_uw=False)
        jobs.register("t_ok", lambda uw, **p: None, [], requires_uw=False)
        jobs.execute(jobs.submit("t_f"))
        jid = jobs.submit("t_ok")
        jobs.execute(jid)
        assert jobs.get(jid)["message"] is None and jobs.get(jid)["notes"] == []

    def test_a_freeze_note_survives_a_later_step_that_raises(self, monkeypatch):
        """AG froze 4,251 rows, then Swing raised: the job is an error, and the note is not lost."""
        from RevenueChaser.core import ledger
        self._sync(monkeypatch)

        def universe(uw, **p):
            ledger.add_note("Froze 4,251 AG rows for 2026-10-02.")
            raise RuntimeError("swing fetch failed")

        jobs.register("t_uni_err", universe, [], requires_uw=False)
        jid = jobs.submit("t_uni_err")
        jobs.execute(jid)
        job = jobs.get(jid)
        assert job["status"] == "error"
        assert job["message"] is None
        assert job["notes"] == ["Froze 4,251 AG rows for 2026-10-02."]
        # The dashboard shows only ``error`` for a failed job (``alert('Run failed: ' + j.error)``), so the
        # freeze note has to be in it, or the user is told the run failed and never that AG was frozen.
        assert job["error"] == "swing fetch failed (before the error: Froze 4,251 AG rows for 2026-10-02.)"

    def test_every_note_of_a_failed_job_is_in_the_error_text(self, monkeypatch):
        from RevenueChaser.core import ledger
        self._sync(monkeypatch)

        def universe(uw, **p):
            ledger.add_note("Froze 4,251 AG rows for 2026-10-02.")
            ledger.add_note("Swing not frozen: 1 of 190 picks lack a complete 2026-10-02 daily bar.")
            raise RuntimeError("fundamentals fetch failed")

        jobs.register("t_uni_err2", universe, [], requires_uw=False)
        jid = jobs.submit("t_uni_err2")
        jobs.execute(jid)
        err = jobs.get(jid)["error"]
        assert err.startswith("fundamentals fetch failed")
        assert "Froze 4,251 AG rows" in err and "Swing not frozen" in err

    def test_a_failed_job_without_notes_keeps_the_bare_error_text(self, monkeypatch):
        self._sync(monkeypatch)

        def boom(uw, **p):
            raise RuntimeError("kaput")

        jobs.register("t_bare_err", boom, [], requires_uw=False)
        jid = jobs.submit("t_bare_err")
        jobs.execute(jid)
        job = jobs.get(jid)
        assert job["status"] == "error" and job["error"] == "kaput"
        assert job["message"] is None

