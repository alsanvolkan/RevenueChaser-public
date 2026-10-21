"""Discord board HTML: channels on the left (follow, counts, Update), the message feed on the right.

Every string here is third-party text (message bodies, author and channel names, embed
text, file names). It reaches the page only through ``core.html_safe`` (ADR-X1, #144):
``esc`` for markup and attributes, ``script_json`` for the one inline config block. URLs
that come from a message are only linked when they are http(s), and media paths are
percent-encoded under ``/discord/media/``.
"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import quote, urlencode

from RevenueChaser.core.html_safe import esc, fill, script_json
from RevenueChaser.core.reporting_common import help_html, provider_health_html

MEDIA_URL = "/discord/media/"

CSS = """
:root{--bg:#05111F;--panel:#12161e;--panel2:#1a1f2b;--border:#232a38;--text:#d7dce5;--muted:#8a93a6;
--accent:#0EA5E9;--green:#2ecc71;--amber:#f1c40f;--neon:#00e6a8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;font-size:14px;line-height:1.5}
.wrap{max-width:100%;margin:0 auto;padding:20px 24px 60px}
.topbar{display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:10px;border-bottom:1px solid var(--border);padding-bottom:12px;margin-bottom:14px}
.topbar h1{margin:0;font-size:22px;letter-spacing:-.4px}
.topbar .sub{color:var(--muted);margin-top:2px;font-size:13px}
.sync{color:var(--muted);font-size:12.5px;white-space:nowrap}
.layout{display:grid;grid-template-columns:300px 1fr;gap:16px;align-items:start}
@media (max-width:900px){.layout{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:12px 14px}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
button{background:#10151f;border:1px solid #2a3345;color:var(--text);border-radius:6px;height:30px;padding:0 10px;font:600 12px/1 inherit;cursor:pointer}
button:hover{border-color:var(--accent)}
.status{color:var(--muted);font-size:12px;min-height:16px;margin-bottom:6px}
.status.err{color:#e74c3c}
.guild{font-weight:700;margin:10px 0 2px}
.cat{color:var(--muted);font-size:11.5px;text-transform:uppercase;letter-spacing:.04em;margin:8px 0 2px}
.ch{display:flex;align-items:center;gap:6px;padding:2px 0}
.ch .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cnt{color:var(--muted);font-size:11px}
.upd{height:22px;padding:0 7px;font-size:11px}
.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:12px}
select,input[type=text],input[type=date]{background:#10151f;border:1px solid #2a3345;color:var(--text);border-radius:6px;height:32px;padding:0 8px;font:500 13px/1 inherit}
input[type=text]{min-width:200px;flex:1}
.count{color:var(--muted);font-size:12px;margin-left:auto}
.msg{border-bottom:1px solid var(--border);padding:9px 0}
.msg.reply{margin-left:28px;border-left:2px solid var(--accent);padding-left:10px}
.meta{font-size:12px;color:var(--muted)}
.meta b{color:var(--text)}
.rp{font-size:12px;color:var(--muted);border-left:2px solid var(--border);padding-left:8px;margin:2px 0}
.body{white-space: pre-wrap;word-wrap:break-word;overflow-wrap:anywhere}
.chip{display:inline-block;background:var(--panel2);border:1px solid var(--border);border-radius:20px;padding:0 8px;font-size:11px;color:var(--muted);margin-left:4px}
.thumb{max-width:260px;max-height:180px;border-radius:6px;border:1px solid var(--border);margin:4px 6px 0 0}
.file{display:inline-block;margin:4px 8px 0 0;font-size:12.5px}
.embed{border-left:3px solid var(--accent);background:var(--panel2);border-radius:4px;padding:6px 10px;margin-top:5px;font-size:13px}
.embed .et{font-weight:700}
.embed .ef{color:var(--muted);font-size:12px}
.rx{display:inline-block;background:var(--panel2);border:1px solid var(--border);border-radius:12px;padding:0 8px;margin:4px 6px 0 0;font-size:12px}
.pager{display:flex;gap:14px;align-items:center;justify-content:center;margin-top:14px;color:var(--muted);font-size:13px}
a{color:var(--accent)}
.empty{color:var(--muted);padding:24px 4px}
"""

JS = """
var CFG = __CFG__;
function status(msg, err){ var s = document.getElementById('status'); s.textContent = msg || ''; s.className = 'status' + (err ? ' err' : ''); }
function runJob(btn){
  var body = new URLSearchParams();
  if(btn.dataset.channel){ body.set('channel', btn.dataset.channel); }
  status('Starting...');
  fetch(btn.dataset.run, {method:'POST', headers:{'X-RC-Panel':'discord'}, body: body}).then(function(r){ return r.json(); }).then(function(d){
    if(!d.ok){ status(d.error || 'Failed to start', true); return; }
    status('Started. Refresh when the job is done.');
    try{ parent.postMessage({type:'rc-job-submitted', job_id: d.job_id}, location.origin); }catch(e){}
  }).catch(function(){ status('Could not reach the dashboard', true); });
}
function follow(box){
  var want = box.checked;
  fetch(CFG.follow, {method:'POST', headers:{'Content-Type':'application/json', 'X-RC-Panel':'discord'},
        body: JSON.stringify({channel_id: box.dataset.channel, enabled: want})})
    .then(function(r){ return r.json(); }).then(function(d){
      if(!d.ok){ box.checked = !want; status(d.error || 'Could not save', true); }
    }).catch(function(){ box.checked = !want; status('Could not save', true); });
}
document.addEventListener('click', function(e){
  var b = e.target.closest('button[data-run]'); if(b){ runJob(b); }
});
document.addEventListener('change', function(e){
  if(e.target.classList && e.target.classList.contains('follow')){ follow(e.target); }
});
"""


def _friendly(ts) -> str:
    """'Friday, October 02, 2026 07:55 UTC' from an ISO timestamp (never the raw string)."""
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).strftime("%A, %B %d, %Y %H:%M UTC")
    except ValueError:
        return str(ts)


def _http_url(url) -> str | None:
    u = str(url or "").strip()
    return u if u.lower().startswith(("http://", "https://")) else None


def _media_href(rel: str) -> str:
    return MEDIA_URL + quote(str(rel), safe="/")


def _channels_panel(channels: list[dict]) -> str:
    if not channels:
        return '<div class="empty">No channels yet. Click Discover to list the server\'s channels.</div>'
    out: list[str] = []
    guild = cat = object()
    for c in channels:
        g = (c.get("guild_id"), c.get("guild_name"))
        if g != guild:
            guild, cat = g, object()
            out.append(f'<div class="guild">{esc(c.get("guild_name") or c.get("guild_id") or "Unknown server")}</div>')
        if (c.get("category") or "") != cat:
            cat = c.get("category") or ""
            if cat:
                out.append(f'<div class="cat">{esc(cat)}</div>')
        cid = esc(c.get("channel_id"))
        checked = " checked" if c.get("enabled") else ""
        out.append(
            f'<div class="ch"><input type="checkbox" class="follow" data-channel="{cid}"{checked} '
            f'title="Follow: include in Update"><span class="nm">{esc(c.get("name") or c.get("channel_id"))}</span>'
            f'<span class="cnt">{esc(c.get("message_count"))}</span>'
            f'<button class="upd" data-channel="{cid}" data-run="/run/module_discord">Update</button></div>')
    return "\n".join(out)


def _embed(e: dict) -> str:
    title = esc(e.get("title") or "")
    link = _http_url(e.get("url"))
    head = (f'<a href="{esc(link)}" target="_blank" rel="noopener noreferrer">{title}</a>'
            if link and title else title)
    parts = []
    if e.get("author"):
        parts.append(f'<div class="ef">{esc(e["author"])}</div>')
    if head:
        parts.append(f'<div class="et">{head}</div>')
    if e.get("description"):
        parts.append(f'<div class="body">{esc(e["description"])}</div>')
    for f in e.get("fields") or []:
        parts.append(f'<div class="body"><b>{esc(f.get("name"))}</b> {esc(f.get("value"))}</div>')
    if e.get("footer"):
        parts.append(f'<div class="ef">{esc(e["footer"])}</div>')
    return f'<div class="embed">{"".join(parts)}</div>'


def _attachment(a: dict) -> str:
    name, rel = esc(a.get("file_name") or "file"), a.get("media_rel")
    if not rel:
        return f'<span class="file">{name}</span>'
    href = esc(_media_href(rel))
    if a.get("is_image"):
        return (f'<a href="{href}" target="_blank" rel="noopener">'
                f'<img class="thumb" src="{href}" alt="{name}" loading="lazy"></a>')
    return f'<a class="file" href="{href}" target="_blank" rel="noopener">{name}</a>'


def _message(m: dict) -> str:
    is_reply = bool(m.get("reply_to_id")) and m.get("kind") == "Reply"
    parts = [f'<div class="msg{" reply" if is_reply else ""}" data-message="{esc(m.get("message_id"))}">']
    pin = '<span class="chip">pinned</span>' if m.get("is_pinned") else ""
    bot = '<span class="chip">bot</span>' if m.get("is_bot") else ""
    parts.append(f'<div class="meta"><b>{esc(m.get("author_name"))}</b>{bot} · {esc(m.get("ts_label"))} · '
                 f'#{esc(m.get("channel_name"))}{pin}</div>')
    rp = m.get("reply_preview")
    if is_reply and rp:
        parts.append(f'<div class="rp">↳ {esc(rp.get("author"))}: {esc(rp.get("text"))}</div>')
    if m.get("content"):
        parts.append(f'<div class="body">{esc(m["content"])}</div>')
    parts += [_attachment(a) for a in m.get("attachments") or []]
    parts += [_embed(e) for e in m.get("embeds") or []]
    for r in m.get("reactions") or []:
        parts.append(f'<span class="rx">{esc(r.get("emoji"))} {esc(r.get("count"))}</span>')
    parts.append("</div>")
    return "".join(parts)


def _query(filters: dict, page: int) -> str:
    q = {k: v for k, v in (filters or {}).items() if v}
    q["page"] = page
    return "?" + urlencode(q)


def _filters_form(p: dict) -> str:
    f = p.get("filters") or {}
    opts = ['<option value="">All channels</option>']
    for c in p.get("channels") or []:
        sel = " selected" if str(c.get("channel_id")) == str(f.get("channel")) else ""
        opts.append(f'<option value="{esc(c.get("channel_id"))}"{sel}>{esc(c.get("name") or c.get("channel_id"))}</option>')
    return (
        '<form class="filters" method="get">'
        f'<select name="channel">{"".join(opts)}</select>'
        f'<input type="date" name="date_from" value="{esc(f.get("date_from"))}" title="From">'
        f'<input type="date" name="date_to" value="{esc(f.get("date_to"))}" title="To">'
        f'<input type="text" name="q" placeholder="Search text" value="{esc(f.get("q"))}">'
        '<button type="submit">Filter</button>'
        f'<span class="count">{esc(p.get("total", 0))} messages</span></form>')


def _pager(p: dict) -> str:
    page, pages = int(p.get("page") or 1), int(p.get("pages") or 1)
    if pages <= 1:
        return ""
    f = p.get("filters") or {}
    prev = (f'<a href="{esc(_query(f, page - 1))}" rel="prev">&laquo; Newer</a>' if page > 1 else "")
    nxt = (f'<a href="{esc(_query(f, page + 1))}" rel="next">Older &raquo;</a>' if page < pages else "")
    return f'<div class="pager">{prev}<span>Page {page} of {pages}</span>{nxt}</div>'


def _feed(p: dict) -> str:
    msgs = p.get("messages") or []
    if msgs:
        return "".join(_message(m) for m in msgs)
    f = p.get("filters") or {}
    if any(f.values()):
        return '<div class="empty">No messages match these filters.</div>'
    if not p.get("channels"):
        return '<div class="empty">Nothing archived yet. Discover the server\'s channels first.</div>'
    return '<div class="empty">No messages archived yet. Follow a channel, then click Update.</div>'


def render_discord_board(p: dict) -> str:
    cfg = script_json({"follow": "/api/discord/follow"})
    sync = p.get("last_sync")
    help_block = help_html(
        "An archive of the Discord channels you follow: channels on the left, their messages on the right. "
        "Every message is third-party text, shown as plain text; times are UTC. <b>Discover</b> lists the "
        "server's channels. <b>Update all</b> (or a channel's Update) is the Fetch here: it runs "
        "<b>DiscordChatExporter</b> for the followed channels and stores the new messages. "
        "Loading the page, Refresh and the filters read the stored archive only and never reach Discord.")
    body = f"""<div class="wrap">
<div class="topbar"><div><h1>Discord</h1>
<div class="sub">Archive of followed channels (DiscordChatExporter). Third-party text, shown as plain text. Times in UTC.</div></div>
<div class="sync">Last sync: {esc(_friendly(sync)) if sync else "never"}</div></div>
{provider_health_html(p.get("provider_health"))}
{help_block}
<div class="layout">
<div class="panel"><div class="actions">
<button data-run="/run/module_discord_discover">Discover</button>
<button data-run="/run/module_discord">Update all</button></div>
<div id="status" class="status"></div>
{_channels_panel(p.get("channels") or [])}</div>
<div class="panel">{_filters_form(p)}{_feed(p)}{_pager(p)}</div>
</div></div>"""
    page = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Discord</title>'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<style>{CSS}</style></head><body>__BODY__<script>{JS}</script></body></html>")
    return fill(page, {"__BODY__": body, "__CFG__": cfg})
