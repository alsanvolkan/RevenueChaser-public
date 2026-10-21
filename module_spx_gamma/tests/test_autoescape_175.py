"""The SPX position map escapes its context values (G15, #175).

``render_html`` used to run Jinja with ``autoescape=False``, so a provider string in the
snapshot (``ticker``, ``source``, a candle source) reached the page as markup. HTML now
renders with autoescape on; the three Plotly fragments are the only trusted markup and
are marked ``Markup`` in ``render_html``. The Markdown template stays unescaped (it is
not HTML).

Autoescape must change nothing for normal data. ``golden/position_map.base.html`` and
``golden/position_map.base.md`` are the output of the pre-change renderer for
``make_snapshot()`` with the fixed chart fragments below. The HTML is compared as a
sequence of tags, attributes and text nodes after entity decoding (what a browser shows),
the Markdown byte for byte.
"""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest

from RevenueChaser.module_spx_gamma import render, run
from .conftest import make_snapshot

GOLDEN = Path(__file__).resolve().parent / "golden"
CHART = '<div id="gex-chart"><b>chart &amp; more</b><script>var a = 1 < 2;</script></div>'
TORNADO = '<div id="tornado"><svg><text>t &lt; 5</text></svg></div>'
ENVELOPE = '<div id="env"><i>envelope</i></div>'


@pytest.fixture(autouse=True)
def _no_local_state(monkeypatch):
    """History and the Brier scorecard are files; the golden was cut without them."""
    monkeypatch.setattr(run, "_load_history", lambda: {})
    monkeypatch.setattr(render, "scorecard", lambda: {
        "average": None, "n": 0, "verdict": "No scored weeks yet", "calibrated": None, "records": []})


def _snapshot(**over) -> dict:
    snap = make_snapshot()
    snap["api_snapshot_time"] = "2026-08-14T21:00:00Z"   # normalize_snapshot stamps "now"
    snap.update(over)
    return snap


def _html(snap=None) -> str:
    return render.render_html(snap or _snapshot(), chart_png="chart.png", chart_html=CHART,
                              tornado_html=TORNADO, envelope_html=ENVELOPE)


class _Sequence(HTMLParser):
    """Tags with attributes, text nodes and comments, in document order."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[list] = []

    def handle_starttag(self, tag, attrs):
        self.out.append(["<", tag, [[k, v] for k, v in attrs]])

    def handle_endtag(self, tag):
        self.out.append([">", tag])

    def handle_data(self, data):
        if self.out and self.out[-1][0] == "#":
            self.out[-1][1] += data
        else:
            self.out.append(["#", data])

    def handle_comment(self, data):
        self.out.append(["!", data])


def sequence(html: str) -> list[list]:
    parser = _Sequence()
    parser.feed(html)
    parser.close()
    return parser.out


def test_normal_data_renders_the_same_tags_and_text_as_before():
    # Two additions came after the golden, so they are taken out first: the shared help box
    # (#383 C3) and the data-quality banner (M-SPX-5, #376) — the fixture's reduced raw day
    # leaves max pain, VIX and CFTC unavailable, so that banner is present.
    html = _html()
    assert html.count(render.HELP + "\n") == 1
    html = html.replace(render.HELP + "\n", "")
    banner = render.data_quality_notice(_snapshot())
    if banner:
        html = html.replace(render.notice_html(banner), "")
    got = sequence(html)
    want = sequence((GOLDEN / "position_map.base.html").read_text(encoding="utf-8"))
    assert len(got) == len(want)
    for i, (g, w) in enumerate(zip(got, want)):
        assert g == w, f"item {i} differs:\n  now:  {g}\n  base: {w}"


def test_the_three_plotly_fragments_stay_markup():
    html = _html()
    for fragment in (CHART, TORNADO, ENVELOPE):
        assert fragment in html


def test_markdown_is_byte_identical_to_base():
    md = render.render_markdown(_snapshot(), chart_png="chart.png")
    assert md == (GOLDEN / "position_map.base.md").read_text(encoding="utf-8")


def test_a_hostile_snapshot_string_is_text_in_the_html():
    payload = '<img src=x onerror=__x(1)>"\'</script>'
    snap = _snapshot(ticker=payload, candle_source=payload)
    html = _html(snap)
    assert "<img src=x onerror" not in html
    assert "&lt;img src=x onerror=__x(1)&gt;" in html


def test_a_hostile_chart_path_cannot_leave_its_attribute():
    html = render.render_html(_snapshot(), chart_png='x"><img src=y onerror=__x(2)>')
    assert "<img src=y onerror" not in html


def test_the_markdown_template_is_not_escaped():
    """It is not HTML: ``&`` and ``<`` in a ticker must reach the file as written."""
    assert "A&B<C>" in render.render_markdown(_snapshot(ticker="A&B<C>"))
