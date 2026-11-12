"""The Watch lists board (#786): avo's named lists, their sections and their five column tabs.

Read-only and rendered in memory on every load, as the LEAPS board is: nothing is written to ``.cache``, no
artifact is kept and no job fills it. The lists themselves live in ``boards/watchlists.duckdb``
(``module_watchlists/store.py``); the numbers come from the rcdata layer and from nothing else.

What a row's numbers are, by the symbol's ``coverage`` (``module_watchlists/symbols.py``):

- ``us_equity`` — the prices store's split-basis bars (``prices.bars``): last close, the change against the close
  before it, that session's volume and the average over the last ``VOLUME_SESSIONS`` bars with a volume. The market
  cap is the newest one of the class (``prices.latest``). The company name is the spine's (``identity.security``).
- ``market_series`` — the series' own newest two observations (``market.series``): a level and its change, with no
  volume and no cap. A yield carries ``%`` as its unit, so the column header's "$" does not read over it.
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

from RevenueChaser.core import fundamentals_layer
from RevenueChaser.core.html_safe import ESC_JS, script_json
from RevenueChaser.core.reporting_common import THEME_CSS, THEME_JS, help_html
from RevenueChaser.module_watchlists import financials, indicators, store, symbols
from RevenueChaser.rcdata import read

#: This board's ``X-RC-Panel`` value, the ``added_by`` its own writes are recorded under.
BOARD = "watchlists"
#: How many bars the average volume is over, the number ``rcdata.read.latest_volume`` uses for the screeners.
VOLUME_SESSIONS = 20
#: Calendar days of bars read to find ``VOLUME_SESSIONS`` sessions plus the previous close, with room for holidays.
BARS_WINDOW_DAYS = 70
#: The column tabs. Price rides the first payload; the other four are asked for when first opened.
TABS = (("price", "Price"), ("financials", "Financials"), ("performance", "Performance"), ("risk", "Risk"),
        ("technicals", "Technicals"))
#: Sessions of history the derived tabs need at most (the 5-year window plus the bar it is measured from).
HISTORY_SESSIONS = max(indicators.WINDOWS.values()) + 1
#: Calendar days that covers, at five sessions a week, with room for holidays.
HISTORY_DAYS = HISTORY_SESSIONS * 7 // 5 + 60
#: The Financials tab's beta is the one-year one, so it reads a year of bars and not five.
FINANCIALS_DAYS = (indicators.WINDOWS["1Y"] + 1) * 7 // 5 + 60
#: The index the beta columns are against, as TradingView's are against the S&P 500.
MARKET_TICKER = "SPY"
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
        "tab": FIRST_TAB,
        "issue": NO_SOURCE_ISSUE,
    }


# ── the derived tabs (#786 step 4b) ────────────────────────────────────────────────────────────────────────────
# Performance, Risk and Technicals are all read off the same bar history, so they share one read and are worked
# out by ``module_watchlists/indicators.py`` — the module that owns TradingView's method. They are NOT in the
# first payload: the history they need is five years deep, and a board that opened on Price would pay for it
# without showing it. The browser asks for a tab's numbers the first time that tab is opened.

def _history(tickers: list[str], days: int = HISTORY_DAYS) -> dict[str, dict]:
    """``{ticker: {"bars": [Bar, ...], "sessions": [date, ...]}}`` oldest first, from one prices-store read."""
    if not tickers:
        return {}
    frame = read.ticker_bars(tickers, start=date.today() - timedelta(days=days), basis="split")
    out: dict[str, dict] = {}
    if not len(frame):
        return out
    for ticker, rows in frame.sort_values("session_date").groupby("ticker", sort=False):
        bars, sessions = [], []
        for day, close, high, low, volume in zip(rows["session_date"], rows["close"], rows["high"], rows["low"],
                                                 rows["volume"]):
            value = _f(close)
            if value is None:
                continue
            bars.append(indicators.Bar(close=value, high=_f(high), low=_f(low), volume=_f(volume)))
            sessions.append(day.date())
        out[str(ticker)] = {"bars": bars, "sessions": sessions}
    return out


def _aligned(mine: dict, market: dict) -> tuple[list, list]:
    """The two series over the sessions they share, in order — what :func:`indicators.beta` asks its caller for.

    A name that listed last year has fewer sessions than the index; pairing the two by position would measure its
    returns against the index's returns of other days, which is not a beta at all.
    """
    if not mine or not market:
        return [], []
    theirs = dict(zip(market["sessions"], market["bars"]))
    pairs = [(bar, theirs[day]) for day, bar in zip(mine["sessions"], mine["bars"]) if day in theirs]
    return [a for a, _ in pairs], [b for _, b in pairs]


def _performance(bars, sessions_this_year: int) -> dict:
    out = {f"p{name}": indicators.performance(bars, n) for name, n in indicators.WINDOWS.items()}
    out["pYTD"] = indicators.performance_ytd(bars, sessions_this_year)
    return out


def _risk(found: dict, market: dict) -> dict:
    bars = found["bars"]
    mine, theirs = _aligned(found, market)
    return {"v1D": indicators.volatility(bars, 1), "v1W": indicators.volatility(bars, indicators.WINDOWS["1W"]),
            "v1M": indicators.volatility(bars, indicators.WINDOWS["1M"]),
            "b1Y": indicators.beta(mine, theirs, indicators.WINDOWS["1Y"]),
            "b5Y": indicators.beta(mine, theirs, indicators.WINDOWS["5Y"])}


def _technicals(bars) -> dict:
    out = dict(indicators.ratings(bars))
    out["rsi"] = indicators.rsi([b.close for b in bars])
    out["mom"] = indicators.momentum([b.close for b in bars])
    return out


def _financials(parsed: list, wanted: list[str]) -> dict[str, dict]:
    """``{symbol: Financials values}`` for the US listings of a list (``module_watchlists/financials.py``).

    Four reads for the whole list, whatever its length: the statements (two, inside ``records_many``), the newest
    closes, a year of bars for the beta, and the dividends of the last year.
    """
    statements = fundamentals_layer.records_many(wanted)
    prices = read.latest_market(wanted)
    history = _history(sorted(set(wanted) | {MARKET_TICKER}), days=FINANCIALS_DAYS)
    market = history.get(MARKET_TICKER) or {}
    ids = read.security_ids(wanted)
    today = date.today()
    actions_of: dict[str, list[dict]] = {}
    for a in read.actions(ids.values(), start=today - timedelta(days=financials.DIVIDEND_DAYS)):
        actions_of.setdefault(str(a["security_id"]), []).append(a)
    values: dict[str, dict] = {}
    for symbol in parsed:
        if symbol.coverage != "us_equity":
            continue
        annual, quarterly, _meta = statements.get(symbol.ticker, ([], [], {}))
        found = financials.statements(annual, quarterly)
        price = _f((prices.get(symbol.ticker) or {}).get("price"))
        dividend = financials.dividends_per_share(actions_of.get(ids.get(symbol.ticker, ""), []), today)
        bars = history.get(symbol.ticker)
        beta = None
        if bars and bars["bars"]:
            mine, theirs = _aligned(bars, market)
            beta = indicators.beta(mine, theirs, indicators.WINDOWS["1Y"])
        row = financials.row(found, price, dividend, beta)
        if any(v is not None for k, v in row.items() if k != "fb"):
            values[symbol.raw] = row
    return values


def columns(list_id: str, tab: str) -> dict | None:
    """One tab's numbers for a list: ``{tab, values: {symbol: {...}}}``. None for an unknown list or tab.

    Only the US listings get numbers. A market series has two observations a day at best, which is not a bar
    series, and a symbol with no source has nothing at all; both are left out and show "—" as on the Price tab.
    Beta is against ``MARKET_TICKER``, aligned by taking both series over the same window.
    """
    if tab not in DERIVED_TABS or store.get_list(list_id) is None:
        return None
    whole = store.read_list(list_id)
    parsed = [symbols.parse(i["symbol"]) for s in whole["sections"] for i in s["items"]]
    wanted = sorted({p.ticker for p in parsed if p.coverage == "us_equity"})
    if tab == "financials":
        return {"tab": tab, "values": _financials(parsed, wanted)}
    history = _history(sorted(set(wanted) | {MARKET_TICKER}))
    market = history.get(MARKET_TICKER) or {}
    this_year = date.today().year
    values: dict[str, dict] = {}
    for symbol in parsed:
        found = history.get(symbol.ticker) if symbol.coverage == "us_equity" else None
        if not found or not found["bars"]:
            continue
        if tab == "performance":
            values[symbol.raw] = _performance(found["bars"],
                                              sum(1 for d in found["sessions"] if d.year == this_year))
        elif tab == "risk":
            values[symbol.raw] = _risk(found, market)
        else:
            values[symbol.raw] = _technicals(found["bars"])
    return {"tab": tab, "values": values}


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
    "<p><b>Performance</b>, <b>Risk</b> and <b>Technicals</b> are worked out here from the stored daily bars, by "
    "TradingView's published method: fifteen moving averages and eleven oscillators vote, and the rating is the "
    "mean of their votes. <b>They will not match TradingView digit for digit</b> — the bars are ours and the "
    "session is ours — and a name whose history is too short for a window shows \u2014 rather than a number "
    "made up from a shorter one. Beta is against SPY, over the sessions the two actually share. These three tabs "
    "read five years of bars, so they are fetched the first time you open them, not on every load.</p>"
    "<p><b>Financials</b> reads the SEC statements the fundamentals layer holds: revenue and EPS over the trailing "
    "twelve months where a 10-Q follows the fiscal year, the fiscal year otherwise (hover a number to see which). "
    "P/E is the last close over that EPS, and a loss shows \u2014. Dividend is the cash paid per share over the "
    "last year, specials left out, and Yield is that over the last close. Beta is the one-year one against SPY.</p>"
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
/* The cell grid (avo, 13 Nov 2026, vault-memory rules/ui-tasarim.md "Cell grid"): every cell of a column is as wide
   as the column's widest value (the table does that), and a cell's text sits where the cell stands: the symbol,
   leftmost, to the left; the numbers between, centred; the ×, rightmost, to the right. */
.wl th,.wl td{padding:3px var(--sp-3);text-align:center;font-size:var(--fs-base);white-space:nowrap;
  line-height:1.25}
.wl th{padding:0;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--card);z-index:1}
.wl th:first-child,.wl td:first-child{text-align:left;width:26%}
.wl tbody tr:hover td{background:var(--hover-faint)}
.wl td{border-bottom:1px solid var(--border)}
.wl tbody tr:last-child td{border-bottom:0}
.wl-rows-cozy td{padding-top:6px;padding-bottom:6px}
.wl-rows-wide td{padding-top:10px;padding-bottom:10px}
.wl th button{all:unset;cursor:pointer;box-sizing:border-box;display:flex;align-items:center;
  justify-content:center;gap:3px;width:100%;min-height:30px;padding:0 var(--sp-3);
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
.wl-xc{width:1%;text-align:right;white-space:nowrap;padding-left:var(--sp-2);padding-right:var(--sp-2)}
.wl th.wl-xc{padding:0;position:relative}
.wl-x{all:unset;cursor:pointer;color:var(--muted);opacity:0;padding:0 4px;border-radius:3px;font-size:var(--fs-sm)}
.wl tr:hover .wl-x{opacity:.7}
.wl-x:hover{background:var(--neg-soft);color:var(--neg);opacity:1}
/* The header's hidden "Remove" is pinned inside its own cell: left to its static place it sat one pixel past
   the table's right edge, and on a 390 px phone that was a sideways scroll (live, 12 Nov 2026). */
.wl .sr{position:absolute;left:0;top:0;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
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
  .wl-grip{display:none}
  /* avo, 12 Nov 2026: "Sorun degil company name gozuksun" — the name comes back as a small second line under
     the ticker, capped so a long name is cut rather than widening the column the numbers need. */
  .wl-sym{flex-direction:column;align-items:flex-start;gap:1px}
  .wl-nm{display:block;max-width:70px;font-size:8.5px;line-height:1.15;white-space:nowrap}
  /* A long symbol (FRED:BAMLH0A0HYM2, TVC:US10Y-TVC:US02Y) is cut like the name, at the name's width: on the
     index list its full width pushed the table 5 px past a 390 px phone (live, 12 Nov 2026). The full symbol
     stays in the badge's title. */
  .wl-badge{min-width:0;max-width:70px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:1px 3px;
    font-size:9.5px}
  .wl-u{font-size:8.5px;margin-left:0}
  .wl-sec .in{padding:0 var(--sp-2);font-size:9px;min-height:22px}
  .wl-sec .secttools{display:none}
  .wl-nosrc{font-size:9px;padding:0 3px}
  /* No hover on a phone, so the × stays visible. avo, 12 Nov 2026: "X icin dokunma alani X harfinden cok az
     daha buyuk olsun, geri kalan kisimlari uygun yerlestir" — the cell is the glyph and a hair around it, and
     every pixel it does not take goes to the number columns, which are the ones he reads. */
  .wl td.wl-xc{padding:0 2px}
  .wl td.wl-xc .wl-x{opacity:.7;padding:0 2px;font-size:11px}
}
"""

