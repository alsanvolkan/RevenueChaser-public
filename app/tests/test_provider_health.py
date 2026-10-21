"""MB3 / ER-1 — provider gaps must reach the job message and the board.

PL1 made ``UWClient`` count its final failures (``health()``). A job whose
runner finished while some of those calls failed is still "done", but the UI
must say the board may be incomplete, and each UW-backed board shows an amber
banner instead of passing for a quiet market.
"""
from __future__ import annotations

import time
from datetime import date

import pytest

from RevenueChaser.app import jobs, runners
from RevenueChaser.app.jobs import JobMessage
from RevenueChaser.core.reporting_common import provider_health_html


@pytest.fixture(autouse=True)
def _keep_registrations(monkeypatch):
    """``_reset`` clears the runner registry; give it copies so later test files keep dashboard.py's jobs (a test that
    imports the dashboard after this file found ``sec13f_map`` missing, R5 full test)."""
    for name in ("_RUNNERS", "_PRODUCES", "_REQUIRES_UW", "_LANE"):
        monkeypatch.setattr(jobs, name, dict(getattr(jobs, name)))


def _health(failed=0, calls=10, auth_error=None, failures=None):
    if failures is None:
        failures = [{"endpoint": "/stock/XYZ/ohlc", "status": 429, "error": "HTTP 429",
                     "attempts": 3}] if failed else []
    return {"calls": calls, "failed": failed, "failures": failures, "auth_error": auth_error}


class _FakeUW:
    def __init__(self, health=None, auth_error=None):
        self._health = health or _health()
        self.auth_error = auth_error

    @property
    def active(self):
        return True

    def verify(self):
        return True, None

    def health(self):
        return self._health

    def close(self):
        pass


# ── job-level warning ─────────────────────────────────────────────────────────

def _reset():
    jobs._JOB_QUEUE.queue.clear()
    with jobs._LOCK:
        jobs._JOBS.clear()
    jobs._RUNNERS.clear()
    jobs._PRODUCES.clear()


def _run(monkeypatch, uw, runner=lambda uw, **p: None):
    _reset()
    monkeypatch.setattr(jobs, "UWClient", lambda: uw)
    jobs.register("t", runner, ["tab"])
    jid = jobs.submit("t")
    end = time.time() + 5
    while time.time() < end:
        j = jobs.get(jid)
        if j and j["status"] in ("done", "error"):
            return j
        time.sleep(0.02)
    return jobs.get(jid)


class TestJobWarning:
    def test_gaps_finish_done_with_a_warning(self, monkeypatch):
        j = _run(monkeypatch, _FakeUW(_health(failed=12, calls=340)))
        assert j["status"] == "done"
        msg = j["message"]
        assert msg.startswith("Finished with gaps: 12 of 340 Unusual Whales calls failed")
        assert "/stock/XYZ/ohlc: 429" in msg
        assert "run Fetch again later" in msg
        assert j["provider_health"]["failed"] == 12

    def test_an_open_breaker_is_named_in_the_message(self, monkeypatch):
        # #166: after BREAKER_THRESHOLD final failures in a row the client stops sending; the job
        # message must say so and how many calls were never sent, not read like 8 failed requests.
        h = _health(failed=8, calls=8, failures=[
            {"endpoint": "/e/1", "status": 503, "error": "HTTP 503", "attempts": 3}])
        h.update({"breaker_open": True, "skipped_calls": 3})
        j = _run(monkeypatch, _FakeUW(h))
        assert j["status"] == "done"
        msg = j["message"]
        assert "stopped sending requests" in msg
        assert "3 calls were never sent" in msg
        assert "run Fetch again later" in msg

    def test_a_closed_breaker_adds_nothing(self, monkeypatch):
        h = _health(failed=2, calls=10)
        h.update({"breaker_open": False, "skipped_calls": 0})
        j = _run(monkeypatch, _FakeUW(h))
        assert "stopped sending" not in j["message"] and "never sent" not in j["message"]

    def test_clean_run_has_no_message(self, monkeypatch):
        j = _run(monkeypatch, _FakeUW(_health(failed=0)))
        assert j["status"] == "done"
        assert j["message"] is None

    def test_runner_message_is_kept_and_the_gap_appended(self, monkeypatch):
        def runner(uw, **p):
            raise JobMessage("No earnings events scheduled for 2026-09-30.")
        j = _run(monkeypatch, _FakeUW(_health(failed=2, calls=4)), runner)
        assert j["status"] == "done"
        assert j["message"].startswith("No earnings events scheduled")
        assert "2 of 4 Unusual Whales calls failed" in j["message"]

    def test_a_transport_failure_names_the_error_kind(self, monkeypatch):
        h = _health(failed=1, calls=1, failures=[
            {"endpoint": "/x", "status": None, "error": "ConnectTimeout", "attempts": 3}])
        j = _run(monkeypatch, _FakeUW(h))
        assert "/x: ConnectTimeout" in j["message"]

    def test_client_without_health_is_tolerated(self, monkeypatch):
        class _Old(_FakeUW):
            health = None
        j = _run(monkeypatch, _Old())
        assert j["status"] == "done" and j["message"] is None


