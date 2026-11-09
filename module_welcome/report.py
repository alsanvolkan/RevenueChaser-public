"""The Welcome page, the dashboard's opening screen (welcome plan §2), in Turkish with Amsterdam times.

Four cards: today's economic calendar in short (read from ``boards/news.duckdb``, nothing copied), the earnings of
today and the next trading day, the last stored market closes, and how fresh each job's data is. Store-only: it
reads the two board files and never fetches. The US session clock runs in the page from the session times written
here (``core/calendar``: holidays and early closes included), so it stays right between renders.

Market tiles follow the stat-tile form (dataviz): label, value, signed change against the session before (green
when that direction is good for the reader, red when bad, plain when neither), and a 20-session line in a muted ink
with today's point in the accent.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

from RevenueChaser.core import calendar as mcal
from RevenueChaser.core import rawstore
from RevenueChaser.core.config import OUTPUT_DIR
from RevenueChaser.core.reporting_common import THEME_CSS, THEME_JS, write_artifact
from RevenueChaser.module_news.calendar import AMS, EVENTS, HIGHLIGHT, local_day
from RevenueChaser.module_news.report import (WEEKDAYS, day_label, dots_html, esc, hm, long_date, vals_html)
from RevenueChaser.module_welcome.build import EARNINGS, FRESHNESS, NY, SNAPSHOT, earnings_days

FILENAME = "welcome_latest.html"
SESSION_DAYS = 8
FRESH_DAYS = 7
FRESH_LIMIT = 12
EARNINGS_PER_DAY = 8
_SHORT = ("Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara")

#: series -> (label, up is ..., unit). ``good``/``bad`` colours the change; ``neutral`` leaves it plain.
TILES = (("SPX", "S&P 500", "good", ""), ("VIX", "VIX", "bad", ""), ("US10Y", "ABD 10 yıllık faiz", "neutral", "%"),
         ("MOVE", "MOVE (tahvil oynaklığı)", "bad", ""))
_SESSION_TEXT = {"premarket": "Açılış öncesi", "afterhours": "Kapanış sonrası"}


def tr_num(x: float, decimals: int = 2, sign: bool = False) -> str:
    """Turkish number writing: ``6012.5`` -> ``6.012,50``; ``sign`` adds ``+`` to a positive value."""
    text = f"{abs(x):,.{decimals}f}".replace(",", " ").replace(".", ",").replace(" ", ".")
    lead = "-" if x < 0 else ("+" if sign and x > 0 else "")
    return lead + text


def _short_date(d) -> str:
    return f"{d.day} {_SHORT[d.month - 1]}"


def _ago(ts: float, now: float) -> str:
    mins = max(0, int((now - float(ts)) // 60))
    if mins < 60:
        return f"{mins} dk önce"
    if mins < 48 * 60:
        return f"{mins // 60} sa önce"
    return f"{mins // 1440} gün önce"


def _open_tab(key: str, text: str) -> str:
    return (f'<button type="button" class="go" onclick="openTab(\'{key}\')">{esc(text)} →</button>')


# ── the cards ────────────────────────────────────────────────────────────────────────────────────────────────────
def _calendar_card(now: float) -> str:
    head = '<section class="card"><h2 class="lbl">Ekonomik takvim · bugün</h2>'
    if not rawstore.board_db_path("news").exists():
        return head + '<p class="empty">News board henüz kurulmadı.</p></section>'
    today = local_day(now)
    start = datetime(today.year, today.month, today.day, tzinfo=AMS).timestamp()
    events = rawstore.query(
        f"SELECT * FROM {EVENTS} WHERE event_ts >= ? AND event_ts < ? AND retired_at IS NULL ORDER BY event_ts",
        [start, start + 86400 + 3600], db="news")
    events = [e for e in events if local_day(e["event_ts"]) == today]
    hl = rawstore.query(f"SELECT * FROM {HIGHLIGHT} WHERE session_date = ?", [today.isoformat()], db="news")
    if not events:
        return head + '<p class="empty">Bugün takvimde veri yok.</p>' + _open_tab("module_news", "Haftayı gör") + \
            "</section>"
    body = f'<div class="big">bugün {len(events)} veri</div>'
    if hl:
        h = hl[0]
        body += (f'<div class="hl"><div class="muted">Öne çıkan · {hm(h["event_ts"])} {dots_html(h.get("importance"))}'
                 f'</div><div class="name">{esc(h["event"])}</div>{vals_html(h)}</div>')
    hl_key = (hl[0]["event_ts"], hl[0]["event"]) if hl else None
    nxt = [e for e in events if float(e["event_ts"]) >= now and (e["event_ts"], e["event"]) != hl_key][:2]
    if nxt:
        body += '<div class="muted sub">Sıradaki</div><ul class="list">' + "".join(
            f'<li class="row"><span class="t">{hm(e["event_ts"])}</span><span>{esc(e["event"])}</span>'
            f'{dots_html(e.get("importance"))}</li>' for e in nxt) + "</ul>"
    return head + body + _open_tab("module_news", "News board") + "</section>"


def _earnings_card(now: float, watchlist: set[str]) -> str:
    days = earnings_days(now)
    rows = rawstore.query(f"SELECT * FROM {EARNINGS} WHERE earnings_date IN (?, ?) AND retired_at IS NULL",
                          [days[0], days[1]], db="welcome")
    today_ny = datetime.fromtimestamp(now, NY).date()
    out = ['<section class="card"><h2 class="lbl">Bilanço · bugün ve sonraki işlem günü</h2>']
    if not rows:
        out.append('<p class="empty">Bu iki gün için saklı bilanço yok.</p>')
    for d in days:
        got = [r for r in rows if r["earnings_date"] == d]
        got.sort(key=lambda r: (r["ticker"] not in watchlist, -(r.get("expected_move_perc") or 0), r["ticker"]))
        name = ("Bugün · " if d == today_ny else "") + day_label(d)
        out.append(f'<h3 class="day">{esc(name)} · {len(got)} şirket</h3>')
        if not got:
            continue
        out.append('<ul class="list">')
        for r in got[:EARNINGS_PER_DAY]:
            mine = ' <span class="pill">listemde</span>' if r["ticker"] in watchlist else ""
            em = r.get("expected_move_perc")
            move = f'<span class="em">±{esc("%" + tr_num(em * 100, 1))}</span>' if em is not None else ""
            out.append(f'<li class="row"><span class="tk">{esc(r["ticker"])}</span>{mine}'
                       f'<span class="muted">{_SESSION_TEXT.get(r["report_session"], esc(r["report_session"]))}'
                       f'</span>{move}</li>')
        out.append("</ul>")
        if len(got) > EARNINGS_PER_DAY:
            out.append(f'<p class="muted">ve {len(got) - EARNINGS_PER_DAY} şirket daha</p>')
    out.append('<p class="muted note">± = opsiyonların fiyatladığı beklenen hareket</p>')
    out.append(_open_tab("module_earnings_radar", "Earnings Radar") + "</section>")
    return "".join(out)


def _spark(values: list[float]) -> str:
    if len(values) < 2:
        return ""
    w, h, pad = 160.0, 36.0, 3.0
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    pts = [(pad + i * (w - 2 * pad) / (len(values) - 1), h - pad - (v - lo) * (h - 2 * pad) / span)
           for i, v in enumerate(values)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    x, y = pts[-1]
    label = f"Son {len(values)} kapanış: en düşük {tr_num(lo)}, en yüksek {tr_num(hi)}"
    return (f'<svg class="spark" viewBox="0 0 {w:.0f} {h:.0f}" role="img" aria-label="{esc(label)}">'
            f'<title>{esc(label)}</title><polyline points="{line}" fill="none" stroke="var(--muted)" '
            f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="var(--amber)"/></svg>')


def _snapshot_card() -> str:
    latest = {r["series"]: r for r in rawstore.query(
        f"SELECT * FROM {SNAPSHOT} QUALIFY row_number() OVER (PARTITION BY series ORDER BY session_date DESC) = 1",
        db="welcome")}
    tiles = []
    for key, label, up, unit in TILES:
        r = latest.get(key)
        if not r:
            continue
        value = (unit + tr_num(r["close"])) if unit == "%" else tr_num(r["close"])
        change, direction, delta = r.get("change"), "flat", ""
        if change is not None:
            change = round(change, 2) or 0.0                      # a change that shows as 0,00 is flat
            rising = change > 0
            direction = "neutral" if up == "neutral" or change == 0 else (
                ("good" if rising else "bad") if up == "good" else ("bad" if rising else "good"))
            pct = f' ({"%" + tr_num((r.get("change_pct") or 0) * 100, 2, sign=True).lstrip("+-")})' \
                if r.get("change_pct") is not None and unit != "%" else ""
            sign = "+" if change > 0 else ("-" if change < 0 else "")
            delta = (f'<div class="delta" data-dir="{direction}">{"▲" if rising else ("▼" if change < 0 else "•")} '
                     f'{sign}{tr_num(abs(change), 2)}{pct}</div>')
        tiles.append(f'<div class="tile"><div class="muted">{esc(label)}</div><div class="val">{esc(value)}</div>'
                     f'{delta}{_spark(json.loads(r.get("spark") or "[]"))}'
                     f'<div class="muted small">{_short_date(r["session_date"])} kapanışı</div></div>')
    body = ('<div class="tiles">' + "".join(tiles) + "</div>") if tiles else (
        '<p class="empty">Saklı kapanış yok.</p>')
    return ('<section class="card"><h2 class="lbl">Piyasa · son kapanış</h2>' + body +
            '<p class="muted note">Canlı fiyat değil: saklı son kapanış ve bir önceki seansa göre değişim.</p></section>')


def _freshness_card(now: float, labels: dict[str, str]) -> str:
    rows = rawstore.query(f"SELECT * FROM {FRESHNESS} WHERE last_finished_at >= ?",
                          [now - FRESH_DAYS * 86400], db="welcome")

    def failing(r):
        return r.get("last_status") == "error" or (r.get("last_failed_at") or 0) >= now - 86400

    rows.sort(key=lambda r: (not failing(r), -float(r["last_finished_at"])))
    head = '<section class="card"><h2 class="lbl">Veri tazeliği · son 7 gün</h2>'
    if not rows:
        return head + '<p class="empty">Son 7 günde biten iş yok.</p>' + _open_tab("module_settings", "Data health") + \
            "</section>"
    n_bad = sum(1 for r in rows if failing(r))
    out = [head, f'<div class="big">{n_bad} işte hata</div>' if n_bad else '<div class="big">Hepsi tamam</div>',
           '<ul class="list">']
    for r in rows[:FRESH_LIMIT]:
        bad = failing(r)
        state = '<span class="st bad">✕ hata</span>' if r.get("last_status") == "error" else (
            '<span class="st warn">! 24 saatte hata</span>' if bad else '<span class="st ok">✓ tamam</span>')
        err = f'<div class="muted small err">{esc(r["last_error"])}</div>' if bad and r.get("last_error") else ""
        out.append(f'<li class="row fr"><div><div class="name">{esc(labels.get(r["job"], r["job"]))}</div>{err}</div>'
                   f'{state}<span class="muted small">{_ago(r["last_finished_at"], now)}</span></li>')
    out.append("</ul>" + _open_tab("module_settings", "Data health") + "</section>")
    return "".join(out)


def sessions(now: float) -> list[list[float]]:
    """``[open, close]`` epoch seconds of the US sessions from today's New York date on (early closes included)."""
    d, out = datetime.fromtimestamp(now, NY).date(), []
    if not mcal.is_trading_day(d):
        d = mcal.next_trading_day(d)
    while len(out) < SESSION_DAYS:
        opened = datetime.combine(d, dtime(9, 30), NY).timestamp()
        out.append([opened, mcal.session_close(d).timestamp()])
        d = mcal.next_trading_day(d)
    return out


