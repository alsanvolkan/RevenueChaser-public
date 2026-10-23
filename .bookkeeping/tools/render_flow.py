"""Render the data-flow record from flow_data.py.

Usage, from the repo root:
    python .bookkeeping/tools/render_flow.py              # writes .bookkeeping/data_flow.md and data_flow.svg
    python .bookkeeping/tools/render_flow.py --html PATH  # also writes the browsable page (published as an Artifact)
"""
import argparse
import html
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from flow_data import (AREAS, AS_OF, CHART_J2S, CHART_JOBS, CHART_S2J, CHART_SOURCES,  # noqa: E402
                       CHART_STORES, MODES, STORES)

TOKENS = """:root{
  --bg:#f4f6f8; --panel:#ffffff; --fg:#17202b; --muted:#5a6676; --line:#d5dce5;
  --raw:#1f5fa6; --raw-bg:#e7f0fa; --edgar:#2b7a4b; --edgar-bg:#e5f4ea; --discord:#6a4bb0; --discord-bg:#efeafb;
  --files:#9a5a12; --files-bg:#faf0e2; --none:#a8402f; --none-bg:#fbe9e6; --accent:#1f5fa6;
  --sans:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif; --mono:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#11161d; --panel:#18202a; --fg:#e4e9ef; --muted:#9aa6b4; --line:#2b3644;
  --raw:#7fb2ec; --raw-bg:#17283c; --edgar:#7cc99a; --edgar-bg:#15291e; --discord:#b5a0ee; --discord-bg:#241d38;
  --files:#e2a865; --files-bg:#33240f; --none:#ee8f7f; --none-bg:#3a1c17; --accent:#7fb2ec; color-scheme:dark}}
"""

SX, SW, JX, JW, TX, TW = 10, 180, 300, 270, 680, 390
H = 30

def jy(i): return 50 + i * 40
# sources spread over the height of the jobs column (86 px apart at most)
def sy(k): return 50 + k * min(86, (jy(len(CHART_JOBS) - 1) - 50) / max(1, len(CHART_SOURCES) - 1))

def curve(x1, y1, x2, y2, cls):
    mx = (x1 + x2) / 2
    return f'<path class="{cls}" d="M{x1},{y1} C{mx},{y1} {mx},{y2} {x2},{y2}"/>'

def svg(standalone=False):
    height = max(700, jy(len(CHART_JOBS) - 1) + H + 20,
                 max(y0 + h for y0, h, *_ in CHART_STORES.values()) + 20)
    head = f'<svg xmlns="http://www.w3.org/2000/svg" class="flowsvg" viewBox="0 0 1080 {height}" role="img" aria-label="Sources feed jobs, jobs write to raw.duckdb, edgar.duckdb, discord.duckdb or files">'
    o = [head]
    if standalone:
        o.append("<style>" + TOKENS + CSS.replace(".flowsvg{width:100%;min-width:760px;height:auto;display:block}", "") + "</style>")
        o.append(f'<rect width="1080" height="{height}" fill="var(--bg)"/>')
    for x, t in ((SX, "SOURCES"), (JX, "JOBS THAT WRITE"), (TX, "WHERE IT IS STORED")):
        o.append(f'<text class="hd" x="{x}" y="30">{t}</text>')
    jpos = {k: i for i, (k, _) in enumerate(CHART_JOBS)}
    spos = {k: i for i, (k, _) in enumerate(CHART_SOURCES)}
    for s, js in CHART_S2J.items():
        for j in js:
            o.append(curve(SX + SW, sy(spos[s]) + H / 2, JX, jy(jpos[j]) + H / 2, "e-src"))
    for j, ts in CHART_J2S.items():
        for t in ts:
            y0, h, *_ = CHART_STORES[t]
            yj = jy(jpos[j]) + H / 2
            yt = min(max(yj, y0 + 10), y0 + h - 10)
            o.append(curve(JX + JW, yj, TX, yt, f"e-{t}"))
    for i, (k, lbl) in enumerate(CHART_SOURCES):
        o.append(f'<rect class="b-src" x="{SX}" y="{sy(i)}" width="{SW}" height="{H}" rx="4"/><text class="t" x="{SX+10}" y="{sy(i)+20}">{E(lbl)}</text>')
    for i, (k, lbl) in enumerate(CHART_JOBS):
        cls = "b-job b-dash" if k == "ER" else "b-job"
        o.append(f'<rect class="{cls}" x="{JX}" y="{jy(i)}" width="{JW}" height="{H}" rx="4"/><text class="t" x="{JX+10}" y="{jy(i)+20}">{E(lbl)}</text>')
    for k, (y0, h, title, lines) in CHART_STORES.items():
        o.append(f'<rect class="b-st s-{k}" x="{TX}" y="{y0}" width="{TW}" height="{h}" rx="5"/>')
        o.append(f'<text class="tt c-{k}" x="{TX+12}" y="{y0+20}">{E(title)}</text>')
        for n, line in enumerate(lines):
            if h <= 40:
                o[-1] = o[-1]  # single-line boxes keep the title only
                o.append(f'<text class="ts" x="{TX+135}" y="{y0+20}">{E(line)}</text>')
            else:
                step = 27 if k == 'raw' else 22
                o.append(f'<text class="ts" x="{TX+12}" y="{y0+42+n*step}">{E(line)}</text>')
    o.append("</svg>")
    return "".join(o)

