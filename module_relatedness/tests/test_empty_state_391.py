"""#391, #392: an empty Relatedness board names no data date and points at no Fetch it does not have."""
from __future__ import annotations

from RevenueChaser.module_relatedness import engine
from RevenueChaser.module_relatedness.report import render_relatedness_dashboard


def test_an_empty_payload_claims_no_sync_date():
    html = render_relatedness_dashboard({"as_of": "2026-11-11T10:00:00", "n": 0, "companies": [], "peers": {}})
    assert "Last sync: no run yet" in html and "November 11, 2026" not in html


def test_a_run_with_names_shows_its_date():
    html = render_relatedness_dashboard({"as_of": "2026-11-11T10:00:00", "n": 1,
                                         "companies": [{"ticker": "A"}], "peers": {}})
    assert "Last sync: Wednesday, November 11, 2026 EOD" in html


def test_the_empty_state_names_the_off_peak_job_not_a_fetch():
    html = render_relatedness_dashboard({"n": 0, "companies": [], "peers": {}})
    assert "Run <b>Fetch</b>" not in html and "RC-Relatedness-OffPeak builds them" in html


def test_no_payload_file_has_no_data_date(monkeypatch, tmp_path):
    monkeypatch.setattr(engine.config, "RELATEDNESS_JSON", tmp_path / "missing.json")
    assert engine.load_payload()["as_of"] is None