_CSS = """
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1200px;margin:0 auto;padding:16px}
.top{display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px 16px;align-items:flex-end;margin-bottom:12px}
.eyebrow{color:var(--muted);font-size:12px;letter-spacing:.04em;text-transform:uppercase}
h1{margin:2px 0 0;color:var(--bright);font-size:22px}
.clock{display:inline-flex;align-items:center;gap:8px;min-height:44px;padding:0 14px;border-radius:10px;
 border:1px solid var(--border);background:var(--card);color:var(--bright)}
.clock[data-state="open"]{border-color:var(--pos)}
.muted{color:var(--muted);font-size:12.5px}
.small{font-size:12px}
.grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px;align-items:start}
.col{display:flex;flex-direction:column;gap:16px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
.lbl{margin:0 0 10px;font-size:12px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);font-weight:600}
.big{font-size:18px;color:var(--bright);font-weight:600;margin-bottom:8px}
.hl{border:1px solid var(--amber);border-radius:10px;background:var(--input-bg);padding:10px 12px;margin-bottom:10px}
.name{color:var(--bright)}
.dots{color:var(--amber);letter-spacing:1px;font-size:11px}
.vals{display:flex;gap:12px;font-size:12.5px;color:var(--muted);flex-wrap:wrap;margin-top:4px}
.vals b{color:var(--fg);font-weight:600}
.sub{margin:4px 0}
.list{list-style:none;margin:0;padding:0}
.row{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;min-height:44px;padding:6px 0;
 border-bottom:1px solid var(--border)}
.row.fr{display:grid;grid-template-columns:1fr auto auto;gap:4px 12px}
.t{font-variant-numeric:tabular-nums;color:var(--bright);font-weight:600;min-width:44px}
.tk{color:var(--bright);font-weight:600;min-width:52px}
.pill{font-size:11px;padding:1px 8px;border-radius:999px;border:1px solid var(--amber);color:var(--amber)}
.em{margin-left:auto;font-variant-numeric:tabular-nums;color:var(--fg)}
.day{margin:12px 0 2px;font-size:13px;color:var(--muted);font-weight:600}
.note{margin:10px 0 0}
.tiles{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.tile{border:1px solid var(--border);border-radius:10px;padding:10px 12px}
.val{font-size:20px;font-weight:600;color:var(--bright);font-variant-numeric:tabular-nums}
.delta{font-size:12.5px;font-variant-numeric:tabular-nums;color:var(--fg)}
.delta[data-dir="good"]{color:var(--pos)}
.delta[data-dir="bad"]{color:var(--neg)}
.spark{display:block;width:100%;height:auto;margin:6px 0 2px}
.st{font-size:12px;white-space:nowrap}
.st.ok{color:var(--pos)}.st.bad{color:var(--neg)}.st.warn{color:var(--amber)}
.err{overflow-wrap:anywhere}
.go{margin-top:12px;min-height:44px;padding:0 16px;border-radius:10px;border:1px solid var(--border);
 background:transparent;color:var(--sky);font:inherit;cursor:pointer}
.go:hover{background:var(--hover-soft)}
.empty{color:var(--muted);margin:6px 0}
.foot{margin-top:16px;color:var(--muted);font-size:12px}
@media (max-width:900px){.grid{grid-template-columns:minmax(0,1fr)}}
@media (max-width:560px){.wrap{padding:12px}.go{width:100%}}
"""