CSS = """
.flowsvg{width:100%;min-width:760px;height:auto;display:block}
.flowsvg .hd{font:600 11px var(--sans);letter-spacing:.08em;fill:var(--muted)}
.flowsvg .t{font:13px var(--sans);fill:var(--fg)}
.flowsvg .tt{font:600 14px var(--mono)}
.flowsvg .ts{font:12px var(--mono);fill:var(--muted)}
.flowsvg .b-src,.flowsvg .b-job{fill:var(--panel);stroke:var(--line);stroke-width:1}
.flowsvg .b-dash{stroke-dasharray:4 3;stroke:var(--none)}
.flowsvg path{fill:none;stroke-width:1.4;opacity:.75}
.flowsvg .e-src{stroke:var(--muted);opacity:.35}
.flowsvg .e-raw{stroke:var(--raw)} .flowsvg .e-edgar{stroke:var(--edgar)} .flowsvg .e-files{stroke:var(--files)}
.flowsvg .e-discord{stroke:var(--discord)} .flowsvg .e-none{stroke:var(--none);stroke-dasharray:4 3}
.flowsvg .b-st{stroke-width:1.5}
.flowsvg .s-raw{fill:var(--raw-bg);stroke:var(--raw)} .flowsvg .s-edgar{fill:var(--edgar-bg);stroke:var(--edgar)}
.flowsvg .s-files{fill:var(--files-bg);stroke:var(--files)} .flowsvg .s-discord{fill:var(--discord-bg);stroke:var(--discord)}
.flowsvg .s-none{fill:var(--none-bg);stroke:var(--none);stroke-dasharray:4 3}
.flowsvg .c-raw{fill:var(--raw)} .flowsvg .c-edgar{fill:var(--edgar)} .flowsvg .c-files{fill:var(--files)}
.flowsvg .c-discord{fill:var(--discord)} .flowsvg .c-none{fill:var(--none)}
"""


E = html.escape
DBLABEL = {"raw": "raw.duckdb", "edgar": "edgar.duckdb", "discord": "discord.duckdb", "prices": "prices.duckdb",
           "files": "file on disk", "none": "not stored"}



def chip(db):
    return f'<span class="db db-{db}">{E(DBLABEL[db])}</span>'


def cite(c):
    return f'<span class="cite">{E(c)}</span>' if c else ""


def flow_html(f):
    src, src_c, wr, wr_c, db, table, table_c, key, mode, kept, kept_c = f
    if db == "none" and not wr_c:
        writer = '<div class="node node-none"><div class="lbl">Writer</div><div class="v">none</div></div>'
    else:
        writer = f'<div class="node"><div class="lbl">Writer</div><div class="v">{E(wr) or "none"}</div>{cite(wr_c)}</div>'
    keymode = ""
    if key or mode:
        parts = []
        if key:
            parts.append(f'key <code>{E(key)}</code>')
        if mode:
            rule = MODES.get(mode) or (MODES["replace per"] if mode.startswith("replace per") else "")
            parts.append(f'<span class="mode" title="{E(rule)}">{E(mode)}</span>')
        keymode = '<div class="km">' + " · ".join(parts) + "</div>"
    return f"""<div class="flow">
  <div class="node"><div class="lbl">Source</div><div class="v">{E(src)}</div>{cite(src_c)}</div>
  <div class="arrow" aria-hidden="true"></div>
  {writer}
  <div class="arrow" aria-hidden="true"></div>
  <div class="node node-store store-{db}"><div class="lbl">Stored in {chip(db)}</div><div class="v"><code>{E(table)}</code></div>{cite(table_c)}{keymode}</div>
  <div class="arrow" aria-hidden="true"></div>
  <div class="node node-kept"><div class="lbl">How long it stays</div><div class="v">{E(kept) or "n/a"}</div>{cite(kept_c)}</div>
</div>"""


