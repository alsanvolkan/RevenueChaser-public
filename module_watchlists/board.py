"""The Watch lists board (#786): avo's named lists, their sections and the Price columns.

Read-only and rendered in memory on every load, as the LEAPS board is: nothing is written to ``.cache``, no
artifact is kept and no job fills it. The lists themselves live in ``boards/watchlists.duckdb``
(``module_watchlists/store.py``); the numbers come from the rcdata layer and from nothing else.

What a row's numbers are, by the symbol's ``coverage`` (``module_watchlists/symbols.py``):

- ``us_equity`` — the prices store's split-basis bars (``prices.bars``): last close, the change against the close
  before it, that session's volume and the average over the last ``VOLUME_SESSIONS`` bars with a volume. The market
  cap is the newest one of the class (``prices.latest``). The company name is the spine's (``identity.security``).
- ``market_series`` — the series' own newest two observations (``market.series``): a level and its change, with no
  volume and no cap. A yield carries ``%`` as its unit, so the column header's "USD" does not read over it.
- ``derived`` and ``unsupported`` — no number at all. The row stays, because the list is avo's and must come back
  out of an export whole; it says "No data source yet" and names issue #785.

The design is the mock avo approved on 12 Nov 2026 ("Boyle uygun"): one header line with the list picker, its
description and the list menu, the column tabs over fixed rows, a sortable header, an add bar with a ticker search
and a + Section button, drag handles on rows and section headings, and — on a phone — every column on the screen
at once, with the unit said once in the header instead of on every row.
"""
from __future__ import annotations

import html
from datetime import date, timedelta

from RevenueChaser.core.html_safe import ESC_JS, script_json
from RevenueChaser.core.reporting_common import THEME_CSS, THEME_JS, help_html
from RevenueChaser.module_watchlists import store, symbols
from RevenueChaser.rcdata import read

#: This board's ``X-RC-Panel`` value, the ``added_by`` its own writes are recorded under.
BOARD = "watchlists"
#: How many bars the average volume is over, the number ``rcdata.read.latest_volume`` uses for the screeners.
VOLUME_SESSIONS = 20
#: Calendar days of bars read to find ``VOLUME_SESSIONS`` sessions plus the previous close, with room for holidays.
BARS_WINDOW_DAYS = 70
#: The column tabs. Only ``price`` has its columns wired; the rest arrive with the indicators step (#786 step 4b).
TABS = (("price", "Price"), ("financials", "Financials"), ("performance", "Performance"), ("risk", "Risk"),
        ("technicals", "Technicals"))
#: The issue that is looking for a source for the symbols we cannot serve.
NO_SOURCE_ISSUE = "#785"


def _f(value) -> float | None:
    """A number or None; a NaN reads as None, because a bar's volume can be one."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out


def _equity_numbers(tickers: list[str]) -> dict[str, dict]:
    """``{ticker: {last, prev, vol, avol, cap}}`` from one prices-store read of the window plus the caps read.

    One ``ticker_bars`` call for every ticker of the list, not one per row: a list of 148 names would otherwise be
    148 queries. A ticker with no bar in the window is absent, which the row shows as no data.
    """
    if not tickers:
        return {}
    frame = read.ticker_bars(tickers, start=date.today() - timedelta(days=BARS_WINDOW_DAYS), basis="split")
    out: dict[str, dict] = {}
    if len(frame):
        for ticker, rows in frame.sort_values("session_date").groupby("ticker", sort=False):
            closes = [_f(v) for v in rows["close"].tolist()]
            volumes = [_f(v) for v in rows["volume"].tolist()]
            window = [v for v in volumes[-VOLUME_SESSIONS:] if v is not None]
            out[str(ticker)] = {
                "last": closes[-1] if closes else None,
                "prev": closes[-2] if len(closes) > 1 else None,
                "vol": volumes[-1] if volumes else None,
                "avol": (sum(window) / len(window)) if window else None,
                "session": rows["session_date"].iloc[-1].date().isoformat(),
            }
    for ticker, row in read.latest_market(tickers).items():
        out.setdefault(ticker, {}).setdefault("last", _f(row.get("price")))
        out[ticker]["cap"] = _f(row.get("market_cap"))
    return out


def _series_numbers(series_ids: list[str]) -> dict[str, dict]:
    """``{series_id: {last, prev}}`` from one ``market.series`` read: the newest two observations of each."""
    if not series_ids:
        return {}
    rows = read.market_series(series_ids, start=date.today() - timedelta(days=BARS_WINDOW_DAYS))
    by_id: dict[str, list[tuple]] = {}
    for r in rows:
        value = _f(r.get("value"))
        if value is not None:
            by_id.setdefault(str(r["series_id"]), []).append((r["obs_date"], value))
    out = {}
    for series_id, pairs in by_id.items():
        pairs.sort()
        out[series_id] = {"last": pairs[-1][1], "prev": pairs[-2][1] if len(pairs) > 1 else None}
    return out


def _row(item: dict, equities: dict[str, dict], series: dict[str, dict], names: dict[str, str]) -> dict:
    """One stored symbol as the page's row (short keys: a list can hold hundreds)."""
    parsed = symbols.parse(item["symbol"])
    row = {"sym": parsed.raw, "t": parsed.ticker, "ex": parsed.exchange, "cov": parsed.coverage,
           "nm": None, "last": None, "chg": None, "chgp": None, "vol": None, "avol": None, "cap": None,
           "unit": None, "note": item.get("note") or None}
    if parsed.coverage == "us_equity":
        found = equities.get(parsed.ticker) or {}
        row["nm"] = names.get(parsed.ticker)
        row["last"], row["vol"] = found.get("last"), found.get("vol")
        row["avol"], row["cap"] = found.get("avol"), found.get("cap")
        prev = found.get("prev")
    elif parsed.coverage == "market_series":
        found = series.get(parsed.series_id or "") or {}
        row["last"], prev = found.get("last"), found.get("prev")
        row["unit"] = "%" if str(parsed.series_id or "").startswith(("DGS", "T10Y")) else None
    else:
        prev = None
    if row["last"] is not None and prev not in (None, 0):
        row["chg"] = row["last"] - prev
        row["chgp"] = (row["last"] - prev) / abs(prev) * 100.0
    return row


