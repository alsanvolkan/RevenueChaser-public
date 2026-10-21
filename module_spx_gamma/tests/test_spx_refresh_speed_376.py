"""#376 M-SPX-4: Refresh skips the PNG export; the tornado sets its annotations once."""
from __future__ import annotations

import json

import plotly.graph_objects as go

from RevenueChaser.module_spx_gamma import run as spx_run
from RevenueChaser.module_spx_gamma.charts import tornado
from RevenueChaser.module_spx_gamma.charts.tornado import (
    BG, GREEN, GRID, ORANGE, RED, TEXT, _level_entries, bar_value_and_overflow, fmt_signed_compact,
    tornado_range, tornado_rows,
)

from .conftest import make_raw, make_snapshot
from .test_s2_fetch import _no_network

DAY = "2026-08-14"


# ── the pre-#438 tornado builder, kept verbatim: it shows the bug that was fixed (add_annotation
#    in a loop, and its footer passed to update_layout collapses every annotation to one text) ──
def old_build_tornado_fig(snapshot: dict, height: int = 460) -> go.Figure | None:
    """Build a standalone tornado figure for the bottom panel."""
    rows = tornado_rows(snapshot)
    if not rows:
        return None
    xmin, xmax = tornado_range(rows)
    if xmin is None:
        return None

    strikes = [r["strike"] for r in rows]
    raw_vals = [r["net_gamma"] for r in rows]
    colors = [GREEN if v >= 0 else RED for v in raw_vals]
    cap = (xmax or 0) / 1.15
    vals = []
    overflow = []
    for v in raw_vals:
        clamped, ovf = bar_value_and_overflow(v, cap)
        vals.append(clamped)
        overflow.append(ovf)

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=vals, y=strikes, orientation="h",
        marker=dict(color=colors, line=dict(width=0)),
        customdata=[[fmt_signed_compact(v)] for v in raw_vals],
        hovertemplate="Strike %{y:,.0f}<br>Net GEX %{customdata[0]}<extra></extra>",
        showlegend=False,
    ))

    # zero baseline
    fig.add_shape(type="line", xref="x", yref="y", x0=0, x1=0,
                  y0=1.0, y1=1.0, line=dict(color="#2c3e50", width=1.4))

    # reference level lines + right-edge labels
    slo, shi = min(strikes), max(strikes)
    for e in _level_entries(snapshot, slo, shi):
        fig.add_shape(
            type="line", xref="x", yref="y", x0=1.0, x1=1.0,
            y0=e["value"], y1=e["value"],
            line=dict(color=e["color"], width=1.4, dash=e["dash"]), opacity=0.9,
        )
        fig.add_annotation(
            xref="paper", x=1.0, y=e["value"], yref="y",
            text=f"<b>{e['label']}</b>  {e['value']:,.0f}",
            xanchor="left", xshift=6, showarrow=False,
            font=dict(color=e["color"], size=10),
            bgcolor=BG, bordercolor=e["color"], borderpad=3,
        )

    # value labels at bar tips (only for prominent bars > 8% of cap)
    for s, v, raw, ovf in zip(strikes, vals, raw_vals, overflow):
        if abs(raw) < 0.08 * cap:
            continue
        label = "▸ " + fmt_signed_compact(raw) if ovf and raw > 0 else fmt_signed_compact(raw)
        label = "◂ " + fmt_signed_compact(raw) if ovf and raw < 0 else label
        fig.add_annotation(
            x=v + (0.03 * cap if v >= 0 else -0.03 * cap), y=s, text=label,
            showarrow=False, font=dict(color=TEXT, size=9),
            xanchor="left" if v >= 0 else "right",
        )

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=BG, plot_bgcolor=BG,
        font=dict(color=TEXT, size=11),
        title=dict(
            text=f"Strike-Based Signed Net GEX — {snapshot.get('asof_date', '')}",
            font=dict(size=14),
        ),
        height=height,
        margin=dict(l=60, r=220, t=50, b=40),
        xaxis=dict(
            title="Signed Net GEX", range=[xmin, xmax],
            gridcolor=GRID, zeroline=False, tickformat="~s",
        ),
        yaxis=dict(
            title="Strike", gridcolor=GRID, tickformat=",",
        ),
        hoverlabel=dict(bgcolor="#111a2c"),
        annotations=[
            dict(
                text="<span style='color:#d64550'>&larr; Negative GEX (short gamma)</span>  "
                     "<span style='color:#9c9da1'>| unit: notional</span>  "
                     "<span style='color:#1e9e63'>Positive GEX (long gamma) &rarr;</span>",
                xref="paper", yref="paper", x=0.5, y=-0.16,
                showarrow=False, font=dict(size=11),
            ),
        ],
    )
    return fig


