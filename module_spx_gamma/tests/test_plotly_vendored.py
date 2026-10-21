"""SPX Gamma must not load Plotly from a CDN (Phase 5: vendored locally)."""
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE = _ROOT / "templates" / "section_position_map.html.j2"


def test_template_loads_vendored_plotly():
    assert "/static/vendor/plotly.min.js" in _TEMPLATE.read_text(encoding="utf-8")


def test_no_cdn_reference_in_pipeline():
    for rel in ("run.py", "tools/regenerate.py"):
        text = (_ROOT / rel).read_text(encoding="utf-8")
        assert "cdn" not in text, f"{rel} still references a CDN"
