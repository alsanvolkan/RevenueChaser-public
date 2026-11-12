"""LEAPS board, end to end: the board stays but the scan is switched off (#730).

Until 12 Nov 2026 this file drove the whole fetch -> store -> restart -> refresh path; the board was the
first one wired into the harness. The scan is now off and the board waits on a measurement (avo: "Kalsin
board ama nasil olcum yapacagimiz bulalim"): the job is not registered and ``POST /run/module_leaps``
answers 409, so there is no fetch to drive. What is left is the refusal, and the page the tab serves
instead of the board.

The filter bar's tier behaviour in a real browser went with the board (the kit itself is covered by
``core/tests/test_rc_filters*.py`` and by four boards that are still served).
"""
from __future__ import annotations



class TestScanIsSwitchedOff:
    def test_the_dashboard_serves_its_shell(self, dashboard):
        """Sanity: the subprocess really is the app, not a stub."""
        r = dashboard.get("/")
        assert r.status_code == 200
        assert "Earnings Radar" in r.text

    def test_the_fetch_route_refuses_with_a_reason(self, dashboard):
        r = dashboard.post_fetch("/run/module_leaps")
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["ok"] is False
        assert "switched off" in body["error"] and "#730" in body["error"]

    def test_the_refusal_starts_no_job_and_calls_no_provider(self, dashboard, provider_routes):
        before = provider_routes.request_count
        dashboard.post_fetch("/run/module_leaps")
        dashboard.wait_until_idle()
        assert provider_routes.request_count == before
        assert dashboard.table_count("leaps_flow_prints") == 0
        assert dashboard.table_count("leaps_cache") == 0

    def test_the_tab_serves_the_no_data_page(self, dashboard):
        """No scan can run, so the tab says so -- 200 with a reason, not a 500 and not the board."""
        r = dashboard.view("module_leaps")
        assert r.status_code == 200
        assert "No data yet" in r.text and "#730" in r.text
        assert "ACCUMULATION RADAR" not in r.text.upper()   # the board itself is never served

    def test_the_board_has_no_payload(self, dashboard):
        assert dashboard.get("/view/module_leaps/payload.json").status_code == 404

    def test_the_nav_keeps_the_tab_under_its_own_name(self, dashboard):
        page = dashboard.get("/").text
        assert ">LEAPS<" in page or "LEAPS" in page
        assert "LEAPS (off)" not in page