def payload(list_id: str | None = None) -> dict | None:
    """Every list, and one of them whole with its Price numbers. None when no list has been made yet.

    ``list_id`` picks the list; an unknown one falls back to the first, so a stale link opens a board rather than
    an error. One prices read and one series read for the whole list, whatever its length.
    """
    lists = store.lists()
    if not lists:
        return None
    chosen = next((r for r in lists if r["list_id"] == list_id), lists[0])
    whole = store.read_list(chosen["list_id"])
    if whole is None:                                     # deleted between the two reads
        return None
    items = [i for section in whole["sections"] for i in section["items"]]
    parsed = [symbols.parse(i["symbol"]) for i in items]
    equities = _equity_numbers(sorted({p.ticker for p in parsed if p.coverage == "us_equity"}))
    series = _series_numbers(sorted({p.series_id for p in parsed if p.series_id}))
    names = {r["ticker"]: r.get("name") for r in read.identity(sorted({p.ticker for p in parsed
                                                                      if p.coverage == "us_equity"}))}
    sections = [{"id": s["section_id"], "name": s["name"],
                 "rows": [_row(i, equities, series, names) for i in s["items"]]}
                for s in whole["sections"]]
    sessions = sorted({v["session"] for v in equities.values() if v.get("session")})
    return {
        "lists": [{"id": r["list_id"], "name": r["name"], "symbols": r.get("symbols") or 0} for r in lists],
        "list": {"id": whole["list_id"], "name": whole["name"], "description": whole.get("description") or ""},
        "sections": sections,
        "counts": {"symbols": len(items), "sections": len(sections),
                   "unsupported": sum(1 for p in parsed if not p.is_supported)},
        "session": sessions[-1] if sessions else None,
        "tabs": [{"key": k, "label": label} for k, label in TABS],
        "issue": NO_SOURCE_ISSUE,
    }


#: How many suggestions the add box offers.
SEARCH_LIMIT = 8
#: The exchange prefixes a suggestion is written with, by the spine's own ``exchange`` value. A name whose
#: exchange is not one of these is still offered, with the spine's spelling, because avo's lists hold a few.
_EXCHANGE_SPELLING = {"NASDAQ": "NASDAQ", "NYSE": "NYSE", "NYSE MKT": "AMEX", "AMEX": "AMEX", "NYSE ARCA": "AMEX",
                      "ARCA": "AMEX", "CBOE": "CBOE", "BATS": "CBOE", "OTC": "OTC"}


def search(q: str, limit: int = SEARCH_LIMIT) -> list[dict]:
    """The add box's suggestions for what avo typed: the spine's names whose ticker or name matches.

    A ticker that starts with the query comes first, then one that holds it, then a name that holds it, so typing
    ``CRD`` offers ``CRDO`` before ``Cardiff``. One spine read (``identity.security``) and a filter in Python: the
    table is some thousands of rows, and a reader that filters in SQL would need a reader of its own in
    ``rcdata.read`` for the one box that asks.
    """
    text = (q or "").strip().upper()
    if not text:
        return []
    starts, holds, by_name = [], [], []
    for row in read.identity():
        ticker = str(row.get("ticker") or "").upper()
        if not ticker:
            continue
        name = str(row.get("name") or "")
        if ticker.startswith(text):
            bucket = starts
        elif text in ticker:
            bucket = holds
        elif text in name.upper():
            bucket = by_name
        else:
            continue
        exchange = _EXCHANGE_SPELLING.get(str(row.get("exchange") or "").upper().strip(),
                                          str(row.get("exchange") or "").upper().strip())
        bucket.append({"ticker": ticker, "name": name, "exchange": exchange,
                       "symbol": f"{exchange}:{ticker}" if exchange else ticker})
    for bucket in (starts, holds, by_name):
        bucket.sort(key=lambda r: r["ticker"])
    return (starts + holds + by_name)[:max(1, int(limit))]


HELP = help_html(
    "<p>Your own watch lists, the way you keep them in TradingView: a named list with a description, ordered "
    "sections, and one row per symbol. The column tabs (Price, Financials, Performance, Risk, Technicals) change "
    "which numbers the same rows show.</p>"
    "<p>The numbers come from what this repository already stores. A US listing shows its last stored close, the "
    "change against the close before it, that session's volume, the average volume of the last 20 sessions and "
    "its market cap. A market series (VIX, the Treasury yields) shows its level and change only. A symbol we have "
    "no source for — a foreign listing, a future, an FX pair — keeps its place in the list and says so; "
    f"issue {NO_SOURCE_ISSUE} is the hunt for a source.</p>"
    "<p><b>Fetch</b> does nothing here and the board has no Fetch button: a watch list is yours, not downloaded, "
    "and the numbers beside it are whatever the price and spine datasets last stored. <b>Refresh</b> reloads the "
    "page, which reads the lists and those datasets again — there is no cached copy of this board to drop.</p>"
    "<p>Drag a row by its handle to move it inside a section or into another one; drag a section heading to move "
    "the section. The search box adds a symbol to the section named beside it, <b>+ Section</b> adds a heading, "
    "and the ⋯ menu renames the list, edits its description, makes a new one or deletes it.</p>")

