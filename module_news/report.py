"""The News board page (plan §6, mock ``News.dc.html``): today's US economic calendar, this week, the day's highlight
and the market headlines, in Turkish, times in Europe/Amsterdam.

Store-only: it reads ``boards/news.duckdb`` and never fetches. Event names stay as the provider serves them; the board's
own words are Turkish. Phone first: one column under 900 px, 44 px touch targets, light and dark from the shell's theme.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from RevenueChaser.core import econ_importance, rawstore
from RevenueChaser.core.config import OUTPUT_DIR
from RevenueChaser.core.html_safe import esc
from RevenueChaser.core.reporting_common import THEME_CSS, THEME_JS, help_html, provider_health_html, write_artifact
from RevenueChaser.module_news.calendar import AMS, EVENTS, HIGHLIGHT, local_day, tr_value
from RevenueChaser.module_news import x_check
from RevenueChaser.module_news.x_feed import PROVIDER as X_PROVIDER

FILENAME = "news_latest.html"
HEADLINES = "news_headlines"
WEEK_DAYS = 7
HEADLINE_HOURS = 24
HEADLINE_LIMIT = 30

MONTHS = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım",
           "Aralık")
WEEKDAYS = ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar")
_KIND = {"report": "Veri", "fed-speaker": "Fed", "fomc": "FOMC"}
_LEVEL = {3: "yüksek", 2: "orta", 1: "düşük"}


def hm(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), AMS).strftime("%H:%M")


def long_date(d) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def day_label(d) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]}"


def _load(now: float) -> tuple[list[dict], dict | None, list[dict], float | None]:
    today = local_day(now)
    start = datetime(today.year, today.month, today.day, tzinfo=AMS)
    end = start + timedelta(days=WEEK_DAYS)
    events = rawstore.query(
        f"SELECT * FROM {EVENTS} WHERE event_ts >= ? AND event_ts < ? AND retired_at IS NULL ORDER BY event_ts, event",
        [start.timestamp(), end.timestamp()], db="news")
    hl = rawstore.query(f"SELECT * FROM {HIGHLIGHT} WHERE session_date = ?", [today.isoformat()], db="news")
    x = f"coalesce(provider, '') = '{X_PROVIDER}'"
    lanes = (f"NOT {x} AND coalesce(tickers, '[]') = '[]'", f"NOT {x} AND coalesce(tickers, '[]') <> '[]'",
             f"{x} AND coalesce(kind, '') <> 'other'")        # the X lane leaves out what Grok filed as "other"
    heads = [r for cond in lanes for r in rawstore.query(      # each lane its own newest LIMIT
        f"SELECT * FROM {HEADLINES} WHERE published_at >= ? AND {cond} "
        f"ORDER BY published_at DESC LIMIT {HEADLINE_LIMIT}", [now - HEADLINE_HOURS * 3600], db="news")]
    stamp = rawstore.query(f"SELECT max(run_at) AS t FROM {EVENTS}", db="news")
    return events, (hl[0] if hl else None), heads, (stamp[0]["t"] if stamp else None)


def dots_html(level) -> str:
    n = int(level or 1)
    return (f'<span class="dots" title="{_LEVEL.get(n, "düşük")} önem" aria-label="{_LEVEL.get(n, "düşük")} önem">'
            f'{econ_importance.dots(n)}</span>')


def vals_html(e: dict) -> str:
    fc, prev = tr_value(e.get("forecast")), tr_value(e.get("prev"))
    return (f'<div class="vals"><span>Beklenti <b>{esc(fc or "–")}</b></span>'
            f'<span>Önceki <b>{esc(prev or "–")}</b></span></div>')


def _row(e: dict, now: float, hl_key) -> str:
    key = (e["event_ts"], e["event"], e["type"])
    cls = "ev hl" if key == hl_key else "ev"
    past = ' data-past="1"' if float(e["event_ts"]) < now else ""
    kind = _KIND.get(str(e.get("type") or "").lower(), "Veri")
    return (f'<li class="{cls}"{past}><span class="t">{hm(e["event_ts"])}</span>'
            f'<div class="nm"><span class="kind">{kind}</span> {dots_html(e.get("importance"))}'
            f'<div class="name">{esc(e["event"])}</div></div>{vals_html(e)}</li>')


def _calendar(events: list[dict], now: float, hl_key) -> tuple[str, str]:
    today = local_day(now)
    todays = [e for e in events if local_day(e["event_ts"]) == today]
    today_html = ("<ul class=\"list\">" + "".join(_row(e, now, hl_key) for e in todays) + "</ul>") if todays else (
        '<p class="empty">Bugün takvimde veri yok.</p>')
    if not events:
        return today_html, '<p class="empty">Bu hafta takvimde veri yok. Yenile, saklı takvimden yeniden kurar.</p>'
    by_day: dict = {}
    for e in events:
        by_day.setdefault(local_day(e["event_ts"]), []).append(e)
    week = "".join(f'<h3 class="day">{day_label(d)}</h3><ul class="list">'
                   + "".join(_row(e, now, hl_key) for e in rows) + "</ul>" for d, rows in by_day.items())
    return today_html, week


def _highlight(hl: dict | None) -> str:
    if not hl:
        return ('<section class="card feat"><h2 class="lbl">Öne çıkan veri</h2>'
                '<p class="empty">Bugün öne çıkan bir veri yok.</p></section>')
    return (f'<section class="card feat"><h2 class="lbl">Öne çıkan veri</h2>'
            f'<div class="feat-name">{esc(hl["event"])}</div>'
            f'<div class="muted">{hm(hl["event_ts"])} Amsterdam · {dots_html(hl.get("importance"))}</div>'
            f'{vals_html(hl)}<p class="why">{esc(hl.get("text"))}</p></section>')


def _tickers(value) -> list[str]:
    try:
        got = json.loads(value) if isinstance(value, str) else (value or [])
    except ValueError:
        return []
    return [str(t) for t in got if t][:4] if isinstance(got, list) else []


def lane(row: dict) -> str:
    """``x`` for a tweet from the Grok X mails, ``market`` for a broad-tape headline (no ticker, #673), else
    ``stocks``."""
    if row.get("provider") == X_PROVIDER:
        return "x"
    return "stocks" if _tickers(row.get("tickers")) else "market"


_LANES = {"market": ("Piyasa", "Piyasa akışından henüz başlık yok."),
          "stocks": ("Hisseler", "Henüz başlık yok."),
          "x": ("X", "X özetlerinden henüz kayıt yok.")}
#: The tweet kinds of the X lane, in the board's Turkish (the JS keeps the same map).
X_KINDS = {"news": "haber", "level": "seviye", "option": "opsiyon", "macro": "makro", "position": "pozisyon",
           "other": "diğer"}


def _check(r: dict) -> dict:
    try:
        return json.loads(r["x_check"]) if r.get("x_check") else {}
    except ValueError:
        return {}


def x_lines(r: dict) -> tuple[str, str]:
    """An X row's contract chip and its fact line: the UW check for an option tweet, else Grok's option field."""
    state = _check(r)
    return x_check.chip(state), (x_check.facts(state) or (r.get("option") or ""))


def _curated(rows: list[dict]) -> list[dict]:
    """The X lane: one card a contract and session (the same print tweeted by two accounts is one card, the
    accounts joined), newest first."""
    out, seen = [], {}
    for r in rows:
        state = _check(r)
        key = (state.get("occ"), state.get("session")) if state.get("occ") else None
        if key and key in seen:
            first = seen[key]
            if r.get("source") and r["source"] not in first["source"]:
                first["source"] = f'{first["source"]}, {r["source"]}'
            continue
        r = dict(r)
        out.append(r)
        if key:
            seen[key] = r
    return out


def _head_li(r: dict) -> str:
    tk = "".join(f'<span class="tk">{esc(t)}</span>' for t in _tickers(r.get("tickers")))
    kind = f'<span class="xk">{esc(X_KINDS.get(r.get("kind") or "", r.get("kind")))}</span>' if r.get("kind") else ""
    src = f' · {esc(r["source"])}' if r.get("source") else ""
    text = esc(r.get("headline"))
    if r.get("url") and str(r["url"]).startswith(("https://", "http://")):
        text = f'<a href="{esc(r["url"])}" target="_blank" rel="noopener noreferrer">{text}</a>'
    chip, line = x_lines(r) if lane(r) == "x" else ("", "")
    if chip:
        tk = f'<span class="tk">{esc(chip)}</span>'
    opt = f'<div class="xo{" ok" if _check(r).get("status") == "seen" else ""}">{esc(line)}</div>' if line else ""
    return (f'<li class="hd" data-hid="{esc(r["headline_id"])}" data-at="{float(r["published_at"])}">'
            f'<div class="muted">{hm(r["published_at"])}{src} {kind}{tk}</div>'
            f'<div class="htext">{text}</div>{opt}</li>')


def _headlines(rows: list[dict]) -> str:
    """Three cards: Piyasa (the broad tape), Hisseler (per ticker) and X (the Grok X mails); the page's poll adds to
    their lists."""
    out = []
    for key, (title, empty) in _LANES.items():
        mine = [r for r in rows if lane(r) == key]
        mine = (_curated(mine) if key == "x" else mine)[:HEADLINE_LIMIT]
        out.append(f'<section class="card"><h2 class="lbl">{title} · son {HEADLINE_HOURS} saat</h2>'
                   f'<p class="empty"{" hidden" if mine else ""}>{empty}</p>'
                   f'<ul class="list" id="heads-{key}">{"".join(_head_li(r) for r in mine)}</ul></section>')
    return "".join(out)


def api_rows(since: float, limit: int = 2 * HEADLINE_LIMIT) -> list[dict]:
    """Headlines stored after ``since`` (their ``run_at``), newest first, as the poll draws them."""
    rows = rawstore.query(
        f"SELECT headline_id, provider, published_at, headline, source, tickers, url, kind, option, x_check, run_at "
        f"FROM {HEADLINES} "
        f"WHERE run_at > ? AND published_at >= ? ORDER BY published_at DESC LIMIT {int(limit)}",
        [float(since), time.time() - HEADLINE_HOURS * 3600], db="news")
    rows = [r for r in rows if not (lane(r) == "x" and r.get("kind") == "other")]
    return [{"headline_id": r["headline_id"], "published_at": r["published_at"], "hm": hm(r["published_at"]),
             "headline": r["headline"], "source": r.get("source") or "",
             "tickers": [x_lines(r)[0]] if lane(r) == "x" and x_lines(r)[0] else _tickers(r.get("tickers")),
             "kind": X_KINDS.get(r.get("kind") or "", r.get("kind") or ""),
             "option": x_lines(r)[1] if lane(r) == "x" else "",
             "url": r["url"] if str(r.get("url") or "").startswith(("https://", "http://")) else "",
             "lane": lane(r), "run_at": r["run_at"]} for r in rows]


_CSS = """
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1200px;margin:0 auto;padding:16px}
.top{display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px 16px;align-items:flex-end;margin-bottom:12px}
.eyebrow{color:var(--muted);font-size:12px;letter-spacing:.04em;text-transform:uppercase}
h1{margin:2px 0 0;color:var(--bright);font-size:22px}
.muted{color:var(--muted);font-size:12.5px}
.grid{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr);gap:16px}
.side{display:flex;flex-direction:column;gap:16px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
.lbl{margin:0 0 10px;font-size:12px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);font-weight:600}
.tabs{display:flex;gap:8px;margin-bottom:12px}
.tabs button{min-height:44px;padding:0 18px;border-radius:10px;border:1px solid var(--border);background:transparent;
 color:var(--fg);font:inherit;cursor:pointer}