def _wide_snapshot() -> dict:
    snap = make_snapshot()
    spot = snap["current_level"]
    # many strikes with prominent bars of both signs so level labels and bar labels both occur
    snap["strike_exposures"] = [
        {"strike": float(spot - 150 + 5 * i), "net_gamma": (1 if i % 3 else -1) * (1e8 * (1 + (i % 7)))}
        for i in range(60)
    ]
    return snap


def _overflow_snapshot() -> dict:
    """The wide snapshot with two bars far past the x-axis cap, so both overflow markers occur."""
    snap = _wide_snapshot()
    snap["strike_exposures"][0]["net_gamma"] = 5e12
    snap["strike_exposures"][1]["net_gamma"] = -6e12
    return snap


# ── #438: the footer is appended, not merged into every level and bar label ──
FOOTER_MARKERS = ("Negative GEX (short gamma)", "Positive GEX (long gamma)")
FOOTER_XY = ("paper", 0.5, -0.16, "paper")      # xref, x, y, yref


def _annotation_xy(ann) -> tuple:
    return (ann.xref, ann.x, ann.y, ann.yref)


def _footer_indices(fig) -> list[int]:
    texts = [a.text or "" for a in fig.layout.annotations]
    return [i for i, t in enumerate(texts) if all(m in t for m in FOOTER_MARKERS)]


def _level_texts(snap: dict) -> list[str]:
    """The level label texts the builder draws (label + value), for the visible strike window."""
    rows = tornado_rows(snap)
    entries = _level_entries(snap, min(r["strike"] for r in rows), max(r["strike"] for r in rows))
    return [f"<b>{e['label']}</b>  {e['value']:,.0f}" for e in entries]


def _bar_texts(snap: dict) -> list[str]:
    """The bar-tip labels the builder draws: the fmt_signed_compact value, with the overflow marker."""
    rows = tornado_rows(snap)
    _, xmax = tornado_range(rows)
    cap = (xmax or 0) / 1.15
    texts = []
    for r in rows:
        raw = r["net_gamma"]
        if abs(raw) < 0.08 * cap:
            continue
        _, overflowed = bar_value_and_overflow(raw, cap)
        text = fmt_signed_compact(raw)
        if overflowed:
            text = ("▸ " if raw > 0 else "◂ ") + text
        texts.append(text)
    return texts


def _snapshots() -> list[dict]:
    return [make_snapshot(), _wide_snapshot(), _overflow_snapshot()]


def test_tornado_level_labels_keep_their_text():
    """T1: every level label reads its own name and value, not the footer's text."""
    for snap in _snapshots():
        fig = tornado.build_tornado_fig(snap)
        assert fig is not None
        texts = [a.text or "" for a in fig.layout.annotations]
        level_texts = _level_texts(snap)
        assert len(level_texts) >= 4, "fixture must render several level labels"
        missing = [t for t in level_texts if t not in texts]
        assert missing == [], f"level labels lost their text: {missing}; got {texts[:3]}"


def test_tornado_footer_appears_once_and_last():
    """T2: one footer, after every label."""
    for snap in _snapshots():
        fig = tornado.build_tornado_fig(snap)
        assert fig is not None
        idx = _footer_indices(fig)
        assert len(idx) == 1, f"expected exactly one footer annotation, got {len(idx)}"
        assert idx == [len(fig.layout.annotations) - 1], "the footer must be the last annotation"


def test_tornado_only_the_footer_sits_at_the_footer_position():
    """T3: no level or bar label was moved to the footer's paper position."""
    for snap in _snapshots():
        fig = tornado.build_tornado_fig(snap)
        assert fig is not None
        footer = _footer_indices(fig)
        assert len(footer) == 1
        assert _annotation_xy(fig.layout.annotations[footer[0]]) == FOOTER_XY
        misplaced = [
            (i, a.text) for i, a in enumerate(fig.layout.annotations)
            if i != footer[0] and _annotation_xy(a) == FOOTER_XY
        ]
        assert misplaced == [], f"labels carry the footer position: {misplaced[:3]}"


def test_tornado_bar_tips_keep_their_text():
    """T4: the bar-tip labels still read their own fmt_signed_compact value, overflow markers included."""
    for snap in _snapshots():
        fig = tornado.build_tornado_fig(snap)
        assert fig is not None
        texts = [a.text or "" for a in fig.layout.annotations]
        bar_texts = _bar_texts(snap)
        assert len(bar_texts) >= 3, "fixture must render several bar-tip labels"
        missing = [t for t in bar_texts if t not in texts]
        assert missing == [], f"bar labels lost their text: {missing}; got {texts[:3]}"
    # the overflow fixture really does draw both markers
    overflow_texts = [a.text or "" for a in tornado.build_tornado_fig(_overflow_snapshot()).layout.annotations]
    assert "▸ +5.0T" in overflow_texts
    assert "◂ -6.0T" in overflow_texts