_CSS = """
.wl{max-width:1600px;margin:0 auto;padding:var(--sp-3) var(--sp-3) var(--sp-5)}
.wl :focus-visible{outline:2px solid var(--focus);outline-offset:1px}

/* header: the list picker, its description, Import / Export and the list menu */
.wl-head{display:flex;align-items:center;gap:var(--sp-3);flex-wrap:wrap}
.wl-pick{display:inline-flex;align-items:center;gap:var(--sp-2);background:none;border:0;cursor:pointer;
  color:var(--bright);font:600 var(--fs-xl)/1.15 inherit;padding:2px var(--sp-2);border-radius:var(--r-sm);
  margin-left:-6px}
.wl-pick:hover{background:var(--hover-faint)}
.wl-pick svg{width:15px;height:15px;opacity:.6}
.wl-desc{color:var(--muted);font-size:var(--fs-sm);margin:0;flex:1 1 220px;min-width:0;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.wl-tools{display:flex;gap:var(--sp-2);align-items:center;margin-left:auto}
.wl-ghost{min-height:28px;display:inline-flex;align-items:center;gap:var(--sp-2);padding:0 var(--sp-3);
  border:1px solid var(--input-border);background:var(--card);color:var(--fg);border-radius:var(--r-sm);
  font:var(--fs-sm)/1 inherit;cursor:pointer}
.wl-ghost:hover{background:var(--hover-faint)}
.wl-ghost svg{width:12px;height:12px;opacity:.7}
.wl-more{position:relative}
.wl-more>.wl-ghost{padding:0 var(--sp-2)}
.wl-menu{display:none;position:absolute;right:0;top:calc(100% + 4px);z-index:6;min-width:184px;
  background:var(--card);border:1px solid var(--border);border-radius:var(--r-sm);box-shadow:var(--elev-2);
  padding:4px}
.wl-more.open .wl-menu{display:block}
.wl-menu button{all:unset;cursor:pointer;display:flex;align-items:center;width:100%;box-sizing:border-box;
  min-height:30px;padding:0 var(--sp-2);border-radius:4px;font-size:var(--fs-sm)}
.wl-menu button:hover{background:var(--hover-faint)}
.wl-menu .sep{height:1px;background:var(--border);margin:4px 0}
.wl-menu .danger{color:var(--neg)}

/* the column tabs and the row density */
.wl-bar{display:flex;align-items:center;gap:var(--sp-3);margin:var(--sp-3) 0 var(--sp-2)}
.wl-tabs{display:flex;gap:2px;overflow-x:auto;scrollbar-width:none}
.wl-tabs::-webkit-scrollbar{display:none}
.wl-tab{min-height:28px;display:inline-flex;align-items:center;white-space:nowrap;padding:0 var(--sp-3);
  border:0;background:none;color:var(--muted);border-radius:var(--r-sm);font:500 var(--fs-sm)/1 inherit;
  cursor:pointer}
.wl-tab:hover{background:var(--hover-faint);color:var(--fg)}
.wl-tab[aria-selected="true"]{background:var(--card-2);color:var(--bright);font-weight:600}
.wl-dens{margin-left:auto;display:flex;align-items:center;gap:var(--sp-2);color:var(--muted);
  font-size:var(--fs-xs)}
.wl-seg{display:flex;border:1px solid var(--input-border);border-radius:var(--r-sm);overflow:hidden}
.wl-seg button{all:unset;cursor:pointer;min-height:26px;padding:0 var(--sp-2);font:500 var(--fs-xs)/1 inherit;
  color:var(--muted);display:inline-flex;align-items:center}
.wl-seg button[aria-pressed="true"]{background:var(--card-2);color:var(--bright)}

/* the panel and its add bar */
.wl-panel{background:var(--card);border:1px solid var(--border);border-radius:var(--r-lg);
  box-shadow:var(--elev-1);overflow:visible}
.wl-add{display:flex;align-items:center;gap:var(--sp-2);padding:var(--sp-2) var(--sp-3);
  border-bottom:1px solid var(--border);background:var(--card-2);
  border-radius:var(--r-lg) var(--r-lg) 0 0;position:relative}
.wl-search{position:relative;flex:0 1 320px;display:flex;align-items:center}
.wl-search>svg{position:absolute;left:var(--sp-2);width:13px;height:13px;color:var(--muted)}
.wl-search input{width:100%;min-height:28px;padding:0 var(--sp-2) 0 26px;border:1px solid var(--input-border);
  border-radius:var(--r-sm);background:var(--input-bg);color:var(--fg);font:var(--fs-sm) inherit}
.wl-sugg{display:none;position:absolute;top:calc(100% + 4px);left:0;width:100%;z-index:5;background:var(--card);
  border:1px solid var(--border);border-radius:var(--r-sm);box-shadow:var(--elev-2);padding:4px}
.wl-search.open .wl-sugg{display:block}
.wl-sugg button{all:unset;cursor:pointer;display:flex;align-items:center;gap:var(--sp-2);width:100%;
  box-sizing:border-box;min-height:30px;padding:0 var(--sp-2);border-radius:4px;font-size:var(--fs-sm);
  white-space:nowrap;overflow:hidden}
.wl-sugg button:hover,.wl-sugg button.hot{background:var(--accent-soft)}
.wl-sugg .ex{color:var(--muted);font-size:var(--fs-xs);margin-left:auto}
.wl-into{color:var(--muted);font-size:var(--fs-xs);white-space:nowrap}
.wl-into b{color:var(--fg);font-weight:600}
.wl-add .right{margin-left:auto;display:flex;gap:var(--sp-2);align-items:center}

/* the table */
.wl table{width:100%;border-collapse:collapse}
.wl th,.wl td{padding:3px var(--sp-3);text-align:right;font-size:var(--fs-base);white-space:nowrap;
  line-height:1.25}
.wl th{padding:0;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--card);z-index:1}
.wl th:first-child,.wl td:first-child{text-align:left;width:26%}
.wl tbody tr:hover td{background:var(--hover-faint)}
.wl td{border-bottom:1px solid var(--border)}
.wl tbody tr:last-child td{border-bottom:0}
.wl-rows-cozy td{padding-top:6px;padding-bottom:6px}
.wl-rows-wide td{padding-top:10px;padding-bottom:10px}
.wl th button{all:unset;cursor:pointer;box-sizing:border-box;display:flex;align-items:center;
  justify-content:flex-end;gap:3px;width:100%;min-height:30px;padding:0 var(--sp-3);
  font:500 var(--fs-sm)/1 inherit;color:var(--muted)}
.wl th:first-child button{justify-content:flex-start}
.wl th button:hover{background:var(--hover-faint);color:var(--fg)}
.wl th button svg{width:11px;height:11px;opacity:0;flex:0 0 auto}
.wl th button:hover svg{opacity:.45}
.wl th[aria-sort] button{color:var(--bright);font-weight:600;background:var(--accent-soft)}
.wl th[aria-sort] button svg{opacity:1;color:var(--accent)}
.wl th[aria-sort="ascending"] button svg{transform:rotate(180deg)}
.wl-rank{font-size:var(--fs-xs);color:var(--muted);font-weight:400;margin-left:2px}
.wl-hu{font-size:var(--fs-xs);color:var(--muted);font-weight:400;margin-left:2px}

/* section headings */
.wl-sec td{background:var(--card-2);padding:0}
.wl-sec .in{display:flex;align-items:center;gap:var(--sp-2);padding:0 var(--sp-3);min-height:24px;
  color:var(--muted);font:600 var(--fs-xs)/1 inherit;letter-spacing:.06em;text-transform:uppercase}
.wl-sec .in:hover{color:var(--fg)}
.wl-sec .cap{display:flex;align-items:center;gap:var(--sp-2);cursor:pointer;background:none;border:0;
  color:inherit;font:inherit;letter-spacing:inherit;text-transform:inherit;padding:0}
.wl-sec svg{width:10px;height:10px;opacity:.7;flex:0 0 auto}
.wl-sec.shut .cap svg{transform:rotate(-90deg)}
.wl-sec .n{font-weight:400;letter-spacing:0;text-transform:none;opacity:.75}
.wl-sec .secttools{margin-left:auto;display:flex;gap:6px;opacity:.6;text-transform:none;letter-spacing:0;
  font-weight:400}
.wl-sec .secttools button{all:unset;cursor:pointer;padding:0 2px;border-radius:3px}
.wl-sec .secttools button:hover{background:var(--hover-faint);color:var(--fg)}

/* the drag handle, the symbol cell and the row states */
.wl-grip{flex:0 0 auto;width:14px;height:18px;display:grid;place-items:center;cursor:grab;color:var(--muted);
  opacity:.3;background:none;border:0;padding:0}
.wl tr:hover .wl-grip{opacity:.6}
.wl-grip svg{width:10px;height:14px}
.wl-sym{display:flex;align-items:center;gap:var(--sp-2);min-width:0}
.wl-badge{flex:0 0 auto;min-width:48px;text-align:center;padding:1px 4px;border:1px solid var(--border);
  border-radius:4px;font:600 var(--fs-xs)/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--bright);
  background:var(--card-2)}
.wl-nm{color:var(--muted);font-size:var(--fs-sm);overflow:hidden;text-overflow:ellipsis}
.wl-u{font-size:var(--fs-xs);color:var(--muted);margin-left:2px}
.wl .pos{color:var(--pos)}
.wl .neg{color:var(--neg)}
.wl .dash{color:var(--muted)}
.wl-nosrc{font-size:var(--fs-xs);color:var(--muted);border:1px dashed var(--border);border-radius:4px;
  padding:0 var(--sp-2)}
.wl-un .wl-badge,.wl-un .wl-nm{opacity:.62}
.wl-un td[colspan]{text-align:left}
.wl-x{all:unset;cursor:pointer;color:var(--muted);opacity:0;padding:0 4px;border-radius:3px;font-size:var(--fs-sm)}
.wl tr:hover .wl-x{opacity:.7}
.wl-x:hover{background:var(--neg-soft);color:var(--neg);opacity:1}
.wl tr.dragging td{background:var(--accent-soft);opacity:.95}
.wl tr.dragging .wl-grip{opacity:1;color:var(--accent);cursor:grabbing}
.wl tr.dropline td{height:0;padding:0;border-bottom:2px solid var(--accent)}
.wl-empty{padding:var(--sp-5) var(--sp-3);text-align:center;color:var(--muted);font-size:var(--fs-sm)}
.wl-soon{padding:var(--sp-5) var(--sp-3);text-align:center;color:var(--muted);font-size:var(--fs-sm)}

/* Phone: every column on the screen at once (avo, 12 Nov 2026: "hepsini tam olarak sigdir panellerin, fontu
   kucultebilirsin, yandaki surukle birak telefona lazim degil"). The ticker alone carries the row, the handle is
   for the mouse, and the unit is said once in the header. */
@media (max-width:720px){
  .wl{padding:var(--sp-2) var(--sp-1) var(--sp-5)}
  .wl-pick{font-size:var(--fs-lg)}
  .wl-desc{white-space:normal;flex-basis:100%}
  .wl-tools{margin-left:0}
  .wl-ghost{min-height:var(--touch)}
  .wl-tab{min-height:36px}
  .wl-dens{display:none}
  .wl-add{flex-wrap:wrap;padding:var(--sp-2)}
  .wl-search{flex:1 1 100%}
  .wl-search input{min-height:var(--touch)}
  .wl-add .right{margin-left:0;width:100%}
  .wl-add .right .wl-ghost{flex:1 1 auto;justify-content:center}
  .wl-into{display:none}
  .wl table{width:100%;font-size:10.5px}
  .wl th button{min-height:var(--touch);padding:0 2px;font-size:9.5px;gap:1px}
  .wl th:first-child button{padding-left:var(--sp-2)}
  .wl th button svg{display:none}
  .wl-hu,.wl-rank{font-size:8.5px;margin-left:1px}
  .wl td{padding:4px 2px;font-size:10.5px}
  .wl td:first-child{padding-left:var(--sp-2)}
  .wl th:first-child,.wl td:first-child{width:1%;white-space:nowrap}
  .wl-grip,.wl-nm{display:none}
  .wl-badge{min-width:0;padding:1px 3px;font-size:9.5px}
  .wl-u{font-size:8.5px;margin-left:0}
  .wl-sec .in{padding:0 var(--sp-2);font-size:9px;min-height:22px}
  .wl-sec .secttools{display:none}
  .wl-nosrc{font-size:9px;padding:0 3px}
  .wl-x{opacity:.7;padding:0 2px}
}
"""

