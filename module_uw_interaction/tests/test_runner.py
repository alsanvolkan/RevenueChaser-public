"""Runner-level tests for UW Interaction: payload persistence + empty messaging.

These exercise ``app.runners.run_uw_interaction`` with a fake UW client so no
network is involved. The state file location is redirected to a temp path via
monkeypatch (the runner reads the module config's ``UW_STATE_JSON``).
"""
import json

import pytest

from RevenueChaser.app import jobs
from RevenueChaser.app.runners import run_uw_interaction


class FakeUW:
    def __init__(self, body, auth_error=None):
        self.body = body
        self.auth_error = auth_error
        self.calls = []

    def raw_endpoint(self, ep, params=None):
        self.calls.append({"ep": ep, "params": params})
        return self.body


@pytest.fixture
def state_path(tmp_path, monkeypatch):
    from RevenueChaser.module_uw_interaction import config as uwcfg
    d = tmp_path / "uw_interaction"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(uwcfg, "UW_STATE_DIR", d)
    monkeypatch.setattr(uwcfg, "UW_STATE_JSON", d / "uw_interaction_latest.json")
    return uwcfg.UW_STATE_JSON


class TestRunner:
    def test_writes_payload_for_nonempty_result(self, state_path):
        uw = FakeUW({"data": [{"ticker": "AAPL", "premium": 120000}]})
        out = run_uw_interaction(uw, tool="flow_alerts")
        assert out == state_path and state_path.exists()
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        assert payload["row_count"] == 1
        assert payload["generated_at"]
        assert payload["tool_key"] == "flow_alerts"
        # The board consumes these keys.
        assert "columns" in payload and "rows" in payload

    def test_empty_result_raises_job_message(self, state_path):
        uw = FakeUW({"data": []})
        with pytest.raises(jobs.JobMessage, match="no rows"):
            run_uw_interaction(uw, tool="flow_alerts")
        # The payload is still written so the board can show the empty state.
        assert state_path.exists()
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        assert payload["row_count"] == 0

    def test_credential_rejection_message(self, state_path):
        uw = FakeUW({}, auth_error={"status": 403, "endpoint": "/x", "detail": "route_not_permitted"})
        with pytest.raises(jobs.JobMessage, match="HTTP 403"):
            run_uw_interaction(uw, tool="flow_alerts")

    def test_unknown_tool_is_an_error(self, state_path):
        uw = FakeUW({})
        with pytest.raises(jobs.JobMessage, match="Unknown tool"):
            run_uw_interaction(uw, tool="nope")