def test_tornado_keeps_every_label_and_adds_the_footer():
    """T5: level labels + bar labels + one footer — nothing was dropped while the text was fixed."""
    for snap in _snapshots():
        old = old_build_tornado_fig(snap)
        new = tornado.build_tornado_fig(snap)
        assert old is not None and new is not None
        level_texts, bar_texts = _level_texts(snap), _bar_texts(snap)
        assert len(new.layout.annotations) == len(level_texts) + len(bar_texts) + 1
        # the old builder drew the same label slots (the footer was merged into them, adding none)
        footer = _footer_indices(new)
        assert len(footer) == 1
        assert len(new.layout.annotations) - 1 == len(old.layout.annotations)
        # the bug the old builder carried: every annotation collapsed to the one footer text
        old_texts = {a.text for a in old.layout.annotations}
        assert len(old_texts) == 1, "old builder reference no longer reproduces #438"


def test_tornado_does_not_call_add_annotation(monkeypatch):
    def boom(self, *a, **k):
        raise AssertionError("add_annotation in a loop is quadratic")

    monkeypatch.setattr(go.Figure, "add_annotation", boom)
    assert tornado.build_tornado_fig(_wide_snapshot()) is not None


# ── Refresh skips the PNG, a Fetch still writes it ──────────────────────────
class _PngCalls:
    def __init__(self, monkeypatch):
        self.paths = []

        def fake(fig, path, *a, **k):
            self.paths.append(str(path))
            with open(path, "wb") as f:
                f.write(b"png:" + str(path).encode())

        monkeypatch.setattr(go.Figure, "write_image", lambda self, path, *a, **k: fake(self, path))



def _stored_day(root, monkeypatch):
    """A stored raw day (manifest + one raw file) whose content is the full synthetic payload,
    so all three charts exist; the loader is faked, the raw files only feed the sha256 map."""
    raw = dict(make_raw(), _meta={"effective_date": DAY})
    d = root / "raw" / DAY
    d.mkdir(parents=True)
    (d / "manifest.json").write_text("{}", encoding="utf-8")
    (d / "gex_levels.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(spx_run, "load_raw_day", lambda day_dir: raw)
    _no_network(monkeypatch)
    return raw


def _sha_map(root):
    return json.loads((root / "latest.json").read_text(encoding="utf-8"))["sha256"]


def test_refresh_writes_no_png(tmp_path, monkeypatch):
    _stored_day(tmp_path, monkeypatch)
    calls = _PngCalls(monkeypatch)
    spx_run.refresh(DAY, data_root=tmp_path)
    assert calls.paths == []
    assert not list((tmp_path / "charts" / DAY).glob("*.png"))
    assert (tmp_path / "charts" / DAY / "position_map.html").exists()


def test_fetch_build_still_writes_the_pngs(tmp_path, monkeypatch):
    raw = _stored_day(tmp_path, monkeypatch)
    calls = _PngCalls(monkeypatch)
    spx_run.build_day(raw, DAY, tmp_path)               # record=True: the Fetch path
    assert any(p.endswith("position_map.png") for p in calls.paths)
    assert (tmp_path / "charts" / DAY / "position_map.png").exists()
    assert "position_map.png" in _sha_map(tmp_path)


def test_refresh_records_no_sha256_for_a_png_it_did_not_write(tmp_path, monkeypatch):
    raw = _stored_day(tmp_path, monkeypatch)
    _PngCalls(monkeypatch)
    spx_run.build_day(raw, DAY, tmp_path)               # Fetch: png on disk and in the map
    before = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    spx_run.refresh(DAY, data_root=tmp_path)
    after = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert "position_map.png" not in after["sha256"]
    assert set(after["sha256"]) == {k for k in before["sha256"] if not k.endswith(".png")}
    # latest.json is equal to before except for the sha256 map
    assert {k: v for k, v in after.items() if k != "sha256"} == {k: v for k, v in before.items() if k != "sha256"}
    # every recorded hash matches a file on disk
    for name, digest in after["sha256"].items():
        p = (tmp_path / "raw" / DAY / name) if name.endswith(".json") else (tmp_path / "charts" / DAY / name)
        assert spx_run._sha256(p) == digest


def test_refresh_keeps_the_report_image_reference(tmp_path, monkeypatch):
    raw = _stored_day(tmp_path, monkeypatch)
    _PngCalls(monkeypatch)
    spx_run.build_day(raw, DAY, tmp_path)
    md_before = (tmp_path / "reports" / DAY / "section.md").read_text(encoding="utf-8")
    spx_run.refresh(DAY, data_root=tmp_path)
    assert (tmp_path / "reports" / DAY / "section.md").read_text(encoding="utf-8") == md_before