#: One caret, drawn wherever a control sorts or folds.
_CARET = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" aria-hidden="true">'
          '<path d="M6 9l6 6 6-6"/></svg>')
#: The six dots of a drag handle.
_GRIP = ('<svg viewBox="0 0 10 14" fill="currentColor" aria-hidden="true"><circle cx="2.5" cy="2" r="1.3"/>'
         '<circle cx="7.5" cy="2" r="1.3"/><circle cx="2.5" cy="7" r="1.3"/><circle cx="7.5" cy="7" r="1.3"/>'
         '<circle cx="2.5" cy="12" r="1.3"/><circle cx="7.5" cy="12" r="1.3"/></svg>')

#: The Price tab's columns: ``(key, label, unit)``. ``unit`` is the header's own "USD", said there instead of on
#: every row (avo, 12 Nov 2026: "USD yazmana gerek yok her satira yukari yazsan yeterli").
PRICE_COLUMNS = (("t", "Symbol", None), ("last", "Last", "USD"), ("chgp", "Chg%", None), ("chg", "Chg", "USD"),
                 ("vol", "Volume", None), ("avol", "Avg vol", None), ("cap", "Mkt cap", "USD"))


def _header_cells() -> str:
    cells = []
    for key, label, unit in PRICE_COLUMNS:
        chip = f'<span class="wl-hu">{html.escape(unit)}</span>' if unit else ""
        cells.append(f'<th data-col="{html.escape(key)}"><button type="button">{html.escape(label)}{chip}'
                     f'<span class="wl-rank"></span>{_CARET}</button></th>')
    return "".join(cells)


