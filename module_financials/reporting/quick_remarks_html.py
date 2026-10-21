"""Quick Remarks HTML — 3 sinyal (0-9) skor rozeti + sinyal kartları + overlay'ler."""
from __future__ import annotations

from html import escape as _esc


def _fmt_usd(v) -> str:
    if v is None:
        return "N/A"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e12:
        return f"{sign}${a / 1e12:,.2f}T"
    if a >= 1e9:
        return f"{sign}${a / 1e9:,.2f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:,.2f}M"
    if a >= 1e3:
        return f"{sign}${a / 1e3:,.1f}K"
    return f"{sign}${a:,.0f}"


def _fmt_value(sub: dict) -> str:
    v = sub.get("value")
    unit = sub.get("unit", "")
    if v is None:
        return "N/A"
    if unit == "bool":
        return "Yes" if v else "No"
    if unit == "pair_pct":
        a, b = v if isinstance(v, (tuple, list)) else (None, None)
        return f"{_pct(a)} → {_pct(b)}"
    if unit == "pct":
        return _pct(v)
    if unit == "usd":
        return _fmt_usd(v)
    if unit == "ratio":
        return f"{v:,.2f}×"
    if unit == "runway":
        return "self-funded" if v >= 999 else f"{v:,.1f}y"
    return str(v)


def _pct(v) -> str:
    if v is None:
        return "N/A"
    return f"{v * 100:,.1f}%"


def _tone(passed: bool | None) -> str:
    """Green / red, and grey for an unknown sub-criterion (an input was missing, #151)."""
    return "grey" if passed is None else ("green" if passed else "red")


def _mark(passed: bool | None) -> str:
    return "&#63;" if passed is None else ("&#10003;" if passed else "&#10007;")


def _score(s: dict) -> str:
    """``passed/total``, plus ``of n known`` when a sub-criterion was unknown (a stored card may lack ``known``)."""
    known = s.get("known")
    text = f"{s['passed']}/{s['total']}"
    return text if known is None or known == s["total"] else f"{text} of {known} known"


def _signals_html(result: dict) -> str:
    out = []
    for s in result.get("signals", []):
        subs_html = []
        for sub in s.get("subs", []):
            subs_html.append(
                f'<div class="sub">'
                f'<span class="mark m-{_tone(sub["passed"])}">{_mark(sub["passed"])}</span>'
                f'<span class="slabel">{_esc(str(sub["label"]))}</span>'
                f'<span class="sval">{_esc(_fmt_value(sub))} <em>{_esc(str(sub["threshold"]))}</em></span>'
                f'</div>'
            )
        out.append(
            f'<div class="panel rule">'
            f'<div class="rhead"><span class="rname">{_esc(str(s["name"]))}</span>'
            f'<span class="rscore">{_esc(_score(s))}</span></div>'
            f'{"".join(subs_html)}'
            f'</div>'
        )
    return "".join(out)


def _overlays_html(result: dict) -> str:
    tags = []
    if result.get("dilution_warning"):
        d = result.get("dilution_yoy")
        pct = f"{d * 100:,.0f}%" if d is not None else ""
        tags.append(f'<span class="tag warn">High dilution ({pct} shares)</span>')
    if result.get("tech_breakthrough"):
        tags.append('<span class="tag tech">Technological Breakthrough</span>')
    rd = result.get("rd_intensity")
    if rd is not None:
        tags.append(f'<span class="tag">R&D / Revenue {rd * 100:,.1f}%</span>')
    if not tags:
        return ""
    return '<div class="qr-tags">' + "".join(tags) + "</div>"


def _text(result: dict) -> dict:
    """Provider strings (company, ticker, sector, source) escaped for markup (FN-1)."""
    return {
        "company": _esc(str(result.get("company", result.get("ticker")))),
        "ticker": _esc(str(result.get("ticker"))),
        "sector": _esc(str(result.get("sector") or "N/A")),
        "source": _esc(str(result.get("source", "ticker_scores / yfinance"))),
    }