#: One caret, drawn wherever a control sorts or folds.
_CARET = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" aria-hidden="true">'
          '<path d="M6 9l6 6 6-6"/></svg>')
#: The six dots of a drag handle.
_GRIP = ('<svg viewBox="0 0 10 14" fill="currentColor" aria-hidden="true"><circle cx="2.5" cy="2" r="1.3"/>'
         '<circle cx="7.5" cy="2" r="1.3"/><circle cx="2.5" cy="7" r="1.3"/><circle cx="7.5" cy="7" r="1.3"/>'
         '<circle cx="2.5" cy="12" r="1.3"/><circle cx="7.5" cy="12" r="1.3"/></svg>')

#: Each tab's columns: ``(key, label, unit, kind)``. ``unit`` is the header's own "$" or "%", said there
#: instead of on every row (avo, 12 Nov 2026: "USD yazmana gerek yok her satira yukari yazsan yeterli"), and as
#: the currency's symbol, not its code (avo, 13 Nov 2026: "Dolar için USD kullanmana gerek yok ... simge kullanırsan
#: daha az yer kaplarlar"; vault-memory rules/ui-tasarim.md).
#: ``kind`` is how the browser draws the value: ``sym`` the symbol cell, ``num`` a plain number, ``signed`` a
#: number that takes a colour from its sign, ``pct`` a signed percent, ``big`` a T/B/M/K abbreviation,
#: ``rating`` a word with its score in the title.
COLUMNS = {
    "price": (("t", "Symbol", None, "sym"), ("last", "Last", "$", "num"), ("chgp", "Chg%", None, "pct"),
              ("chg", "Chg", "$", "signed"), ("vol", "Volume", None, "big"), ("avol", "Avg vol", None, "big"),
              ("cap", "Mkt cap", "$", "big")),
    "performance": (("t", "Symbol", None, "sym"), ("p1W", "1W", "%", "pct"), ("p1M", "1M", "%", "pct"),
                    ("p3M", "3M", "%", "pct"), ("pYTD", "YTD", "%", "pct"), ("p1Y", "1Y", "%", "pct"),
                    ("p5Y", "5Y", "%", "pct")),
    "risk": (("t", "Symbol", None, "sym"), ("v1D", "Vol 1D", "%", "num"), ("v1W", "Vol 1W", "%", "num"),
             ("v1M", "Vol 1M", "%", "num"), ("b1Y", "Beta 1Y", None, "num"), ("b5Y", "Beta 5Y", None, "num")),
    "financials": (("t", "Symbol", None, "sym"), ("rev", "Revenue", "$", "big"), ("eps", "EPS", "$", "num"),
                   ("pe", "P/E", None, "num"), ("beta", "Beta", None, "num"), ("div", "Dividend", "$", "num"),
                   ("yld", "Yield", "%", "num")),
    "technicals": (("t", "Symbol", None, "sym"), ("tech_label", "Tech rating", None, "rating"),
                   ("ma_label", "MA rating", None, "rating"), ("os_label", "Os rating", None, "rating"),
                   ("rsi", "RSI 14", None, "num"), ("mom", "Momentum 10", None, "signed")),
}
#: The tab the board opens on, and the one whose numbers ride the first payload.
FIRST_TAB = "price"
#: The tabs whose numbers are fetched when the tab is first opened: Financials from the statements, the other three
#: from the bar history.
DERIVED_TABS = ("financials", "performance", "risk", "technicals")