# ── the shared banner ─────────────────────────────────────────────────────────

class TestProviderHealthHtml:
    def test_empty_for_a_clean_run_or_no_health(self):
        assert provider_health_html(None) == ""
        assert provider_health_html({}) == ""
        assert provider_health_html(_health(failed=0)) == ""

    def test_shows_counts_and_example(self):
        out = provider_health_html(_health(failed=3, calls=50))
        assert 'role="alert"' in out
        assert "3 of 50" in out
        assert "/stock/XYZ/ohlc" in out and "429" in out

    def test_auth_error_alone_shows_the_banner(self):
        out = provider_health_html(_health(failed=0, auth_error={"status": 401, "endpoint": "/a"}))
        assert "401" in out and "key" in out.lower()

    def test_escapes_provider_text(self):
        h = _health(failed=1, failures=[
            {"endpoint": "/stock/<script>alert(1)</script>/x", "status": None,
             "error": "<b>boom</b>", "attempts": 3}])
        out = provider_health_html(h)
        assert "<script>" not in out and "<b>boom" not in out
        assert "&lt;script&gt;" in out


# ── boards ────────────────────────────────────────────────────────────────────

_BAD = _health(failed=5, calls=40)


class TestEarningsBoard:
    def test_runner_passes_health_and_the_board_shows_the_banner(self, tmp_path, monkeypatch):
        # EARNINGS_LATEST stays under the (session-temp) OUTPUT_DIR; the capture
        # below redirects the actual write into tmp_path.
        path = tmp_path / "earnings_radar_latest.html"
        monkeypatch.setattr(runners, "_fetch_calendar_range", lambda uw, s, e: (
            [{"ticker": "AAA", "earnings_date": date(2026, 9, 30), "earnings_time": "BMO"}],
            {}))

        class _Pipe:
            def __init__(self, *a, **kw):
                self._filter_stats = {}

            def run(self, use_preselect=True):
                return []

        monkeypatch.setattr(runners, "EarningsPipeline", _Pipe)
        import RevenueChaser.module_sector_rotation.engine as rot
        monkeypatch.setattr(rot, "rotation_quadrant_map", lambda uw: {})
        from RevenueChaser.module_earnings_radar import report
        seen = {}
        real = report.export_html

        def _capture(*a, **kw):
            seen.update(kw)
            kw["filename"] = str(path)
            return real(*a, **kw)

        monkeypatch.setattr(report, "export_html", _capture)
        with pytest.raises(JobMessage) as exc:   # 0 cards: the board is written, the job says why (#198 E23)
            runners.run_earnings_range(_FakeUW(_BAD), date(2026, 9, 30), date(2026, 9, 30))
        assert "5 of 40" in exc.value.message
        assert seen["provider_health"]["failed"] == 5
        html = path.read_text(encoding="utf-8")
        assert "rc-provider-health" in html and "5 of 40" in html

    def test_clean_run_has_no_banner(self, tmp_path):
        from RevenueChaser.module_earnings_radar.report import export_html
        p = export_html([], [], {}, filename=str(tmp_path / "e.html"),
                        provider_health=_health(failed=0))
        assert "rc-provider-health" not in p.read_text(encoding="utf-8")

    def test_empty_calendar_with_rejected_key_says_so(self, tmp_path, monkeypatch):
        """ER-1: a dead key must not read as "no earnings scheduled"."""
        path = tmp_path / "earnings_radar_latest.html"
        monkeypatch.setattr(runners, "EARNINGS_LATEST", path)
        monkeypatch.setattr(runners, "_fetch_calendar_range", lambda uw, s, e: ([], {}))
        uw = _FakeUW(_health(failed=2, auth_error={"status": 401, "endpoint": "/e"}),
                     auth_error={"status": 401, "endpoint": "/e"})
        with pytest.raises(JobMessage) as exc:
            runners.run_earnings_range(uw, date(2026, 9, 30), date(2026, 9, 30))
        assert "401" in exc.value.message
        assert "No earnings events" not in exc.value.message
        html = path.read_text(encoding="utf-8")
        assert "No earnings events" not in html
        assert "401" in html

    def test_empty_calendar_after_outage_is_not_no_earnings(self, tmp_path, monkeypatch):
        path = tmp_path / "earnings_radar_latest.html"
        monkeypatch.setattr(runners, "EARNINGS_LATEST", path)
        monkeypatch.setattr(runners, "_fetch_calendar_range", lambda uw, s, e: ([], {}))
        with pytest.raises(JobMessage) as exc:
            runners.run_earnings_range(_FakeUW(_BAD), date(2026, 9, 30), date(2026, 9, 30))
        assert "calendar" in exc.value.message.lower()
        assert "No earnings events" not in exc.value.message
        assert "No earnings events" not in path.read_text(encoding="utf-8")


