"""#383 C6b page-weight guard: the wire count and the verdicts, against a fake dashboard (no network)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "research" / "383_page_weight.py"

pytestmark = pytest.mark.skipif(not _SCRIPT.exists(),
                                reason=f"{_SCRIPT.name} is not in this tree")


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("page_weight_383", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake(pages: dict):
    """GET stand-in: url -> (status, bytes)."""
    def get(url):
        return pages.get(url, (404, 0))
    return get


def test_the_total_is_the_html_plus_the_payload_as_served(mod):
    get = _fake({"b/view/x": (200, 1000), "b/view/x/payload.json": (200, 300),
                 "b/view/y": (200, 500)})
    m = mod.measure("b", ["x", "y"], get=get)
    assert m["x"] == {"status": 200, "html": 1000, "payload": 300, "total": 1300}
    assert m["y"]["payload"] == 0 and m["y"]["total"] == 500, "no payload file: HTML only"


def test_a_board_past_its_baseline_fails_and_one_within_passes(mod):
    measured = {"x": {"status": 200, "html": 0, "payload": 0, "total": 100 + 10},
                "y": {"status": 200, "html": 0, "payload": 0, "total": 100 + 11}}
    rows = {r["board"]: r for r in mod.verdicts(measured, {"x": 100, "y": 100}, slack=10)}
    assert not rows["x"]["over_baseline"]
    assert rows["y"]["over_baseline"]


def test_a_board_that_does_not_load_fails(mod):
    rows = mod.verdicts({"x": {"status": 500, "html": 0, "payload": 0, "total": 0}}, {"x": 100})
    assert rows[0]["over_baseline"]


def test_the_2mb_line_is_reported_not_enforced(mod):
    big = mod.LIMIT + 1
    rows = mod.verdicts({"x": {"status": 200, "html": big, "payload": 0, "total": big}}, {"x": big})
    assert rows[0]["over_2mb"] and not rows[0]["over_baseline"]


def test_every_board_tab_has_a_baseline(mod):
    from RevenueChaser.app import dashboard
    tabs = {t["key"] for t in dashboard.TABS if t["type"] == "tab"} - {"logs", "dealer_hedge"}
    assert tabs <= set(mod.BASELINE_0510)


def test_main_records_and_exits_on_the_verdict(mod, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_get", lambda url: (200, 10))
    out = tmp_path / "b.json"
    assert mod.main(["--base", "http://h", "--baseline", str(out), "--record"]) == 0
    assert set(__import__("json").loads(out.read_text())) == set(mod.BASELINE_0510)
    monkeypatch.setattr(mod, "_get", lambda url: (200, 10 ** 6))
    assert mod.main(["--base", "http://h", "--baseline", str(out)]) == 1