_JS = """
function openTab(key){
  try{ parent.postMessage({type:'rc-open-tab', key:key}, location.origin); }catch(e){}
}
function _amsHM(t){
  return new Date(t * 1000).toLocaleTimeString('tr-TR', {timeZone:'Europe/Amsterdam', hour:'2-digit', minute:'2-digit'});
}
function _amsDay(t){
  return new Date(t * 1000).toLocaleDateString('tr-TR', {timeZone:'Europe/Amsterdam', weekday:'long'});
}
function _left(s){
  var m = Math.max(0, Math.round(s / 60)), h = Math.floor(m / 60);
  return (h ? h + ' sa ' : '') + (m % 60) + ' dk';
}
function sessionClock(){
  var el = document.getElementById('clock'); if(!el) return;
  var now = Date.now() / 1000, text = 'Seans bilgisi yok (sayfayı yenile)', state = 'closed';
  for(var i = 0; i < SESSIONS.length; i++){
    var o = SESSIONS[i][0], c = SESSIONS[i][1];
    if(now >= o && now < c){ state = 'open'; text = 'ABD piyasası açık · kapanışa ' + _left(c - now) + ' (' + _amsHM(c) + ')'; break; }
    if(now < o){
      state = (o - now < 5.5 * 3600) ? 'pre' : 'closed';
      text = (state === 'pre' ? 'Açılış öncesi · açılışa ' + _left(o - now)
                               : 'ABD piyasası kapalı · açılış ' + _amsDay(o)) + ' ' + _amsHM(o);
      break;
    }
  }
  el.setAttribute('data-state', state); el.textContent = text;
}
sessionClock(); setInterval(sessionClock, 30000);
"""