.tabs button[aria-selected="true"]{background:var(--input-bg);color:var(--bright);border-color:var(--amber)}
.list{list-style:none;margin:0;padding:0}
.ev{display:grid;grid-template-columns:60px 1fr auto;gap:8px 12px;align-items:center;padding:10px 8px;
 border-bottom:1px solid var(--border);min-height:44px}
.ev.hl{border:1px solid var(--amber);border-radius:10px;background:var(--input-bg)}
.ev.hl .t{color:var(--amber)}
.ev[data-past="1"]{opacity:.55}
.t{font-variant-numeric:tabular-nums;color:var(--bright);font-weight:600}
.kind{font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.dots{color:var(--amber);letter-spacing:1px;font-size:11px}
.name{color:var(--bright)}
.vals{display:flex;gap:12px;font-size:12.5px;color:var(--muted);flex-wrap:wrap}
.vals b{color:var(--fg);font-weight:600}
.day{margin:14px 0 4px;font-size:13px;color:var(--muted);font-weight:600}
.feat-name{font-size:18px;color:var(--bright);font-weight:600;margin-bottom:2px}
.feat .vals{margin:10px 0}
.why{margin:8px 0 0;line-height:1.6}
.hd{padding:10px 0;border-bottom:1px solid var(--border);overflow-wrap:anywhere}
.htext a{color:var(--bright);text-decoration:none}
.htext a:hover{text-decoration:underline}
.tk{display:inline-block;margin-left:4px;padding:0 6px;border-radius:6px;background:var(--input-bg);
 color:var(--sky);font-size:11.5px}
.xk{display:inline-block;margin-left:4px;padding:0 6px;border-radius:6px;border:1px solid var(--border);
 color:var(--amber);font-size:11.5px}
.xo{margin-top:4px;color:var(--muted);font-size:12.5px;font-variant-numeric:tabular-nums}
.xo.ok{color:var(--fg);border-left:3px solid var(--amber);padding-left:8px}
.empty{color:var(--muted);margin:6px 0}
.foot{margin-top:16px;color:var(--muted);font-size:12px}
[hidden]{display:none!important}
@media (max-width:900px){.grid{grid-template-columns:minmax(0,1fr)}.feat{order:-1}}
@media (max-width:560px){.ev{grid-template-columns:60px 1fr}.vals{grid-column:2}.wrap{padding:12px}}
"""

_JS = """
function _headLi(r){
  var li = document.createElement('li'); li.className = 'hd';
  li.setAttribute('data-hid', r.headline_id); li.setAttribute('data-at', r.published_at);
  var m = document.createElement('div'); m.className = 'muted';
  m.textContent = r.hm + (r.source ? ' · ' + r.source : '') + ' ';
  if(r.kind){ var k = document.createElement('span'); k.className = 'xk'; k.textContent = r.kind; m.appendChild(k); }
  r.tickers.forEach(function(t){ var s = document.createElement('span'); s.className = 'tk'; s.textContent = t; m.appendChild(s); });
  var h = document.createElement('div'); h.className = 'htext';
  if(r.url){ var a = document.createElement('a'); a.href = r.url; a.target = '_blank'; a.rel = 'noopener noreferrer';
    a.textContent = r.headline; h.appendChild(a); } else { h.textContent = r.headline; }
  li.appendChild(m); li.appendChild(h);
  if(r.option){ var o = document.createElement('div'); o.className = r.option.indexOf('UW teyidi:') === 0 ? 'xo ok' : 'xo';
    o.textContent = r.option; li.appendChild(o); }
  return li;
}
function _addHead(r){
  if(document.querySelector('[data-hid="' + r.headline_id + '"]')) return;
  var ul = document.getElementById('heads-' + r.lane); if(!ul) return;
  var li = _headLi(r), kids = ul.children, i = 0;
  while(i < kids.length && parseFloat(kids[i].getAttribute('data-at')) > r.published_at) i++;
  ul.insertBefore(li, kids[i] || null);
  while(ul.children.length > HEAD_LIMIT) ul.removeChild(ul.lastChild);
  var empty = ul.parentNode.querySelector('.empty'); if(empty) empty.hidden = true;
}
function newsPoll(){
  fetch('/api/news/headlines?since=' + NEWS_SINCE, {cache: 'no-store'}).then(function(r){ return r.json(); })
    .then(function(d){
      if(!d || !d.ok) return;
      d.rows.forEach(function(r){ NEWS_SINCE = Math.max(NEWS_SINCE, r.run_at); _addHead(r); });
    }).catch(function(){});
}
setInterval(newsPoll, 60000);
function newsTab(btn){
  var tabs = document.querySelectorAll('.tabs button');
  for(var i = 0; i < tabs.length; i++){
    var on = tabs[i] === btn;
    tabs[i].setAttribute('aria-selected', on ? 'true' : 'false');
    document.getElementById(tabs[i].getAttribute('aria-controls')).hidden = !on;
  }
}
"""


HELP = help_html(
    "<p>ABD ekonomik takvimi (bugün ve bu hafta, saatler Amsterdam) ve izleme listesindeki ve en büyük ABD "
    "şirketlerinin Dow Jones başlıkları (<b>Hisseler</b>), IBKR'nin piyasa geneli haber akışı (<b>Piyasa</b>) ve "
    "Grok'un saatlik X özet maillerindeki paylaşımlar (<b>X</b>; her kaydın X kimliği kendi zamanıyla doğrulanır, "
    "\"diğer\" türü ve aynı kontratın tekrarları düşer). Bir opsiyon paylaşımının kontratı Unusual Whales'ta "
    "aranır: tweet saatinin 4 saat öncesinden 15 dakika sonrasına kadar o kontratın $25B üstü işlemleri (adet, "
    "kontrat, prim, ask/bid tarafı) ve ertesi gün açık pozisyon (OI) değişimi gösterilir. "
    "Gerçekleşen değer gösterilmez: beklenti ve önceki değer.</p>"
    "<p>Başlıklar 5 dakikada bir saklanır; sayfa açıkken dakikada bir yenileri kendisi ekler.</p>"
    "<p><b>Fetch</b> saklı takvimi <code>boards/news.duckdb</code> içine artımlı yazar (yeni ve değişen "
    "olaylar; listeden düşen olay silinmez, emekliye ayrılır) ve IBKR Gateway'den son başlıkları sorar. "
    "<b>Refresh</b> yalnızca saklı veriden sayfayı yeniden çizer, hiçbir şey indirmez.</p>")


def render_html(now: float | None = None, provider_health: dict | None = None) -> str:
    """The page; ``provider_health`` (a Fetch whose gateway did not answer) adds the "Data incomplete" banner."""
    now = time.time() if now is None else now
    events, hl, heads, stamp = _load(now)
    since = max((float(h["run_at"]) for h in heads if h.get("run_at") is not None), default=now)
    today = local_day(now)
    hl_key = (hl["event_ts"], hl["event"], hl["type"]) if hl else None
    today_html, week_html = _calendar(events, now, hl_key)
    n_today = sum(1 for e in events if local_day(e["event_ts"]) == today)
    updated = f"Veri {hm(stamp)}'de güncellendi" if stamp else "Veri henüz kurulmadı"
    return f"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Haberler</title><script>{THEME_JS}</script><style>{THEME_CSS}{_CSS}</style></head>
<body><div class="wrap">
<header class="top"><div><div class="eyebrow">ABD · Ekonomik takvim</div><h1>{long_date(today)}</h1>
<div class="muted">{WEEKDAYS[today.weekday()]} · bugün {n_today} veri</div></div>
<div class="muted">{updated}</div></header>
{provider_health_html(provider_health)}
<div class="grid">
<section class="card"><div class="tabs" role="tablist">
<button type="button" role="tab" aria-selected="true" aria-controls="news-today" onclick="newsTab(this)">Bugün</button>
<button type="button" role="tab" aria-selected="false" aria-controls="news-week" onclick="newsTab(this)">Bu hafta</button>
</div><div id="news-today" role="tabpanel">{today_html}</div>
<div id="news-week" role="tabpanel" hidden>{week_html}</div></section>
<div class="side">{_highlight(hl)}{_headlines(heads)}</div>
</div>
<p class="foot">Kaynak: takvim Unusual Whales, başlıklar Dow Jones (IBKR), X kayıtları Grok özetleri · Saatler Europe/Amsterdam · Önem: ●●● yüksek ●●○ orta ●○○ düşük</p>
{HELP}
</div><script>var NEWS_SINCE = {since!r}, HEAD_LIMIT = {HEADLINE_LIMIT};{_JS}</script></body></html>"""


def render(now: float | None = None, provider_health: dict | None = None) -> Path:
    """Write the board file ``.cache/news_latest.html`` and return its path."""
    return write_artifact(Path(OUTPUT_DIR) / FILENAME, render_html(now, provider_health))
