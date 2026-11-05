"""``my_universe``: the names RevenueChaser itself keeps, and ``my_universe_changes``, who entered or left.

The spine stays unfiltered: ``security`` and ``etf`` keep every row they hold today. This module is the one place
that says which of those rows **we** work on, so a later store can be built from the list instead of from a tier
and a type filter of its own (avo, 4 Nov 2026: "bizim sectigimiz ticker ve EFTler icin ayri bir liste ... duckdb
sadece bizim istedigimiz olur").

**The rule** is avo's, and it lives in :data:`RULES_FILE` (``my_universe_rules.json``), not in this code, so that
the list can be swapped by editing one file:

- a **stock** is a ``security`` row of Tier A or D whose ``security_type`` is common, ADR, REIT or MLP. A royalty
  trust, a BDC and a non-SEC bank are operating types the identity master knows and avo left out; an untyped name
  stays out until it is typed (321 of them on 4 Nov 2026), and Tier B and C never enter whatever the type says;
- an **ETF** is an ``etf`` row that is no ETN and no CEF, with an ``aum`` at or over 100 million, or, where N-CEN
  gives it no AUM at all, an ``adv_usd_20d`` (the 20-session average daily dollar volume, ``etf.py``) at or over
  10 million. A **known** AUM under the floor is the answer: the volume test only decides a fund N-CEN is silent
  about (SPY and DIA as unit investment trusts, GLD, SLV, IBIT and the other commodity and crypto trusts). A fund
  with neither number is **undecided**: it does not enter, and all of them together are **one**
  ``my_universe_etf_undecided`` issue that names them -- one row per fund would be thousands of rows in
  ``ops.sqlite`` for a single input gap. Before N-CEN has answered at all (no ``aum`` anywhere) nothing is
  reported: that is the missing source's story, not the funds';
- a **delisted** name never enters, and neither source holds one: ``security`` is the SEC's current ticker and
  exchange file, ``etf`` Alpha Vantage's *active* rows, N-CEN's series and the spine's own ``etp`` rows;
- :data:`MANUAL_FILE` (``my_universe_manual.json``) adds and removes names by hand, with a reason each. A remove
  wins over an add of the same ticker; a line naming a ticker neither table holds is a
  ``my_universe_manual_unknown`` issue, never invented as a row.

**Both kinds at once.** A ticker the stock rule and the ETF rule both claim is kept once, as the stock, and
reported as ``my_universe_both_kinds``. It does not happen in a normal build (an ``etf`` row of the SEC equity
file is typed ``etp``, so it is Tier B and no stock), which is why it is worth saying out loud when it does.

**An input outage is not a rule change.** ``my_universe`` is exempt from the swap guard's shrink limit
(``serving.SHRINKS_BY_CHOICE``), so a missing ``prices.duckdb`` would otherwise drop every fund that entered on
its dollar volume, be accepted, and write a ``left`` row no later build can take back. :func:`_no_volume_source`
fails the build instead, and the live file stands.

**The changes table** is append-only across builds: the live file's rows are carried forward and today's diff
against the live ``my_universe`` is appended, so the Data settings board can show what entered and left without
keeping its own history. ``entered_on`` of a name the live list already held is the live row's, not today.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb

from .. import base
from . import etf as etf_mod, stage

TABLE = "my_universe"
CHANGES = "my_universe_changes"

#: avo's rules and his hand-written adds and removes. Owned by him; changed only by a pull request.
RULES_FILE = Path(__file__).with_name("my_universe_rules.json")
MANUAL_FILE = Path(__file__).with_name("my_universe_manual.json")

#: ``kind`` is what the name is to us, ``security_type`` the spine's own word for it (``etf`` for every ETF row).
#: ``rule`` is what let it in: ``stock``, ``etf_aum``, ``etf_adv`` or ``manual``; ``reason`` carries the number or
#: avo's words behind that rule. ``source`` names the spine table the row came from, or ``manual``.
SCHEMA = [("ticker", "TEXT PRIMARY KEY"), ("kind", "TEXT"), ("security_type", "TEXT"), ("name", "TEXT"),
          ("exchange", "TEXT"), ("tier", "TEXT"), ("rule", "TEXT"), ("reason", "TEXT"),
          ("aum", "DOUBLE"), ("aum_as_of", "DATE"), ("adv_usd_20d", "DOUBLE"), ("entered_on", "DATE"),
          ("source", "TEXT"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
CHANGES_SCHEMA = [("ticker", "TEXT"), ("kind", "TEXT"), ("change", "TEXT"), ("reason", "TEXT"), ("as_of", "DATE"),
                  ("run_id", "TEXT")]

STOCK, ETF = "stock", "etf"
ENTERED, LEFT = "entered", "left"
#: How many tickers an issue's detail names before it says "and N more".
MANY = 20
#: The issue kinds this module finds; ``security.ISSUE_KINDS`` holds their wording for ``ops.sqlite``.
ISSUES = ("my_universe_etf_undecided", "my_universe_manual_unknown", "my_universe_both_kinds")


@dataclass(frozen=True)
class Rules:
    """:data:`RULES_FILE` read and checked: the tiers and types a stock may have, and the ETF thresholds."""
    stock_tiers: tuple[str, ...]
    stock_types: tuple[str, ...]
    etf_aum_floor: float
    etf_adv_floor: float
    exclude_etn: bool
    exclude_cef: bool


def _need(mapping: dict, key: str, where: str):
    if key not in mapping:
        raise ValueError(f"{where}: {key} is missing; the universe rule is never defaulted here")
    return mapping[key]


def load_rules(path: Path | None = None) -> Rules:
    """:data:`RULES_FILE` (or ``path``) as :class:`Rules`. A missing rule raises: a silently defaulted threshold
    would change the list without a pull request, which is the one thing this file exists to prevent."""
    path = Path(path) if path is not None else RULES_FILE
    doc = json.loads(path.read_text(encoding="utf-8"))
    stocks = _need(doc, "stocks", str(path))
    tiers = tuple(str(t).strip().upper() for t in _need(stocks, "tiers", "stocks"))
    types = tuple(sorted(str(t).strip().lower() for t in _need(stocks, "types", "stocks")))
    etfs = _need(doc, "etfs", str(path))
    return Rules(stock_tiers=tiers, stock_types=types,
                 etf_aum_floor=float(_need(etfs, "aum_floor_usd", "etfs")),
                 etf_adv_floor=float(_need(etfs, "adv_usd_20d_floor", "etfs")),
                 exclude_etn=bool(_need(etfs, "exclude_etn", "etfs")),
                 exclude_cef=bool(_need(etfs, "exclude_cef", "etfs")))


def load_manual(path: Path | None = None) -> tuple[list[dict], list[dict]]:
    """``(adds, removes)`` of :data:`MANUAL_FILE` (or ``path``), each ``{"ticker", "reason"}`` with the ticker
    upper-cased. A line with no ticker is dropped here: it names nothing to add or remove."""
    path = Path(path) if path is not None else MANUAL_FILE
    doc = json.loads(path.read_text(encoding="utf-8"))

    def lines(key: str) -> list[dict]:
        out = []
        for row in doc.get(key) or []:
            ticker = str(row.get("ticker") or "").strip().upper()
            if ticker:
                out.append({"ticker": ticker, "reason": str(row.get("reason") or "").strip()})
        return out

    return lines("add"), lines("remove")


def _stocks(con: duckdb.DuckDBPyConnection, rules: Rules) -> list[dict]:
    tiers = ", ".join(repr(t) for t in rules.stock_tiers)
    types = ", ".join(repr(t) for t in rules.stock_types)
    return _rows(con, f"SELECT ticker, security_type, name, exchange, tier, source, source_as_of, fetched_at "
                      f"FROM security WHERE tier IN ({tiers}) AND security_type IN ({types}) ORDER BY ticker")


def _etfs(con: duckdb.DuckDBPyConnection, rules: Rules) -> list[dict]:
    out = ["TRUE"]
    if rules.exclude_etn:
        out.append("NOT coalesce(is_etn, FALSE)")
    if rules.exclude_cef:
        out.append("NOT coalesce(is_cef, FALSE)")
    return _rows(con, "SELECT ticker, name, aum, aum_as_of, adv_usd_20d, source, source_as_of, fetched_at "
                      f"FROM {etf_mod.TABLE} WHERE {' AND '.join(out)} ORDER BY ticker")


def _rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict]:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _normalise(manual: tuple[list[dict], list[dict]]) -> tuple[list[dict], list[dict]]:
    """The manual lines with their tickers upper-cased and the blank ones dropped, whoever handed them over: a
    caller that read the file itself, or a test that wrote the lines in place."""
    return tuple([{**r, "ticker": str(r.get("ticker") or "").strip().upper()}
                  for r in side if str(r.get("ticker") or "").strip()] for side in manual)


def _money(value: float) -> str:
    return f"{value / 1e6:,.1f}M USD"


def rows(ctx: base.RunContext, con: duckdb.DuckDBPyConnection, rules: Rules | None = None,
         manual: tuple[list[dict], list[dict]] | None = None,
         ) -> tuple[list[dict], list[dict], list[tuple[str, str, str]]]:
    """``(my_universe rows, my_universe_changes rows, issues)`` from the ``security`` and ``etf`` tables of ``con``.

    ``rules`` and ``manual`` default to the two files; a test passes its own. The changes are the live file's rows
    carried forward plus today's diff against the live ``my_universe`` (every name entered when there is none).
    """
    rules = rules if rules is not None else load_rules()
    adds, removes = _normalise(manual if manual is not None else load_manual())
    as_of = ctx.as_of or date.today()
    live = stage.live_rows(TABLE, ["ticker", "kind", "rule", "entered_on"])
    issues: list[tuple[str, str, str]] = []
    kept: dict[str, dict] = {}
    undecided: list[str] = []

    def row(src: dict, kind: str, rule: str, reason: str) -> dict:
        return {"ticker": src["ticker"], "kind": kind, "security_type": src.get("security_type") or ETF,
                "name": src.get("name"), "exchange": src.get("exchange"), "tier": src.get("tier"),
                "rule": rule, "reason": reason, "aum": src.get("aum"), "aum_as_of": src.get("aum_as_of"),
                "adv_usd_20d": src.get("adv_usd_20d"), "entered_on": as_of, "source": src.get("source"),
                "source_as_of": src.get("source_as_of"), "fetched_at": src.get("fetched_at"),
                "run_id": ctx.run_id}

    for s in _stocks(con, rules):
        kept[s["ticker"]] = row(s, STOCK, "stock", f"tier {s['tier']}, {s['security_type']}")
    for e in _etfs(con, rules):
        aum, adv = e["aum"], e["adv_usd_20d"]
        if aum is not None:
            if aum < rules.etf_aum_floor:
                continue
            rule, reason = "etf_aum", f"AUM {_money(aum)}"
        elif adv is not None:
            if adv < rules.etf_adv_floor:
                continue
            rule, reason = "etf_adv", f"no AUM; 20-session dollar volume {_money(adv)}"
        else:
            undecided.append(e["ticker"])
            continue
        if e["ticker"] in kept:
            issues.append(("my_universe_both_kinds", e["ticker"],
                           f"the stock rule and {rule} both claim it; kept as the stock"))
            continue
        kept[e["ticker"]] = row(e, ETF, rule, reason)

    _no_volume_source(con, live)
    # The plan's rule for a source that has never run: the rule is not applied, and the source is one
    # ``source_never_run`` issue instead of thousands of per-name ones. ``aum`` comes from N-CEN alone, so with no
    # AUM anywhere N-CEN has not answered yet and every fund is trivially "undecided" -- saying so per fund, or even
    # once, would report the missing source as a property of the funds.
    aum_known = int(con.execute(f"SELECT count(*) FROM {etf_mod.TABLE} WHERE aum IS NOT NULL").fetchone()[0])
    if undecided and aum_known:
        shown = ", ".join(undecided[:MANY])
        issues.append(("my_universe_etf_undecided", ETF,
                       f"{len(undecided)} ETFs have no AUM in N-CEN and no 20-session dollar volume yet: {shown}"
                       + (f" and {len(undecided) - MANY} more" if len(undecided) > MANY else "")))

    known = {r["ticker"]: r for r in _rows(con, "SELECT ticker, security_type, name, exchange, tier, source, "
                                                "source_as_of, fetched_at FROM security")}
    known_etf = {r["ticker"]: r for r in _rows(con, "SELECT ticker, name, aum, aum_as_of, adv_usd_20d, source, "
                                                    f"source_as_of, fetched_at FROM {etf_mod.TABLE}")}
    removed = {r["ticker"] for r in removes}
    for line in adds:
        ticker = line["ticker"]
        if ticker in removed:
            continue                                            # a remove of the same ticker wins; reported below
        if ticker in kept:
            continue                                            # the rules already let it in: their row stands
        sec_row, etf_row = known.get(ticker), known_etf.get(ticker)
        # ``security`` holds every ETF of the SEC equity file as an ``etp`` Tier B row (SPY, GLD), so resolving a
        # manual add there first would write a fund as a stock with no AUM. The stock row wins only when its type
        # is one the stock rule itself accepts; an untyped name with no ``etf`` row is still a stock.
        as_stock = sec_row is not None and (etf_row is None
                                            or (sec_row.get("security_type") or "") in rules.stock_types)
        if as_stock:
            kept[ticker] = row(sec_row, STOCK, "manual", line["reason"])
        elif etf_row is not None:
            kept[ticker] = row(etf_row, ETF, "manual", line["reason"])
        else:
            issues.append(("my_universe_manual_unknown", ticker,
                           "a manual add naming a ticker neither security nor etf holds"))
    for line in removes:
        ticker = line["ticker"]
        if ticker in kept:
            kept.pop(ticker)
        elif ticker not in known and ticker not in known_etf:
            issues.append(("my_universe_manual_unknown", ticker,
                           "a manual remove naming a ticker neither security nor etf holds"))

    out = [kept[t] for t in sorted(kept)]
    return out, _changes(out, live, as_of, ctx.run_id), issues


def _no_volume_source(con: duckdb.DuckDBPyConnection, live: list[dict] | None) -> None:
    """Refuse the build when the live list holds ``etf_adv`` names and **no** ``etf`` row has a dollar volume.

    ``my_universe`` is in ``serving.SHRINKS_BY_CHOICE``, so a shrink does not refuse the swap. Without this check a
    missing or unreadable ``prices.duckdb`` (:func:`etf.adv_usd_20d` then answers nothing) would drop every fund
    that entered on its volume, accept the swap, and write a ``left`` row that no later build can take back. An
    input outage is not a change of the rules, so the build fails and the live file stands.
    """
    if not live or not any(r["rule"] == "etf_adv" for r in live):
        return
    rows_with, rows_total = con.execute(
        f"SELECT count(*) FILTER (WHERE adv_usd_20d IS NOT NULL), count(*) FROM {etf_mod.TABLE}").fetchone()
    if rows_total and not rows_with:
        raise ValueError(
            f"{sum(1 for r in live if r['rule'] == 'etf_adv')} names of the live {TABLE} entered on their "
            f"20-session dollar volume and not one of the {rows_total} etf rows has one now: the prices store gave "
            f"no volume at all (missing, unreadable or empty). Refusing to drop them; fix the source and build "
            f"again, or take them out of {MANUAL_FILE.name} on purpose.")


def _changes(out: list[dict], live: list[dict] | None, as_of: date, run_id: str) -> list[dict]:
    """The live file's ``my_universe_changes`` rows, then today's entered and left; ``entered_on`` of a name the
    live list already held is set back to the live row's, in ``out`` itself."""
    if live is None:
        return [{"ticker": r["ticker"], "kind": r["kind"], "change": ENTERED, "reason": r["reason"],
                 "as_of": as_of, "run_id": run_id} for r in out]
    before = {r["ticker"]: r for r in live}
    carried = stage.live_rows(CHANGES, [c for c, _ in CHANGES_SCHEMA]) or []
    now = {r["ticker"]: r for r in out}
    for r in out:
        was = before.get(r["ticker"])
        if was is not None and was["entered_on"] is not None:
            r["entered_on"] = was["entered_on"]
    gone = [{"ticker": t, "kind": b["kind"], "change": LEFT,
             "reason": f"the rules no longer hold (was {b['rule']})", "as_of": as_of, "run_id": run_id}
            for t, b in sorted(before.items()) if t not in now]
    came = [{"ticker": r["ticker"], "kind": r["kind"], "change": ENTERED, "reason": r["reason"], "as_of": as_of,
             "run_id": run_id} for r in out if r["ticker"] not in before]
    return list(carried) + gone + came