PRICE_COLUMNS = COLUMNS["price"]


#: What the panel says while a tab's numbers are on their way.
SOON = "Reading the numbers for this tab…"


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
<table><thead><tr id="wl-head"></tr></thead><tbody id="wl-body"></tbody></table>
<div class="wl-soon" id="wl-soon" hidden>{e(SOON)}</div>
</div>
{HELP}
</main>
<script>
var WL_DATA = {script_json(data)};
{_JS}</script></body></html>"""


_JS = r"""
(function(){
  var data = WL_DATA, body = document.getElementById('wl-body'), head = document.getElementById('wl-head');
  var sort = [];                           // [{col, dir}], newest first; at most two columns
  var shut = {};                           // section id -> folded
  var into = data.sections.length ? data.sections[0].id : null;
  var tab = data.tab;                      // which column set is open
  var fetched = {price: true};             // tab -> true once its numbers are merged into the rows
  var pending = null;                      // the tab whose numbers are on their way

  function cols(){ return COLUMNS[tab] || COLUMNS.price; }
  function colAt(i){ return cols()[i]; }
  function rows(){
    var all = [];
    data.sections.forEach(function(s){ s.rows.forEach(function(r){ all.push(r); }); });
    return all;
  }

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
  var DASH = '<span class="dash">—</span>';
  var RATING_WORDS = {strong_buy: 'Strong buy', buy: 'Buy', neutral: 'Neutral', sell: 'Sell',
                      strong_sell: 'Strong sell'};
  function cell(row, col){
    var v = row[col.key];
    if(v === null || v === undefined) return DASH;
    if(col.kind === 'rating'){
      var score = row[col.key.replace('_label', '')];
      var word = RATING_WORDS[v] || v;
      var cls = (v === 'buy' || v === 'strong_buy') ? 'pos' : ((v === 'sell' || v === 'strong_sell') ? 'neg' : '');
      return '<span class="' + cls + '" title="' + (score === null || score === undefined ? '' :
        num(score, 2)) + '">' + rcEsc(word) + '</span>';
    }
    if(col.kind === 'big'){
      var b = big(v);
      if(b === null) return DASH;
      return col.key === 'rev' && row.fb ? '<span title="' + rcEsc(row.fb) + '">' + b + '</span>' : b;
    }
    if(col.kind === 'pct') return signed(v, 2, '%');
    if(col.kind === 'signed') return signed(v, 2, '');
    // 'num': the row's own unit only where it has one (a yield's %, on the Price tab). A loss's EPS reads with a
    // true minus, as TradingView writes it. Revenue and EPS say in their title which year they are (TTM or FY).
    var text = (v < 0 ? '−' : '') + num(Math.abs(v), 2)
      + (col.key === 'last' && row.unit ? '<span class="wl-u">' + row.unit + '</span>' : '');
    if(col.key === 'eps' && row.fb) return '<span title="' + rcEsc(row.fb) + '">' + text + '</span>';
    return text;
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
    var cells = '', set = cols(), un = row.cov !== 'us_equity' && row.cov !== 'market_series';
    if(un){
      // Each column keeps its own cell, empty of a number, so nothing moves sideways (the cell grid); the row
      // says why under its symbol instead of across the numbers.
      for(var j = 1; j < set.length; j++) cells += '<td class="dash">—</td>';
    } else {
      for(var i = 1; i < set.length; i++) cells += '<td>' + cell(row, set[i]) + '</td>';
    }
    return '<tr class="wl-row' + (un ? ' wl-un' : '') + '" data-sym="' + rcEsc(row.sym) + '" data-sec="' +
      rcEsc(sectionId) + '"><td><div class="wl-sym">' + grip('row', row.sym) +
      '<span class="wl-badge" title="' + rcEsc(un ? row.sym : (row.nm || row.sym)) + '">' + rcEsc(row.t) + '</span>' +
      (un ? '<span class="wl-nm" title="' + rcEsc(row.sym) + '"><span class="wl-nosrc">No source · ' +
            rcEsc(data.issue) + '</span></span>'
          : '<span class="wl-nm">' + rcEsc(row.nm || row.sym) + '</span>') + '</div></td>' + cells +
      '<td class="wl-xc"><button class="wl-x" type="button" data-remove="' + rcEsc(row.sym) +
      '" aria-label="Remove ' + rcEsc(row.t) + '">×</button></td></tr>';
  }
  function sectionHtml(section){
    var folded = !!shut[section.id], n = section.rows.length;
    var none = section.rows.filter(function(r){ return r.cov !== 'us_equity' && r.cov !== 'market_series'; }).length;
    var count = none === n && n ? n + ' · no source' : String(n);
    return '<tr class="wl-sec' + (folded ? ' shut' : '') + '" data-sec="' + rcEsc(section.id) +
      '"><td colspan="' + (cols().length + 1) + '"><div class="in">' + grip('sec', section.id) +
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
    body.innerHTML = html || '<tr><td colspan="' + cols().length +
      '"><div class="wl-empty">This list is empty. Type a ticker above to add one.</div></td></tr>';
    drawHead();
  }
  function drawHead(){
    // The header follows the open tab, so it is drawn rather than served: the five tabs do not share a shape.
    head.innerHTML = cols().map(function(col){
      var at = -1;
      for(var k = 0; k < sort.length; k++) if(sort[k].col === col.key) at = k;
      var sorted_by = at < 0 ? '' : ' aria-sort="' + (sort[at].dir === 'asc' ? 'ascending' : 'descending') + '"';
      var unit = col.unit ? '<span class="wl-hu">' + rcEsc(col.unit) + '</span>' : '';
      var rank = (at >= 0 && sort.length > 1) ? String(at + 1) : '';
      return '<th data-col="' + rcEsc(col.key) + '"' + sorted_by + '><button type="button">' +
        rcEsc(col.label) + unit + '<span class="wl-rank">' + rank + '</span>' + CARET + '</button></th>';
    }).join('') + '<th class="wl-xc"><span class="sr">Remove</span></th>';   // avo, 12 Nov 2026: the × sits at the far right
  }

  // ── sorting: click a header, click again to turn it round; shift-click adds a second column ──
  head.addEventListener('click', function(ev){
    var button = ev.target.closest('button');
    if(!button) return;
    var col = button.parentNode.getAttribute('data-col');
    var found = sort.filter(function(s){ return s.col === col; })[0];
    if(found && (ev.shiftKey || sort.length === 1)){ found.dir = found.dir === 'asc' ? 'desc' : 'asc'; }
    else if(!ev.shiftKey){ sort = [{col: col, dir: col === 't' ? 'asc' : 'desc'}]; }
    else if(!found){ sort.push({col: col, dir: col === 't' ? 'asc' : 'desc'}); sort = sort.slice(-2); }
    draw();
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
  var soon = document.getElementById('wl-soon'), SOON_TEXT = soon.textContent;
  function showTab(key){
    tab = key;
    sort = [];                                   // a column of the old tab means nothing on the new one
    document.querySelectorAll('.wl-tab').forEach(function(t){
      t.setAttribute('aria-selected', String(t.getAttribute('data-tab') === key));
    });
    var wired = !!COLUMNS[key];
    document.querySelector('.wl table').hidden = !wired;
    soon.hidden = wired;
    if(!wired){ return; }
    draw();
    if(fetched[key] || pending === key) return;
    pending = key;
    soon.hidden = false;
    soon.textContent = SOON_TEXT;
    fetch(LIST + '/columns/' + encodeURIComponent(key))
      .then(function(r){ return r.json(); })
      .then(function(answer){
        pending = null;
        soon.hidden = true;
        if(!answer || !answer.ok){ return; }
        fetched[key] = true;
        var values = answer.values || {};
        rows().forEach(function(row){
          var found = values[row.sym];
          if(found) for(var k in found) row[k] = found[k];
        });
        if(tab === key) draw();
      }, function(){
        pending = null;
        soon.textContent = 'The numbers could not be read. Reload the page to try again.';
      });
  }
  document.querySelectorAll('.wl-tab').forEach(function(button){
    button.addEventListener('click', function(){ showTab(button.getAttribute('data-tab')); });
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
_JS = (f"var COLUMNS = {script_json({tab: [{'key': k, 'label': lb, 'unit': u, 'kind': kd} for k, lb, u, kd in cols] for tab, cols in COLUMNS.items()})};\n"
       f"var CARET = {script_json(_CARET)};\nvar GRIP = {script_json(_GRIP)};\n" + ESC_JS + _JS)
