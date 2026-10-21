"""Sanity tests for the shared test infrastructure (root conftest)."""
import httpx


def test_golden_dir_points_at_fixtures(golden_dir):
    assert golden_dir.name == "golden"
    assert "fixtures" in golden_dir.parts


def test_mock_httpx_returns_canned(mock_httpx):
    mock_httpx("data.sec.gov", {"ok": 1})
    r = httpx.get("https://data.sec.gov/api/xbrl")
    assert r.json() == {"ok": 1}


def test_mock_httpx_defaults_to_empty_list(mock_httpx):
    r = httpx.get("https://unknown.example")
    assert r.json() == []