def render_quick_remarks_section(result: dict) -> str:
    """Fin Research raporuna gömülecek Quick Remarks fragment'i (0-9 skor + 3 sinyal + overlay)."""
    score = result.get("score")
    mx = result.get("max", 9)
    pct = (score / mx * 100) if score is not None else 0
    t = _text(result)
    return f"""
<section class="qr" style="margin-bottom:32px;">
  <style>
    .qr h2 {{ font-size:18px; border-bottom:1px solid #232a3b; padding-bottom:8px; margin:0 0 12px; }}
    .qr .qr-head {{ display:flex; align-items:center; gap:16px; flex-wrap:wrap; margin-bottom:12px; }}
    .qr .qr-score {{ text-align:center; }}
    .qr .qr-score .big {{ font-size:34px; font-weight:800; color:#d7dce5; }}
    .qr .qr-score .cap {{ font-size:11px; color:#8a93a6; text-transform:uppercase; letter-spacing:1px; }}
    .qr .qr-bar {{ height:6px; background:#1a1f2e; border-radius:3px; overflow:hidden; width:160px; margin-top:6px; }}
    .qr .qr-bar i {{ display:block; height:100%; background:#4f9dff; }}
    .qr .qr-meta {{ display:flex; flex-wrap:wrap; gap:16px; color:#8a93a6; font-size:13px; margin-bottom:14px; }}
    .qr .panel {{ background:#131722; border:1px solid #232a3b; border-radius:10px; padding:14px 16px; margin-bottom:14px; }}
    .qr .rule {{ margin-bottom:12px; }}
    .qr .rhead {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }}
    .qr .rname {{ font-weight:700; font-size:15px; color:#d7dce5; }}
    .qr .rscore {{ color:#8a93a6; font-weight:600; }}
    .qr .sub {{ display:flex; align-items:center; gap:10px; padding:6px 0; border-top:1px solid #1a1f2e; }}
    .qr .sub:first-of-type {{ border-top:none; }}
    .qr .mark {{ width:18px; text-align:center; font-weight:700; }}
    .qr .m-green {{ color:#2ecc71; }}
    .qr .m-red {{ color:#e74c3c; }}
    .qr .m-grey {{ color:#8a93a6; }}
    .qr .slabel {{ flex:1; color:#d7dce5; }}
    .qr .sval {{ color:#8a93a6; font-size:13px; }}
    .qr .sval em {{ font-style:normal; color:#5b6472; }}
    .qr .qr-tags {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:14px; }}
    .qr .tag {{ font-size:10.5px; padding:2px 8px; border-radius:20px; background:#1a1f2e; color:#8a93a6; }}
    .qr .tag.warn {{ background:rgba(231,76,60,.12); color:#e74c3c; }}
    .qr .tag.tech {{ background:rgba(0,230,168,.12); color:#00e6a8; }}
  </style>
  <h2>Quick Remarks</h2>
  <div class="qr-head">
    <div>
      <div style="font-size:15px;color:#8a93a6;">Momentum-Growth Screener · {t["company"]}</div>
    </div>
    <div class="qr-score">
      <div class="big">{score if score is not None else "N/A"}<span style="font-size:18px;color:#8a93a6"> / {mx}</span></div>
      <div class="cap">score</div>
      <div class="qr-bar"><i style="width:{pct:.0f}%"></i></div>
    </div>
  </div>
  <div class="qr-meta">
    <span>Price: {('$' + f'{result["price"]:,.2f}') if result.get("price") is not None else "N/A"}</span>
    <span>Sector: {t["sector"]}</span>
    <span>Source: {t["source"]}</span>
  </div>
  {_overlays_html(result)}
  {_signals_html(result)}
</section>"""


def render_quick_remarks_html(result: dict) -> str:
    """Bağımsız tam sayfa (Fin Research'in Quick Remarks bölümüyle aynı veri)."""
    score = result.get("score")
    mx = result.get("max", 9)
    pct = (score / mx * 100) if score is not None else 0
    t = _text(result)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t["company"]} — Fin Research</title>
<style>
  :root {{ --bg:#0b0e14; --panel:#131722; --panel2:#1a1f2e; --border:#232a3b;
    --text:#d7dce5; --muted:#8a93a6; --accent:#4f9dff; --green:#2ecc71; --red:#e74c3c; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text);
    font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; font-size:14px; line-height:1.5; }}
  .wrap {{ max-width:760px; margin:0 auto; padding:28px 20px 60px; }}
  header {{ display:flex; align-items:center; gap:16px; margin-bottom:24px; flex-wrap:wrap; }}
  header h1 {{ margin:0; font-size:24px; letter-spacing:-.5px; }}
  header .sub {{ color:var(--muted); }}
  .score {{ margin-left:auto; text-align:center; }}
  .score .big {{ font-size:40px; font-weight:800; }}
  .score .cap {{ font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:1px; }}
  .bar {{ height:6px; background:var(--panel2); border-radius:3px; overflow:hidden; margin-top:8px; }}
  .bar i {{ display:block; height:100%; background:var(--accent); }}
  .meta {{ display:flex; flex-wrap:wrap; gap:16px; color:var(--muted); margin-bottom:24px; font-size:13px; }}
  .panel {{ background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:14px 16px; margin-bottom:14px; }}
  .rule {{ margin-bottom:16px; }}
  .rhead {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }}
  .rname {{ font-weight:700; font-size:15px; }}
  .rscore {{ color:var(--muted); font-weight:600; }}
  .sub {{ display:flex; align-items:center; gap:10px; padding:6px 0; border-top:1px solid var(--panel2); }}
  .sub:first-of-type {{ border-top:none; }}
  .mark {{ width:18px; text-align:center; font-weight:700; }}
  .m-green {{ color:var(--green); }}
  .m-red {{ color:var(--red); }}
  .m-grey {{ color:var(--muted); }}
  .slabel {{ flex:1; }}
  .sval {{ color:var(--muted); font-size:13px; }}
  .sval em {{ font-style:normal; color:#5b6472; }}
  .tags {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:16px; }}
  .tag {{ font-size:10.5px; padding:2px 8px; border-radius:20px; background:var(--panel2); color:var(--muted); }}
  .tag.warn {{ background:rgba(231,76,60,.12); color:var(--red); }}
  .tag.tech {{ background:rgba(0,230,168,.12); color:#00e6a8; }}
  footer {{ color:var(--muted); font-size:12px; margin-top:28px; border-top:1px solid var(--border); padding-top:14px; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>{t["company"]} <span style="color:var(--accent)">{t["ticker"]}</span></h1>
      <div class="sub">Fin Research — Momentum-Growth Screener</div>
    </div>
    <div class="score">
      <div class="big">{score if score is not None else "N/A"}<span style="font-size:20px;color:var(--muted)"> / {mx}</span></div>
      <div class="cap">score</div>
      <div class="bar"><i style="width:{pct:.0f}%"></i></div>
    </div>
  </header>
  <div class="meta">
    <span>Price: {('$' + f'{result["price"]:,.2f}') if result.get("price") is not None else "N/A"}</span>
    <span>Sector: {t["sector"]}</span>
    <span>Source: {t["source"]}</span>
  </div>
  <div class="tags">{_overlays_html(result)}</div>
  {_signals_html(result)}
  <footer>
    Source: {t["source"]}. Criteria thresholds match the
    Asymmetric Growth Hunter methodology. This is for informational purposes only and does not
    constitute financial advice.
  </footer>
</div>
</body>
</html>"""
