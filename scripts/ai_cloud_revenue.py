"""AI & Cloud Data Platform — quarterly revenue analysis + QoQ plot.

Aggregates quarterly ``total_revenue`` for a fixed basket of AI / cloud-data
platform names (ORCL, SNOW, MDB, DDOG, MSFT, ESTC), aligns each company's fiscal
quarter to the calendar quarter its period ends in (their fiscal years differ:
May / Jun / Jan / Apr / Dec), then plots the segment revenue (stacked bars) with
its quarter-over-quarter % change (secondary-axis line).

Data source: Unusual Whales ``/api/stock/{ticker}/income-statements`` — the only
provider here that returns fiscal Q4 as a discrete quarter. SEC EDGAR
(``edgar_financials``) files Q1-Q3 only (Q4 lives inside the 10-K), so its quarterly
series is missing every fiscal year-end quarter.

Run from the OpenCode root::

    python -m RevenueChaser.scripts.ai_cloud_revenue

or from the RevenueChaser root::

    python scripts/ai_cloud_revenue.py

Outputs (regenerable, ``.cache/`` via ``RC_CACHE_DIR``): ``ai_cloud_revenue.png``
and ``ai_cloud_revenue.json``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

if __package__ in (None, ""):
    # Allow ``python scripts/ai_cloud_revenue.py`` from the RevenueChaser root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import matplotlib

matplotlib.use("Agg")  # headless-safe: never needs a display
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from RevenueChaser.core.config import OUTPUT_DIR, to_float  # noqa: E402
from RevenueChaser.core.providers.unusual_whales import UWClient  # noqa: E402

SEGMENT = "AI & Cloud Data Platform"
TICKERS = ["ORCL", "SNOW", "MDB", "DDOG", "MSFT", "ESTC"]

# Collect one prior year so 2025 QoQ/YoY have their comparison quarter, but only
# display from 2025 as requested.
COLLECT_START = "2024-01-01"
DISPLAY_START = "2025Q1"

BG = "#111625"
FG = "#e8ecf5"
MUTED = "#8b94a8"
ACCENT = "#f5a623"

PALETTE = ["#4e79a7", "#59a14f", "#e15759", "#b07aa1", "#76b7b2", "#edc948"]


def quarter_label(period_end: str) -> str | None:
    """Calendar-quarter label (``YYYYQn``) for a fiscal period-end date.

    Buckets by the month the period ends in (1-3 -> Q1 ... 10-12 -> Q4), which
    is how the mixed fiscal calendars are made comparable. Returns ``None`` for a
    malformed date rather than guessing a quarter.
    """
    parts = (period_end or "")[:10].split("-")
    if len(parts) != 3:
        return None
    year, month = parts[0], parts[1]
    if len(year) != 4 or not year.isdigit():
        return None
    try:
        m = int(month)
    except ValueError:
        return None
    if not 1 <= m <= 12:
        return None
    return f"{year}Q{(m - 1) // 3 + 1}"


def collect(
    tickers: list[str],
    fetch: Callable[[str], list[dict]],
    start: str = COLLECT_START,
) -> pd.DataFrame:
    """Per-ticker quarterly revenue, one column per ticker, one row per quarter.

    ``fetch(ticker)`` returns raw income-statement rows. Only rows whose
    ``report_type`` is ``quarterly`` are used; a missing/blank ``total_revenue``
    stays ``NaN`` (never 0) so an unreported quarter cannot masquerade as zero
    revenue. When a quarter appears twice (restatement), the latest period-end
    wins.
    """
    series_by_ticker: dict[str, dict[str, float]] = {}
    for t in tickers:
        rows = fetch(t) or []
        series: dict[str, float] = {}
        end_by_label: dict[str, str] = {}
        for row in rows:
            if (row.get("report_type") or "").lower() != "quarterly":
                continue
            end = (row.get("fiscal_date_ending") or "")[:10]
            if end < start:
                continue
            label = quarter_label(end)
            if label is None:
                continue
            rev = to_float(row.get("total_revenue"))
            # Register the quarter even when revenue is blank: an unreported
            # value must stay NaN so the quarter reads as partial. Dropping it
            # would let a missing constituent vanish and the segment total
            # silently understate.
            prev_end = end_by_label.get(label)
            if prev_end is None or end > prev_end:
                series[label] = rev
                end_by_label[label] = end
            elif end == prev_end and rev is not None:
                series[label] = rev
        series_by_ticker[t] = series

    labels = sorted({lab for s in series_by_ticker.values() for lab in s})
    if not labels:
        return pd.DataFrame(columns=list(tickers))
    data = {t: [series_by_ticker[t].get(lab) for lab in labels] for t in tickers}
    return pd.DataFrame(data, index=labels)


def aggregate(df: pd.DataFrame) -> pd.DataFrame:
    """Segment total + QoQ/YoY % change, computed on complete quarters only.

    A quarter missing any constituent is dropped (summing the rest would
    understate the segment); the caller surfaces those partial quarters.
    """
    complete = df.dropna()
    total = complete.sum(axis=1)
    agg = pd.DataFrame({"total": total})
    agg["qoq_pct"] = total.pct_change() * 100
    agg["yoy_pct"] = total.pct_change(4) * 100
    return agg


def per_ticker_qoq(df: pd.DataFrame) -> pd.DataFrame:
    """QoQ % change per company on the complete-quarter frame."""
    return df.dropna().pct_change() * 100


def partial_quarters(df: pd.DataFrame) -> list[str]:
    """Quarters with at least one constituent missing (excluded from aggregate)."""
    return [lab for lab, row in df.iterrows() if row.isna().any()]


def render_plot(df: pd.DataFrame, agg: pd.DataFrame, path: Path) -> None:
    """Stacked revenue bars (segment, $B) + QoQ % line on a secondary axis."""
    display = agg.index[agg.index >= DISPLAY_START]
    if len(display) == 0:
        raise ValueError(f"no complete quarters at or after {DISPLAY_START}")

    rev_b = df.loc[display] / 1e9
    total_b = agg.loc[display, "total"] / 1e9
    qoq = agg.loc[display, "qoq_pct"]

    fig, ax = plt.subplots(figsize=(12, 7), facecolor=BG)
    ax.set_facecolor(BG)

    columns = list(df.columns)
    bottom = [0.0] * len(display)
    x = range(len(display))
    for i, t in enumerate(columns):
        vals = [0.0 if pd.isna(v) else float(v) for v in rev_b[t]]
        ax.bar(x, vals, bottom=bottom, width=0.62, label=t,
               color=PALETTE[i % len(PALETTE)], edgecolor=BG, linewidth=0.6)
        bottom = [b + v for b, v in zip(bottom, vals)]

    for xi, tot in zip(x, total_b):
        ax.text(xi, tot + max(total_b) * 0.015, f"${tot:,.1f}B", ha="center",
                va="bottom", color=FG, fontsize=9, fontweight="bold")

    ax.set_xticks(list(x))
    ax.set_xticklabels(list(display), color=FG, fontsize=10)
    ax.set_ylabel("Segment revenue ($B)", color=FG, fontsize=10)
    ax.tick_params(axis="y", colors=MUTED, labelsize=9)
    ax.tick_params(axis="x", colors=FG)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color("#2a3350")
    ax.grid(axis="y", color="#232c47", linewidth=0.6, alpha=0.7)
    ax.set_axisbelow(True)

    ax2 = ax.twinx()
    ax2.plot(list(x), list(qoq), color=ACCENT, marker="o", markersize=6,
             linewidth=2, label="QoQ %", zorder=5)
    finite = [v for v in qoq if not pd.isna(v)]
    if finite:
        # Headroom so the point labels never collide with the title / legend.
        ax2.set_ylim(min(0.0, min(finite)) - 0.5, max(finite) * 1.35)
    for xi, v in zip(x, qoq):
        if pd.isna(v):
            continue
        ax2.annotate(f"{v:+.1f}%", (xi, v), textcoords="offset points",
                     xytext=(0, 9), ha="center", color=ACCENT, fontsize=9)
    ax2.set_ylabel("QoQ change (%)", color=ACCENT, fontsize=10)
    ax2.tick_params(axis="y", colors=ACCENT, labelsize=9)
    ax2.axhline(0, color=MUTED, linewidth=0.6, alpha=0.4)
    for spine in ("top", "left", "bottom"):
        ax2.spines[spine].set_visible(False)
    ax2.spines["right"].set_color(ACCENT)

    ax.set_title(f"{SEGMENT} — Quarterly Revenue & QoQ Change ({DISPLAY_START[:4]}–)",
                 color=FG, fontsize=14, fontweight="bold", pad=14)
    ax.text(0.0, 1.02, "Stacked bars = constituent revenue; line = segment QoQ %",
            transform=ax.transAxes, color=MUTED, fontsize=9)

    handles, labels = ax.get_legend_handles_labels()
    ax.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, -0.08),
              ncol=len(columns), frameon=False, labelcolor=FG, fontsize=9)

    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)


def _fmt_pct(v) -> str:
    return "  n/a" if v is None or pd.isna(v) else f"{v:+.1f}%"


def _print_table(df: pd.DataFrame, agg: pd.DataFrame) -> None:
    display = agg.index[agg.index >= DISPLAY_START]
    qoq_by = per_ticker_qoq(df)
    header = f"{'Quarter':<9}{'Segment $B':>12}{'QoQ':>9}{'YoY':>9}  " + \
             "".join(f"{t:>9}" for t in df.columns)
    print(header)
    print("-" * len(header))
    for lab in display:
        row = agg.loc[lab]
        cells = "".join(f"{_fmt_pct(qoq_by.loc[lab, t]):>9}" for t in df.columns)
        print(f"{lab:<9}{row['total'] / 1e9:>12,.2f}"
              f"{_fmt_pct(row['qoq_pct']):>9}{_fmt_pct(row['yoy_pct']):>9}  {cells}")


def _json_payload(df: pd.DataFrame, agg: pd.DataFrame) -> dict:
    def clean(v):
        return None if v is None or pd.isna(v) else float(v)

    return {
        "segment": SEGMENT,
        "tickers": list(df.columns),
        "display_start": DISPLAY_START,
        "partial_quarters": partial_quarters(df),
        "quarters": [
            {
                "quarter": lab,
                "total_revenue": float(agg.loc[lab, "total"]),
                "qoq_pct": clean(agg.loc[lab, "qoq_pct"]),
                "yoy_pct": clean(agg.loc[lab, "yoy_pct"]),
                "revenue_by_ticker": {t: clean(df.loc[lab, t]) for t in df.columns},
            }
            for lab in agg.index
        ],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=f"{SEGMENT} quarterly revenue analysis")
    ap.add_argument("--out-dir", default=None, help="output directory (default .cache)")
    args = ap.parse_args(argv)

    client = UWClient()
    if not client.active:
        print("UNUSUAL_WHALES_API_KEY is not set — add it to RevenueChaser/.env "
              "or the environment.", file=sys.stderr)
        return 2

    print(f"Fetching quarterly income statements for {', '.join(TICKERS)} ...",
          flush=True)
    df = collect(TICKERS, client.income_statements)
    if df.empty:
        print("No quarterly revenue returned for any ticker.", file=sys.stderr)
        return 1

    missing = [t for t in TICKERS if df[t].notna().sum() == 0]
    if missing:
        print(f"WARNING: no quarterly revenue for {', '.join(missing)}", file=sys.stderr)

    partial = partial_quarters(df)
    if partial:
        print(f"WARNING: partial quarters excluded from the segment aggregate: "
              f"{', '.join(partial)}", file=sys.stderr)

    agg = aggregate(df)
    if agg.dropna().empty:
        print("No complete quarter has all constituents — nothing to aggregate.",
              file=sys.stderr)
        return 1

    _print_table(df, agg)

    out_dir = Path(args.out_dir) if args.out_dir else OUTPUT_DIR
    png = out_dir / "ai_cloud_revenue.png"
    js = out_dir / "ai_cloud_revenue.json"
    render_plot(df, agg, png)
    js.parent.mkdir(parents=True, exist_ok=True)
    js.write_text(json.dumps(_json_payload(df, agg), indent=2), encoding="utf-8")
    print(f"\nWrote {png}")
    print(f"Wrote {js}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