def build_html():
    out = []
    out.append("""<title>RevenueChaser Data Flow</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* Layout: one column of areas; each dataset is a left-to-right pipeline that stacks on phones. */
:root{
  --bg:#f4f6f8; --panel:#ffffff; --fg:#17202b; --muted:#5a6676; --line:#d5dce5;
  --raw:#1f5fa6; --raw-bg:#e7f0fa; --edgar:#2b7a4b; --edgar-bg:#e5f4ea; --discord:#6a4bb0; --discord-bg:#efeafb;
  --files:#9a5a12; --files-bg:#faf0e2; --none:#a8402f; --none-bg:#fbe9e6; --accent:#1f5fa6;
  --sans:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif; --mono:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#11161d; --panel:#18202a; --fg:#e4e9ef; --muted:#9aa6b4; --line:#2b3644;
  --raw:#7fb2ec; --raw-bg:#17283c; --edgar:#7cc99a; --edgar-bg:#15291e; --discord:#b5a0ee; --discord-bg:#241d38;
  --files:#e2a865; --files-bg:#33240f; --none:#ee8f7f; --none-bg:#3a1c17; --accent:#7fb2ec; color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#11161d; --panel:#18202a; --fg:#e4e9ef; --muted:#9aa6b4; --line:#2b3644;
  --raw:#7fb2ec; --raw-bg:#17283c; --edgar:#7cc99a; --edgar-bg:#15291e; --discord:#b5a0ee; --discord-bg:#241d38;
  --files:#e2a865; --files-bg:#33240f; --none:#ee8f7f; --none-bg:#3a1c17; --accent:#7fb2ec; color-scheme:dark}
body{background:var(--bg);color:var(--fg);font-family:var(--sans);font-size:14px;line-height:1.5}
.wrap{max-width:1180px;margin:0 auto;padding-inline:16px;padding-block:24px 48px;display:grid;gap:28px}
h1{font-size:26px;font-weight:600;margin:0;text-wrap:balance}
h2{font-size:19px;font-weight:600;margin:0 0 12px;text-wrap:balance}
h3{font-size:15px;font-weight:600;margin:0}
p{margin:0;max-width:72ch}
.sub{color:var(--muted)}
code{font-family:var(--mono);font-size:12.5px}
.legend{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.db{display:inline-block;text-transform:none;letter-spacing:0;font-family:var(--mono);font-size:11.5px;font-weight:500;padding:1px 7px;border-radius:4px;border:1px solid currentColor;white-space:nowrap}
.db-raw{color:var(--raw);background:var(--raw-bg)} .db-edgar{color:var(--edgar);background:var(--edgar-bg)}
.db-discord{color:var(--discord);background:var(--discord-bg)} .db-files{color:var(--files);background:var(--files-bg)}
.db-none{color:var(--none);background:var(--none-bg)}
.stores{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.store{background:var(--panel);border:1px solid var(--line);border-top:3px solid var(--c);border-radius:6px;padding:14px;display:grid;gap:10px;align-content:start;min-width:0}
.store h3{color:var(--c);font-family:var(--mono)}
.store .grp{display:grid;gap:4px}
.store .gl{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.tags{display:flex;flex-wrap:wrap;gap:4px}
.tags code{background:var(--bg);border:1px solid var(--line);border-radius:3px;padding:0 5px;font-size:11.5px;overflow-wrap:anywhere}
.diagram{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:12px;overflow-x:auto}
.area{display:grid;gap:14px}
.ds{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:14px;display:grid;gap:10px;min-width:0}
.ds-head{display:flex;flex-wrap:wrap;gap:6px 12px;align-items:baseline}
.ds-id{font-family:var(--mono);font-weight:500;color:var(--accent)}
.readers{color:var(--muted);font-size:13px}
.flow{display:grid;grid-template-columns:minmax(0,1fr) 18px minmax(0,1fr) 18px minmax(0,1.2fr) 18px minmax(0,1.2fr);align-items:stretch}
.node{border:1px solid var(--line);border-radius:5px;padding:8px 10px;display:grid;gap:3px;align-content:start;min-width:0;background:var(--panel)}
.node .lbl{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.node .v{overflow-wrap:anywhere}
.node-store{border-width:1px 1px 1px 4px}
.store-raw{border-left-color:var(--raw)} .store-edgar{border-left-color:var(--edgar)} .store-discord{border-left-color:var(--discord)}
.store-files{border-left-color:var(--files)} .store-none{border-left-color:var(--none);border-style:dashed}
.node-none{border-style:dashed;color:var(--muted)}
.cite{font-family:var(--mono);font-size:11px;color:var(--muted);overflow-wrap:anywhere}
.km{font-size:12px;color:var(--muted)}
.mode{border-bottom:1px dotted var(--muted);cursor:help}
.arrow{position:relative}
.arrow::after{content:"";position:absolute;top:50%;left:3px;right:3px;border-top:2px solid var(--muted);opacity:.6}
.arrow::before{content:"";position:absolute;top:calc(50% - 4px);right:2px;border:5px solid transparent;border-left-color:var(--muted);border-right:0;opacity:.6}
.note,.planned{font-size:13px}
.planned{border-top:1px dashed var(--line);padding-top:8px}
.planned b{font-weight:600}
.flows{display:grid;gap:8px}
table.modes{border-collapse:collapse;font-size:13px}
table.modes td{padding:4px 10px 4px 0;vertical-align:top;border-bottom:1px solid var(--line)}
ul.unk{margin:0;padding-left:18px;display:grid;gap:4px;max-width:80ch}
__SVGCSS__
@media (max-width:820px){
  .flow{grid-template-columns:minmax(0,1fr);grid-auto-rows:auto}
  .arrow{height:14px}
  .arrow::after{top:0;bottom:0;left:50%;right:auto;border-top:0;border-left:2px solid var(--muted)}
  .arrow::before{top:auto;bottom:0;right:auto;left:calc(50% - 4px);border:5px solid transparent;border-top-color:var(--muted);border-bottom:0}
}
</style>
<div class="wrap">
<header style="display:grid;gap:8px">
  <h1>RevenueChaser Data Flow</h1>
  <p class="sub">Where each dataset is stored today: the DuckDB file and table, the function that writes it, its source, its key and write rule, and how long it stays. Source basis: __ASOF__. The last line of each card is the step 2.3 draft target. Grey text is the source of each fact: a file:line, a manifest line (<code>:NNNN</code> = core/rawstore.py) or a lineage row (R2, C2, O2).</p>
  <div class="legend"><span class="sub">Stored in:</span>""")
    for k in ("raw", "edgar", "discord", "files", "none"):
        out.append(chip(k))
    out.append("""</div>
</header>

<section>
  <h2>The big picture</h2>
  <div class="diagram">""" + svg() + """</div>
  <p class="sub" style="margin-top:8px">Each line is a write path found in the code. Colored lines show which file a job writes; the dashed red box is data that is read during a run and not stored.</p>
</section>

<section>
  <h2>What each store file holds</h2>
  <div class="stores">""")
    colors = {"raw": "var(--raw)", "edgar": "var(--edgar)", "discord": "var(--discord)", "files": "var(--files)",
              "history": "var(--files)", "prices": "var(--files)"}
    for key, name, path, groups in STORES:
        out.append(f'<div class="store" style="--c:{colors[key]}"><h3>{E(name)}</h3><div class="cite">{E(path)}</div>')
        for gname, items in groups:
            tags = "".join(f"<code>{E(i)}</code>" for i in items)
            out.append(f'<div class="grp"><div class="gl">{E(gname)} · {len(items)}</div><div class="tags">{tags}</div></div>')
        out.append("</div>")
    out.append("""</div>
  <p class="sub" style="margin-top:10px">Backups: the RC-Store-Backup task backs up raw.duckdb, edgar.duckdb, discord.duckdb and prices.duckdb (2 snapshots, no weekly copy, #156) (and history.duckdb once it exists) every day at 04:00 with --keep 5, mirrors the Discord media and SPX Gamma trees, keeps the first raw snapshot of each week and, when RAW_STORE_BACKUP_MIRROR is set, copies it off this machine, the big raw/edgar files at most once a week, prices last and once a week with 1 copy kept (#156), and the weekly copies not at all (#349; core/automations.py:118-123; scripts/backup_store.py offmachine_copy).</p>
</section>""")

    for area, dss in AREAS:
        out.append(f'<section class="area"><h2>{E(area)}</h2>')
        for d in dss:
            out.append(f'<article class="ds"><div class="ds-head"><span class="ds-id">{E(d["id"])}</span><h3>{E(d["name"])}</h3><span class="readers">Read by: {E(d["readers"])}</span></div><div class="flows">')
            for f in d["flows"]:
                out.append(flow_html(f))
            out.append("</div>")
            if d.get("note"):
                out.append(f'<p class="note sub">{E(d["note"])}</p>')
            out.append(f'<p class="planned"><b>Planned (2.3 draft):</b> {E(d["planned"])}</p></article>')
        out.append("</section>")

    out.append("""<section>
  <h2>Write rules</h2>
  <table class="modes"><tbody>""")
    for k, v in MODES.items():
        out.append(f"<tr><td><code>{E(k)}</code></td><td>{E(v)}</td></tr>")
    out.append("""</tbody></table>
</section>

<section>
  <h2>Not known yet</h2>
  <ul class="unk">
    <li>Today, raw rows of the main datasets carry no <code>source</code> column; only identity_source_cache, ticker_scores and the declared-but-empty tables have one (manifest).</li>
    <li>Which .duckdb file each new step 2.3 table goes to is decided in step 2.6, as is a possible catalyst.duckdb.</li>
    <li>How long new raw tables keep history (for example price bars beyond 2 years) is one of your 2.5 decisions.</li>
    <li>The size each table reaches on your PC was not measured; the cloud has no copy of <code>.store/</code>.</li>
  </ul>
</section>
</div>""")
    return "\n".join(out).replace("__SVGCSS__", CSS).replace("__ASOF__", AS_OF.replace("&", "&amp;").replace("<", "&lt;"))