def render_html(now: float | None = None, *, watchlist: set[str], labels: dict[str, str]) -> str:
    now = time.time() if now is None else now
    today = local_day(now)
    left = _calendar_card(now) + _earnings_card(now, watchlist)
    right = _snapshot_card() + _freshness_card(now, labels)
    return f"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bugün</title><script>{THEME_JS}</script><style>{THEME_CSS}{_CSS}</style></head>
<body><div class="wrap">
<header class="top"><div><div class="eyebrow">RevenueChaser · Bugün</div><h1>{long_date(today)}</h1>
<div class="muted">{WEEKDAYS[today.weekday()]} · saatler Amsterdam · sayfa {hm(now)}'de hazırlandı</div></div>
<div class="clock" id="clock" role="status" data-state="closed">ABD seansı</div></header>
<div class="grid"><div class="col">{left}</div><div class="col">{right}</div></div>
<p class="foot">Saklı verilerden hazırlanır, ağ çağrısı yapmaz · Kaynak: Unusual Whales, Yahoo, FRED</p>
</div><script>var SESSIONS = {json.dumps(sessions(now))};{_JS}</script></body></html>"""


def render(now: float | None = None, *, watchlist: set[str], labels: dict[str, str]) -> Path:
    """Write the board file ``.cache/welcome_latest.html`` and return its path."""
    return write_artifact(Path(OUTPUT_DIR) / FILENAME, render_html(now, watchlist=watchlist, labels=labels))
