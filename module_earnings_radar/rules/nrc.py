"""Rule 0 — NRC. Categorizes all tickers into sectioned HTML. Never eliminates."""
from RevenueChaser.core.reporting_common import write_artifact
import json
from pathlib import Path
from datetime import datetime
from RevenueChaser.core.config import OUTPUT_DIR


# Scoped under `.nrc` so the fragment can be embedded inside the Earnings Radar
# without colliding with the radar's own `.section`/`table`/`.badge` styles.
NRC_CSS = """<style>
.nrc * { margin: 0; padding: 0; box-sizing: border-box; }
.nrc { color: #c9d1d9; font-family: 'Segoe UI', 'Consolas', monospace; padding: 4px 0; }
.nrc .header { margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid #21262d; }
.nrc .header h1 { color: #d2991d; font-size: 20px; letter-spacing: 1px; }
.nrc .header .desc { color: #8b949e; font-size: 12px; line-height: 1.8; margin-top: 8px; }
.nrc .header .desc b { color: #c9d1d9; } .nrc .header .desc span { color: #d2991d; }
.nrc .formula { background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 14px 18px; margin: 14px 0; font-size: 12px; color: #8b949e; line-height: 1.8; }
.nrc .formula b { color: #58a6ff; }
.nrc .section { margin-bottom: 24px; }
.nrc .section-title { font-size: 16px; font-weight: 700; padding: 8px 0; margin-bottom: 6px; border-bottom: 1px solid #21262d; display: flex; justify-content: space-between; }
.nrc .section-desc { color: #8b949e; font-size: 12px; margin-bottom: 10px; line-height: 1.6; }
.nrc .section-desc em { color: #d2991d; font-style: normal; }
.nrc .st-A { color: #3fb950; } .nrc .st-B { color: #56d364; } .nrc .st-C { color: #58a6ff; } .nrc .st-I { color: #8b949e; }
.nrc table { width: 100%; border-collapse: collapse; font-size: 13px; }
.nrc th { text-align: left; padding: 8px 6px; color: #8b949e; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; border-bottom: 1px solid #21262d; }
.nrc td { padding: 8px 6px; border-bottom: 1px solid #161b22; }
.nrc tr:hover { background: #161b22; }
.nrc .ticker { color: #58a6ff; font-weight: 700; } .nrc .star { color: #d2991d; font-size: 14px; } .nrc .cluster { color: #8b949e; font-size: 12px; }
.nrc .bull { color: #3fb950; } .nrc .bear { color: #f85149; } .nrc .neut { color: #8b949e; }
.nrc .badge { display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: 700; }
.nrc .bd-A { background: #1a3a1a; color: #3fb950; border: 1px solid #238636; }
.nrc .bd-B { background: #1a2a1a; color: #56d364; border: 1px solid #2ea043; }
.nrc .bd-C { background: #1a1a2e; color: #58a6ff; border: 1px solid #1f6feb; }
.nrc .bd-I { background: #1a1a1a; color: #8b949e; border: 1px solid #30363d; }
.nrc .em { color: #a5d6ff; font-size: 12px; }
</style>"""

TIER_DESC = {
    "Tier A — Revenue Catalyst": "NRC &ge; 70 — <em>Strongest revenue signal.</em> Clear revenue momentum backed by quality and consistent beats.",
    "Tier B — Watchlist": "NRC 55&ndash;69 — <em>Solid indicators.</em> Good growth and quality. Worth tracking for breakout potential.",
    "Tier C — Emerging": "NRC 40&ndash;54 — <em>Mixed signals.</em> Some positives, some risks. Monitor for improvement.",
    "Ignore": "NRC &lt; 40 — <em>No clear revenue catalyst.</em> May still have valid non-revenue trade thesis.",
}