def build_md():
    L = ["# Storage flow today", "",
         "Per dataset: the source, the function that writes it, the DuckDB file and table, the key and write rule, and how long the rows stay. "
         f"Source basis: {AS_OF}; no store read or network call. `:NNNN` = core/rawstore.py manifest line; "
         "R2/C2/O2 = lineage v2 rows; uw.py = core/providers/unusual_whales.py. Page view: see the RevenueChaser Data Flow artifact.", "",
         "**Write rules:** " + "; ".join(f"`{k}` = {v}" for k, v in MODES.items()) + ".", ""]
    L.append("## Store files"); L.append("")
    for key, name, path, groups in STORES:
        L.append(f"- **{name}** ({path})")
        for gname, items in groups:
            L.append(f"  - {gname} ({len(items)}): " + ", ".join(f"`{i}`" for i in items))
    L.append("- Backups: RC-Store-Backup backs up raw, edgar, discord and prices.duckdb (2 snapshots, no weekly copy, #156) (and history.duckdb once it exists) daily at 04:00 with --keep 5, mirrors the Discord media and SPX Gamma trees, keeps the first raw snapshot of each week and, when RAW_STORE_BACKUP_MIRROR is set, copies it off this machine, the big raw/edgar files at most once a week, prices last and once a week with 1 copy kept (#156), and the weekly copies not at all (#349; core/automations.py:118-123; scripts/backup_store.py offmachine_copy).")
    L.append("")
    for area, dss in AREAS:
        L.append(f"## {area}"); L.append("")
        L.append("| ID | Source | Writer (function, file:line) | File | Table | Key | Write | How long it stays |")
        L.append("|---|---|---|---|---|---|---|---|")
        for d in dss:
            for i, f in enumerate(d["flows"]):
                src, src_c, wr, wr_c, db, table, table_c, key, mode, kept, kept_c = f
                idc = f"{d['id']} {d['name']}" if i == 0 else ""
                s = src + (f" ({src_c})" if src_c else "")
                w = (wr or "none") + (f" ({wr_c})" if wr_c else "")
                t = table + (f" ({table_c})" if table_c else "")
                k = kept + (f" ({kept_c})" if kept_c else "")
                row = [idc, s, w, DBLABEL[db], t, key, mode, k]
                L.append("| " + " | ".join(c.replace("|", "/") for c in row) + " |")
        L.append("")
        for d in dss:
            extra = f"- **{d['id']}** read by {d['readers']}. Planned: {d['planned']}"
            if d.get("note"):
                extra += f" Note: {d['note']}"
            L.append(extra)
        L.append("")
    return "\n".join(L)




def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--html", help="also write the browsable page to this path")
    args = ap.parse_args()
    root = HERE.parent
    (root / "data_flow.md").write_text(build_md(), encoding="utf-8")
    (root / "data_flow.svg").write_text(svg(standalone=True), encoding="utf-8")
    if args.html:
        Path(args.html).write_text(build_html(), encoding="utf-8")
    print("wrote", root / "data_flow.md", root / "data_flow.svg", args.html or "")


if __name__ == "__main__":
    main()