def render_html(data: dict) -> str:
    """The whole board for one list's ``payload``."""
    e = html.escape
    options = "".join(f'<button type="button" data-list="{e(r["id"])}">{e(r["name"])}'
                      f'<span class="ex">{r["symbols"]}</span></button>' for r in data["lists"])
    tabs = "".join(f'<button class="wl-tab" type="button" role="tab" data-tab="{e(t["key"])}" '
                   f'aria-selected="{"true" if t["key"] == "price" else "false"}">{e(t["label"])}</button>'
                   for t in data["tabs"])
    stamp = f'Prices as of {e(data["session"])}' if data["session"] else "No stored prices yet"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Watch lists</title><script>{THEME_JS}</script><style>{THEME_CSS}{_CSS}</style></head>
<body><main class="wl">
<div class="wl-head">
<span class="wl-more" id="wl-lists"><button class="wl-pick" type="button" aria-haspopup="true"
 aria-expanded="false"><span id="wl-name"></span>{_CARET}</button>
<span class="wl-menu wl-sugg-list">{options}</span></span>
<p class="wl-desc" id="wl-desc"></p>
<div class="wl-tools">
<span class="wl-desc" style="flex:0 0 auto">{stamp}</span>
<button class="wl-ghost" type="button" id="wl-export">Export</button>
<span class="wl-more" id="wl-menu"><button class="wl-ghost" type="button" aria-label="List menu"
 aria-haspopup="true" aria-expanded="false">&#8943;</button>
<span class="wl-menu"><button type="button" data-do="rename">Rename list</button>
<button type="button" data-do="describe">Edit description</button>
<button type="button" data-do="new">New list</button>
<span class="sep"></span><button type="button" class="danger" data-do="delete">Delete list</button></span></span>
</div></div>
<div class="wl-bar"><div class="wl-tabs" role="tablist">{tabs}</div>
<div class="wl-dens">Rows<span class="wl-seg" id="wl-dens">
<button type="button" data-dens="compact" aria-pressed="true">Compact</button>
<button type="button" data-dens="cozy" aria-pressed="false">Cozy</button>
<button type="button" data-dens="wide" aria-pressed="false">Wide</button></span></div></div>
<div class="wl-panel">
<div class="wl-add">
<span class="wl-search" id="wl-search">
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>
<input type="text" id="wl-q" autocomplete="off" aria-label="Add a symbol"
 placeholder="Add a symbol — type a ticker or a name">