def render_nrc_section(nrc_results: dict[str, dict], cards: list) -> str:
    """NRC tier tables as a self-contained, `.nrc`-scoped fragment (for embedding)."""
    sections = {k: [] for k in TIER_DESC}
    card_map = {c.ticker: c for c in cards}
    for t, info in nrc_results.items():
        tier = info.get("tier", "Ignore")
        sections[tier].append((t, info, card_map.get(t)))

    body = ""
    for tier_key in ["Tier A — Revenue Catalyst", "Tier B — Watchlist", "Tier C — Emerging", "Ignore"]:
        items = sections[tier_key]
        if not items:
            continue
        cls = "st-A" if "A" in tier_key else ("st-B" if "B" in tier_key else ("st-C" if "C" in tier_key else "st-I"))
        emoji = "🟢" if "A" in tier_key else ("🟡" if "B" in tier_key else ("🔵" if "C" in tier_key else "⚪"))

        body += f'<div class="section"><div class="section-title {cls}">{emoji} {tier_key} <span style="font-size:14px;color:#8b949e">({len(items)})</span></div>'
        body += f'<div class="section-desc">{TIER_DESC[tier_key]}</div>'
        body += '<table><thead><tr><th></th><th>Ticker</th><th>NRC</th><th>Grow</th><th>Qual</th><th>Beat</th><th>Sector</th><th>Dir</th><th>EM%</th><th>Driver</th></tr></thead><tbody>'
        for t, info, card in items:
            star = "⭐" if info.get("watchlist") else ""
            sect = (info.get("sector") or (card.cluster if card else ""))[:20]
            em = f"{card.expected_move_pct:.1f}%" if card and card.expected_move_pct > 0 else "—"
            driver = ((card.key_thesis if card else "") or "")[:45] if card else "—"
            dc = "bull" if (card and card.direction_score > 0) else ("bear" if (card and card.direction_score < 0) else "neut")
            dv = f"{card.direction_score:+5.0f}" if card else "—"
            q = info.get("quality")
            q_s = f"{q:.0f}%" if q is not None else "—"
            g = info.get("growth")
            g_s = f"{g:.0f}%" if g is not None else "—"
            b = info.get("beat")
            b_s = f"{b:.0f}%" if b is not None else "—"
            body += f'<tr><td class="star">{star}</td><td class="ticker">{t}</td><td><span class="badge {cls.replace("st-","bd-")}">{info["nrc"]:.0f}</span></td><td>{g_s}</td><td>{q_s}</td><td>{b_s}</td><td class="cluster">{sect}</td><td class="{dc}">{dv}</td><td class="em">{em}</td><td style="color:#8b949e;font-size:12px">{driver}</td></tr>'
        body += "</tbody></table></div>"

    return (
        '<div class="nrc">' + NRC_CSS +
        '<div class="header"><h1>NRC Filter — Notable Revenue Candidates</h1>'
        '<div class="desc"><b>All tickers advance</b> — NRC categorizes, <b>never eliminates</b>. '
        '<span>Tier A</span> (NRC &ge; 70) · <span>Tier B</span> (55–69) · <span>Tier C</span> (40–54) · <span>Ignore</span> (&lt;40)</div></div>'
        '<div class="formula"><b>NRC = live slots only (growth · AGH accel · R7 quality · event), weights renormalized. No 50-fills.</b>'
        '<br><b>Quality gate:</b> NRC capped at 49 if R7-mapped quality &lt; 12</div>'
        + body + '</div>'
    )


def export_nrc_html(nrc_results: dict[str, dict], cards: list, filename: str = None) -> Path:
    inner = render_nrc_section(nrc_results, cards)
    h = (f'<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
         '<title>RevenueChaser — NRC Filter</title></head>'
         f'<body style="background:#0d1117;padding:24px">{inner}</body></html>')

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = OUTPUT_DIR / (filename or f"nrc/nrc_filter_{ts}.html")
    path.parent.mkdir(parents=True, exist_ok=True)
    write_artifact(path, h)
    return path
