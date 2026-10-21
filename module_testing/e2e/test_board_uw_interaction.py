"""UW Interaction (lab) board, end to end: Run a tool → payload → restart → view (#215).

The lab writes no ``.store`` table: ``run_uw_interaction`` saves one regenerable JSON under ``.cache/``
and the page renders from it. So the invariant is artifact + zero-network view + empty state; there is no
store step and no ``data_asof`` key to assert (the board has neither).
"""
from __future__ import annotations

import json

import pytest

TOOL = {"tool": "option_trades", "min_premium": "10000", "limit": "50"}


@pytest.fixture
def ran(dashboard):
    result = dashboard.run_job("/run/uw", **TOOL)
    assert result.ok, f"UW tool run failed: {result}\n{dashboard.logs()}"
    return result


def _payload(dashboard):
    return json.loads(dashboard.artifact("uw_interaction/uw_interaction_latest.json").read_text(encoding="utf-8"))


class TestRun:
    def test_the_run_writes_the_payload_with_rows(self, dashboard, ran):
        p = _payload(dashboard)
        assert p["tool_key"] == "option_trades" and p["row_count"] > 0 and not p["error"]

    def test_the_run_writes_no_store_table(self, dashboard, ran):
        """The lab is cache-only: no board header lands in history.duckdb."""
        assert dashboard.table_count("result_runs") == 0

    def test_the_board_shows_the_rows(self, dashboard, ran):
        r = dashboard.view("module_uw_interaction")
        assert r.status_code == 200 and "NVDA" in r.text

    def test_the_run_actually_called_the_provider(self, provider_routes, ran):
        assert provider_routes.requests("/api/option-trades")

    def test_an_unknown_tool_is_refused_with_a_message_not_a_500(self, dashboard):
        result = dashboard.run_job("/run/uw", tool="no_such_tool")
        assert result.message, result            # a ToolError becomes the job's message
        assert not _no_payload(dashboard).exists()

    def test_a_missing_tool_is_a_400(self, dashboard):
        assert dashboard.post_fetch("/run/uw").status_code == 400

    def test_a_provider_outage_is_named_not_an_empty_success(self, dashboard, provider_routes):
        provider_routes.route("/api/option-trades", payload={"error": "boom"}, status=500)
        result = dashboard.run_job("/run/uw", **TOOL)
        assert "request failed" in (result.message or "") or "returned nothing" in (result.message or ""), result


def _no_payload(dashboard):
    return dashboard.artifact("uw_interaction/uw_interaction_latest.json")


class TestViewIsOffline:
    def test_view_makes_no_network_calls(self, dashboard, provider_routes, ran):
        before = provider_routes.request_count
        r = dashboard.view("module_uw_interaction", refresh=True)
        assert r.status_code == 200 and "NVDA" in r.text
        assert provider_routes.request_count == before, (
            f"view reached the network: {[q.path for q in provider_routes.requests()[before:]]}")

    def test_view_survives_a_restart(self, dashboard, ran):
        dashboard.restart()
        assert "NVDA" in dashboard.view("module_uw_interaction").text

    def test_a_rejected_key_does_not_break_viewing_the_board(self, dashboard, ran, dead_uw_token):
        dashboard.restart()
        r = dashboard.view("module_uw_interaction")
        assert r.status_code == 200 and "NVDA" in r.text


class TestEmptyStore:
    def test_a_board_with_no_run_renders_the_idle_state(self, dashboard):
        r = dashboard.view("module_uw_interaction")
        assert r.status_code == 200 and "Pick a tool" in r.text