<span class="wl-sugg" id="wl-sugg" role="listbox"></span></span>
<span class="wl-into">into <b id="wl-into"></b></span>
<span class="right"><button class="wl-ghost" type="button" id="wl-newsec">
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true">
<path d="M12 5v14M5 12h14"/></svg>Section</button></span></div>
<table><thead><tr>{_header_cells()}</tr></thead><tbody id="wl-body"></tbody></table>
<div class="wl-soon" id="wl-soon" hidden>These columns arrive with the indicators step — issue #786.</div>
</div>
{HELP}
</main>
<script>
var WL_DATA = {script_json(data)};
{_JS}</script></body></html>"""


_JS = r"""
(function(){
  var data = WL_DATA, body = document.getElementById('wl-body');
  var sort = [];                           // [{col, dir}], newest first; at most two columns
  var shut = {};                           // section id -> folded
  var into = data.sections.length ? data.sections[0].id : null;

  function num(v, digits){
    if(v === null || v === undefined) return null;
    return v.toLocaleString('en-US', {minimumFractionDigits: digits, maximumFractionDigits: digits});
  }
  function big(v){
    if(v === null || v === undefined) return null;
    var a = Math.abs(v), unit = '';
    if(a >= 1e12){ v /= 1e12; unit = 'T'; } else if(a >= 1e9){ v /= 1e9; unit = 'B'; }
    else if(a >= 1e6){ v /= 1e6; unit = 'M'; } else if(a >= 1e3){ v /= 1e3; unit = 'K'; }
    return num(v, 2) + (unit ? '<span class="wl-u">' + unit + '</span>' : '');
  }
  function signed(v, digits, suffix){
    if(v === null || v === undefined) return '<span class="dash">—</span>';
    var cls = v > 0 ? 'pos' : (v < 0 ? 'neg' : '');
    var text = (v < 0 ? '−' : '') + num(Math.abs(v), digits) + (suffix || '');
    return '<span class="' + cls + '">' + text + '</span>';
  }
  function cell(row, col){
    if(col === 'last'){
      if(row.last === null) return '<span class="dash">—</span>';
      return num(row.last, 2) + (row.unit ? '<span class="wl-u">' + row.unit + '</span>' : '');
    }
    if(col === 'chgp') return signed(row.chgp, 2, '%');
    if(col === 'chg') return signed(row.chg, 2, '');
    if(col === 'vol' || col === 'avol' || col === 'cap'){
      var v = big(row[col]);
      return v === null ? '<span class="dash">—</span>' : v;
    }
    return '';
  }
  function compare(a, b, col){
    var x = col === 't' ? a.t : a[col], y = col === 't' ? b.t : b[col];
    if(x === null || x === undefined) return 1;          // no number sorts last, whichever way
    if(y === null || y === undefined) return -1;
    if(typeof x === 'string') return x.localeCompare(y);
    return x - y;
  }
  function sorted(rows){
    if(!sort.length) return rows;
    var out = rows.slice();
    out.sort(function(a, b){
      for(var i = 0; i < sort.length; i++){
        var r = compare(a, b, sort[i].col) * (sort[i].dir === 'asc' ? 1 : -1);
        if(r) return r;
      }
      return 0;
    });
    return out;
  }
  function grip(kind, id){
    return '<button class="wl-grip" type="button" draggable="true" data-drag="' + kind + '" data-id="' +
      rcEsc(id) + '" aria-label="Reorder">' + GRIP + '</button>';
  }
  function rowHtml(row, sectionId){
    var cells = '', un = row.cov !== 'us_equity' && row.cov !== 'market_series';
    if(un){
      cells = '<td colspan="5"><span class="wl-nosrc">No data source yet — ' + rcEsc(data.issue) +
        '</span></td><td class="dash">—</td>';
    } else {
      for(var i = 1; i < COLS.length; i++) cells += '<td>' + cell(row, COLS[i]) + '</td>';
    }
    return '<tr class="wl-row' + (un ? ' wl-un' : '') + '" data-sym="' + rcEsc(row.sym) + '" data-sec="' +
      rcEsc(sectionId) + '"><td><div class="wl-sym">' + grip('row', row.sym) +
      '<span class="wl-badge" title="' + rcEsc(row.nm || row.sym) + '">' + rcEsc(row.t) + '</span>' +
      '<span class="wl-nm">' + rcEsc(row.nm || row.sym) + '</span>' +
      '<button class="wl-x" type="button" data-remove="' + rcEsc(row.sym) + '" aria-label="Remove ' +
      rcEsc(row.t) + '">×</button></div></td>' + cells + '</tr>';
  }
  function sectionHtml(section){
    var folded = !!shut[section.id], n = section.rows.length;
    var none = section.rows.filter(function(r){ return r.cov !== 'us_equity' && r.cov !== 'market_series'; }).length;
    var count = none === n && n ? n + ' · no source' : String(n);
    return '<tr class="wl-sec' + (folded ? ' shut' : '') + '" data-sec="' + rcEsc(section.id) +
      '"><td colspan="' + COLS.length + '"><div class="in">' + grip('sec', section.id) +
      '<button class="cap" type="button" data-fold="' + rcEsc(section.id) + '" aria-expanded="' +
      (folded ? 'false' : 'true') + '">' + CARET + rcEsc(section.name) +
      ' <span class="n">' + rcEsc(count) + '</span></button>' +
      '<span class="secttools"><button type="button" data-sec-rename="' + rcEsc(section.id) + '">rename</button>' +
      '<span>·</span><button type="button" data-sec-delete="' + rcEsc(section.id) +
      '">delete</button></span></div></td></tr>';
  }
  function draw(){
    document.getElementById('wl-name').textContent = data.list.name;
    var counts = data.counts, desc = data.list.description;
    var sub = counts.symbols + ' symbols · ' + counts.sections + ' sections';
    if(counts.unsupported) sub += ' · ' + counts.unsupported + ' with no source yet';
    document.getElementById('wl-desc').textContent = desc ? desc + ' · ' + sub : sub;
    var current = data.sections.filter(function(s){ return s.id === into; })[0] || data.sections[0];
    into = current ? current.id : null;
    document.getElementById('wl-into').textContent = (current && current.name) ? current.name : 'this list';
    var html = '';
    data.sections.forEach(function(section){
      // A list avo gave no headings has one section whose name is empty: its rows are drawn, its heading is not.
      if(section.name) html += sectionHtml(section);
      if(!shut[section.id]) sorted(section.rows).forEach(function(row){ html += rowHtml(row, section.id); });
    });
    body.innerHTML = html || '<tr><td colspan="' + COLS.length +
      '"><div class="wl-empty">This list is empty. Type a ticker above to add one.</div></td></tr>';
    document.querySelectorAll('.wl thead th').forEach(function(th, i){
      var at = -1;
      for(var k = 0; k < sort.length; k++) if(sort[k].col === COLS[i]) at = k;
      if(at < 0){ th.removeAttribute('aria-sort'); }
      else { th.setAttribute('aria-sort', sort[at].dir === 'asc' ? 'ascending' : 'descending'); }
      var rank = th.querySelector('.wl-rank');
      if(rank) rank.textContent = (at >= 0 && sort.length > 1) ? String(at + 1) : '';
    });
  }

  // ── sorting: click a header, click again to turn it round; shift-click adds a second column ──
  document.querySelectorAll('.wl thead th button').forEach(function(button, i){
    button.addEventListener('click', function(ev){
      var col = COLS[i], found = sort.filter(function(s){ return s.col === col; })[0];
      if(found && (ev.shiftKey || sort.length === 1)){ found.dir = found.dir === 'asc' ? 'desc' : 'asc'; }
      else if(!ev.shiftKey){ sort = [{col: col, dir: col === 't' ? 'asc' : 'desc'}]; }
      else if(!found){ sort.push({col: col, dir: col === 't' ? 'asc' : 'desc'}); sort = sort.slice(-2); }
      draw();
    });
  });

  // ── the two menus and the density segment ──
  function menu(id){
    var wrap = document.getElementById(id), button = wrap.querySelector('button');
    button.addEventListener('click', function(ev){
      ev.stopPropagation();
      var open = wrap.classList.toggle('open');
      button.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    return wrap;
  }
  var listMenu = menu('wl-lists'), moreMenu = menu('wl-menu');
  document.addEventListener('click', function(){
    [listMenu, moreMenu].forEach(function(w){
      w.classList.remove('open');
      w.querySelector('button').setAttribute('aria-expanded', 'false');
    });
    document.getElementById('wl-search').classList.remove('open');
  });
  listMenu.querySelectorAll('[data-list]').forEach(function(button){
    button.addEventListener('click', function(){
      location.search = '?list=' + encodeURIComponent(button.getAttribute('data-list'));
    });
  });
  document.getElementById('wl-dens').addEventListener('click', function(ev){
    var button = ev.target.closest('[data-dens]');
    if(!button) return;
    this.querySelectorAll('button').forEach(function(b){ b.setAttribute('aria-pressed', String(b === button)); });
    var table = document.querySelector('.wl table');
    table.classList.remove('wl-rows-cozy', 'wl-rows-wide');
    var dens = button.getAttribute('data-dens');
    if(dens !== 'compact') table.classList.add('wl-rows-' + dens);
  });
  document.querySelectorAll('.wl-tab').forEach(function(tab){
    tab.addEventListener('click', function(){
      document.querySelectorAll('.wl-tab').forEach(function(t){
        t.setAttribute('aria-selected', String(t === tab));
      });
      var price = tab.getAttribute('data-tab') === 'price';
      document.querySelector('.wl table').hidden = !price;
      document.getElementById('wl-soon').hidden = price;
    });
  });

  // ── the writes: every one goes to the store and then redraws from the answer ──
  function post(path, payload){
    return fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-RC-Panel': 'watchlists'},
                        body: JSON.stringify(payload || {})})
      .then(function(r){ return r.json().catch(function(){ return {ok: false, error: 'bad answer'}; }); })
      .then(function(answer){
        if(!answer.ok){ alert(answer.error || 'That did not work.'); return null; }
        if(answer.payload){ data = answer.payload; draw(); }
        return answer;
      }, function(){ alert('The dashboard did not answer.'); return null; });
  }
  var LIST = '/api/watchlists/' + encodeURIComponent(data.list.id);

  body.addEventListener('click', function(ev){
    var fold = ev.target.closest('[data-fold]');
    if(fold){ var id = fold.getAttribute('data-fold'); shut[id] = !shut[id]; draw(); return; }
    var remove = ev.target.closest('[data-remove]');
    if(remove){
      var sym = remove.getAttribute('data-remove');
      if(confirm('Take ' + sym + ' off this list?')) post(LIST + '/symbol/remove', {symbol: sym});
      return;
    }
    var rename = ev.target.closest('[data-sec-rename]');
    if(rename){
      var was = rename.closest('tr').querySelector('.cap').textContent.trim();
      var name = prompt('Section name', was.replace(/\s+\d+( · no source)?$/, ''));
      if(name) post(LIST + '/section/rename', {section_id: rename.getAttribute('data-sec-rename'), name: name});
      return;
    }
    var drop = ev.target.closest('[data-sec-delete]');
    if(drop && confirm('Delete this section? Its symbols move to the first section.')){
      post(LIST + '/section/delete', {section_id: drop.getAttribute('data-sec-delete')});
    }
  });
  document.getElementById('wl-newsec').addEventListener('click', function(){
    var name = prompt('New section name');
    if(name) post(LIST + '/section/add', {name: name});
  });
  moreMenu.querySelectorAll('[data-do]').forEach(function(button){
    button.addEventListener('click', function(){
      var what = button.getAttribute('data-do');
      if(what === 'rename'){
        var name = prompt('List name', data.list.name);
        if(name) post(LIST, {name: name});
      } else if(what === 'describe'){
        var desc = prompt('Description', data.list.description);
        if(desc !== null) post(LIST, {description: desc});
      } else if(what === 'new'){
        var made = prompt('New list name');
        if(made) post('/api/watchlists/new', {name: made}).then(function(answer){
          if(answer && answer.list_id) location.search = '?list=' + encodeURIComponent(answer.list_id);
        });
      } else if(what === 'delete'){
        if(confirm('Delete "' + data.list.name + '" and its ' + data.counts.symbols + ' symbols?')){
          post(LIST + '/delete', {}).then(function(answer){ if(answer) location.search = ''; });
        }
      }
    });
  });
  document.getElementById('wl-export').addEventListener('click', function(){
    location.href = LIST + '/export.txt';
  });

  // ── the ticker search ──
  var box = document.getElementById('wl-q'), sugg = document.getElementById('wl-sugg'),
      search = document.getElementById('wl-search'), timer = null, found = [];
  function showSuggestions(){
    if(!found.length){ search.classList.remove('open'); return; }
    sugg.innerHTML = found.map(function(r, i){
      return '<button type="button" class="' + (i ? '' : 'hot') + '" data-add="' + rcEsc(r.symbol) + '">' +
        '<span class="wl-badge">' + rcEsc(r.ticker) + '</span><span class="wl-nm">' + rcEsc(r.name || '') +
        '</span><span class="ex">' + rcEsc(r.exchange || '') + '</span></button>';
    }).join('');
    search.classList.add('open');
  }
  box.addEventListener('input', function(){
    var q = box.value.trim();
    if(timer) clearTimeout(timer);
    if(q.length < 1){ found = []; search.classList.remove('open'); return; }
    timer = setTimeout(function(){
      fetch('/api/watchlists/search?q=' + encodeURIComponent(q))
        .then(function(r){ return r.json(); })
        .then(function(answer){ found = (answer && answer.rows) || []; showSuggestions(); }, function(){});
    }, 180);
  });
  box.addEventListener('keydown', function(ev){
    if(ev.key === 'Enter'){
      var pick = found.length ? found[0].symbol : box.value.trim().toUpperCase();
      if(pick) add(pick);
    } else if(ev.key === 'Escape'){ search.classList.remove('open'); }
  });
  sugg.addEventListener('click', function(ev){
    var button = ev.target.closest('[data-add]');
    if(button) add(button.getAttribute('data-add'));
  });
  function add(symbol){
    box.value = '';
    found = [];
    search.classList.remove('open');
    post(LIST + '/symbol/add', {symbol: symbol, section_id: into});
  }

  // ── drag and drop: a row moves inside its section or into another one, a heading moves its section ──
  var lifted = null, line = null;
  function clearLine(){ if(line){ line.remove(); line = null; } }
  body.addEventListener('dragstart', function(ev){
    var grip = ev.target.closest('[data-drag]');
    if(!grip){ return; }
    lifted = {kind: grip.getAttribute('data-drag'), id: grip.getAttribute('data-id')};
    grip.closest('tr').classList.add('dragging');
    ev.dataTransfer.effectAllowed = 'move';
    ev.dataTransfer.setData('text/plain', lifted.id);
  });
  body.addEventListener('dragover', function(ev){
    if(!lifted) return;
    ev.preventDefault();
    var over = ev.target.closest('tr');
    if(!over || over.classList.contains('dropline')) return;
    clearLine();
    line = document.createElement('tr');
    line.className = 'dropline';
    line.innerHTML = '<td colspan="' + COLS.length + '"></td>';
    var below = (ev.clientY - over.getBoundingClientRect().top) > over.offsetHeight / 2;
    over.parentNode.insertBefore(line, below ? over.nextSibling : over);
  });
  body.addEventListener('dragend', function(){
    clearLine();
    document.querySelectorAll('tr.dragging').forEach(function(tr){ tr.classList.remove('dragging'); });
    lifted = null;
  });
  function sectionAbove(tr){
    // Which section the drop line sits in: the nearest heading above it. A line above the first heading
    // belongs to the first section, because a symbol always lives under one.
    for(var node = tr; node; node = node.previousElementSibling){
      if(node.classList && node.classList.contains('wl-sec')) return node.getAttribute('data-sec');
    }
    return data.sections.length ? data.sections[0].id : null;
  }
  body.addEventListener('drop', function(ev){
    if(!lifted || !line) return;
    ev.preventDefault();
    // Read the line's place in the table BEFORE taking it out: a detached row has no siblings.
    var before = line.nextElementSibling, section = sectionAbove(line);
    clearLine();
    if(lifted.kind === 'sec'){
      // The headings in the order they are drawn, the dragged one taken out and put back where the line is.
      var ids = [];
      body.querySelectorAll('tr.wl-sec').forEach(function(tr){ ids.push(tr.getAttribute('data-sec')); });
      var nextHeading = null;
      for(var node = before; node; node = node.nextElementSibling){
        if(node.classList && node.classList.contains('wl-sec')){ nextHeading = node.getAttribute('data-sec'); break; }
      }
      ids = ids.filter(function(id){ return id !== lifted.id; });
      var where = nextHeading ? ids.indexOf(nextHeading) : ids.length;
      ids.splice(where < 0 ? ids.length : where, 0, lifted.id);
      post(LIST + '/sections/order', {order: ids});
      return;
    }
    var order = [];
    body.querySelectorAll('tr.wl-row').forEach(function(tr){
      if(tr.getAttribute('data-sec') === section) order.push(tr.getAttribute('data-sym'));
    });
    order = order.filter(function(sym){ return sym !== lifted.id; });
    var mark = before && before.classList.contains('wl-row') &&
      before.getAttribute('data-sec') === section ? before.getAttribute('data-sym') : null;
    var place = mark ? order.indexOf(mark) : order.length;
    order.splice(place < 0 ? order.length : place, 0, lifted.id);
    post(LIST + '/symbol/move', {symbol: lifted.id, section_id: section, order: order});
  });

  draw();
})();
"""
_JS = (f"var COLS = {script_json([c[0] for c in PRICE_COLUMNS])};\n"
       f"var CARET = {script_json(_CARET)};\nvar GRIP = {script_json(_GRIP)};\n" + ESC_JS + _JS)