class TestRotationBoard:
    def test_runner_passes_health_and_the_board_shows_the_banner(self, tmp_path, monkeypatch):
        import RevenueChaser.module_sector_rotation.engine as eng
        import RevenueChaser.module_sector_rotation.report as rep

        class _Eng:
            def __init__(self, uw, benchmark="SPY"):
                pass

            def process_all(self):
                return []

        monkeypatch.setattr(eng, "RotationEngine", _Eng)
        seen = {}
        real = rep.export_rotation_dashboard
        out = tmp_path / "rot.html"

        def _capture(snapshots, **kw):
            seen.update(kw)
            kw["filename"] = str(out)
            return real(snapshots, **kw)

        monkeypatch.setattr(rep, "export_rotation_dashboard", _capture)
        # No board yet, so the empty pull writes the empty map with the banner (ROT-5: an existing board is kept).
        monkeypatch.setattr(runners, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(runners, "ROTATION_LATEST", tmp_path / "sector_rotation_latest.html")
        with pytest.raises(JobMessage, match="5 of 40"):
            runners.run_rotation_latest(_FakeUW(_BAD))
        assert seen["provider_health"]["failed"] == 5
        html = out.read_text(encoding="utf-8")
        assert "rc-provider-health" in html and "5 of 40" in html


class TestPayloadBoards:
    def test_oipd_payload_carries_health_and_renders_the_banner(self, monkeypatch):
        from RevenueChaser.module_oipd import engine, report
        monkeypatch.setattr(engine, "run", lambda **kw: {"ticker": "AAA", "expiries": [{"x": 1}]})
        seen = {}
        monkeypatch.setattr(runners, "_render_oipd", lambda p: seen.update(p) or None)
        runners.run_oipd(_FakeUW(_BAD), ticker="AAA")
        assert seen["provider_health"]["failed"] == 5
        html = report.render_oipd_dashboard({"ticker": "AAA", "provider_health": _BAD})
        assert "rc-provider-health" in html

    def test_uw_interaction_renders_the_banner(self):
        from RevenueChaser.module_uw_interaction.report import render_board
        html = render_board({"label": "t", "rows": [], "row_count": 0, "provider_health": _BAD,
                             "generated_at": "2026-09-30T00:00:00+00:00"})
        assert "rc-provider-health" in html
        assert "rc-provider-health" not in render_board(
            {"label": "t", "rows": [], "row_count": 0, "provider_health": _health()})

    def test_leaps_renders_the_banner(self, tmp_path):
        from RevenueChaser.module_leaps.report import render_leaps_dashboard
        p = render_leaps_dashboard([], [], filename=str(tmp_path / "l.html"), watchlist=set(),
                                   provider_health=_BAD)
        assert "rc-provider-health" in p.read_text(encoding="utf-8")
