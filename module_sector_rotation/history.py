"""Rotation and Money Flow boards -> result history (#374, A3; avo's card 9: the Rotation panels and the money-flow
leaderboard per session).

Two boards, one module (both are built here, ``module_sector_rotation``):

- ``record_rotation`` is a step of a Rotation Fetch (``app/runners.run_rotation_latest``), after the board file. It
  hands ``core/history.record_run`` (board ``module_sector_rotation``) the rows the board embeds for its expandable
  rows: the top holdings of every theme ETF with their SPY-relative strength (``rotation_holdings_history``; sector
  rows show the industry tree instead, ROT-4) and the sector -> industry -> member tree of every sector ETF
  (``sector_tree_history``), and on a live Fetch the RRG points it saved to ``rotation_snapshots``
  (``rotation_snapshots_history``, avo's card). A range Fetch shows stored snapshots: it records no RRG points and its
  run is partial, so the session's snapshot is a live Fetch. So is a live Fetch that kept the stored same-day snapshot
  because its own pull came back thinner (ROT-5).
- ``record_money_flow`` is a step of a Money Flow Fetch (``app/runners.run_money_flow_latest``), after the leaderboard
  file, or where the runner keeps the stored leaderboard. It hands ``record_run`` (board ``module_money_flow``) the
  ranked inflows and outflows (``money_flow_history``).

The mappings live here, in the module, not in ``core/``. A Refresh never calls them (06 rule 9).

Both are session boards: the first complete run of a session keeps its rows; a later, intraday or partial run keeps
its header only (``core/history.record_run``). Rotation is partial when the RRG pull returned no ETF or dropped some
(an ETF with no D01 series, a macro series whose FRED/yfinance read failed), D01 did not hold the session yet (the RRG
is an older session's), the runner kept the stored session snapshot instead, no theme ETF got its holdings' relative
strength (e.g. SPY's closes were short), or the UW key was rejected, the client's breaker opened
or some UW calls failed. An ETF with too little D01 history for the RRG (``engine.MIN_WEEKS``) is a warning, not a
partial run (M-ROT-2): it is short on every run until its history grows. Money Flow is partial when D01 holds no bar
of an ETF it ranks (M-ROT-3), nothing was ranked, the runner kept the stored leaderboard, or UW calls failed.

``stored_leaderboard`` reads the newest complete Money Flow session back (K2 readers, ``core.history``): the
leaderboard a thin pull must not replace (ROT-9), and the one the runner rebuilds the board file from.
"""
from __future__ import annotations

from RevenueChaser.core import history, ledger
from RevenueChaser.module_sector_rotation import engine
from RevenueChaser.module_sector_rotation.universe import ROTATION_ETFS

ROTATION_BOARD, MONEY_FLOW_BOARD = "module_sector_rotation", "module_money_flow"
HOLDINGS_TABLE, TREE_TABLE, MONEY_FLOW_TABLE = "rotation_holdings_history", "sector_tree_history", "money_flow_history"
SNAPSHOTS_TABLE = "rotation_snapshots_history"
# Bump when the relative strength, the tree weights or the flow score change in a way the config hash does not see
# (``relative_strength_pct``, ``build_industry_tree``, ``get_money_flow_leaderboards``' formula), not a setting.
# 2 (M-ROT-1, #374): the recorded holdings are the theme ETFs' only (ROT-4), and the leaderboard ranks positive scores
# as inflows and negative ones as outflows (DF70, #181 R20); neither is a setting.
# 3 (M-ROT-2, #374): the recorded RRG points come from D01 weekly closes dated by their session (A5, #283), not UW's
# weekly candles dated by the machine's day.
# 4 (M-ROT-3, #374): the holdings' relative strength and the money-flow closes and volumes come from D01, not UW
# candles, and a holding with no bars is a recorded "n/a" row instead of a dropped one (ROT-8).
SELECTION_VERSION = "4"
_EXPANDABLE = ("theme",)                  # rows whose detail is the ETF's top holdings with RS (ROT-4)
_HOLDINGS_GROUPS = ("sector", "theme")    # rows with an expandable detail; a sector's is the industry tree
_NOT_RANKED = ("region", "bond", "yield", "commodity")       # money_flow.compute_money_flow skips these groups


def rotation_config(benchmark: str = "SPY", max_holdings: int = 10, max_per_industry: int = 12) -> dict:
    """What the Rotation board's holdings and tree are built with."""
    return {"benchmark": benchmark, "HOLDING_LOOKBACKS": dict(engine.HOLDING_LOOKBACKS),
            "SPY_MIN_CLOSES": engine.SPY_MIN_CLOSES, "max_holdings": max_holdings,
            "max_per_industry": max_per_industry}


def money_flow_config(days: int, window: int, top_n: int) -> dict:
    """What the Money Flow leaderboard is ranked with."""
    return {"days": days, "window": window, "top_n": top_n, "not_ranked_groups": list(_NOT_RANKED),
            "etfs": sorted(t for t, i in ROTATION_ETFS.items() if i.get("group") not in _NOT_RANKED)}


def model_version(params: dict) -> str:
    return ledger.model_version(SELECTION_VERSION, params)


def holdings_rows(holdings: dict[str, list[dict]]) -> tuple[list[dict], int]:
    """``(rows, duplicates)``: one row per ETF holding the board embeds, in its order (``position`` from 1). A holding
    listed twice for one ETF keeps its first row (the key is ``(etf, holding)``); ``duplicates`` counts the rest."""
    rows, seen, dup = [], set(), 0
    for etf, held in holdings.items():
        for pos, h in enumerate(held, start=1):
            if (etf, h.get("ticker")) in seen:
                dup += 1
                continue
            seen.add((etf, h.get("ticker")))
            rows.append({"etf": etf, "holding": h.get("ticker"), "group_name": ROTATION_ETFS.get(etf, {}).get("group"),
                         "position": pos, "name": h.get("name"), "weight": h.get("weight"), "close": h.get("close"),
                         "rs_1w": h.get("rs_1w"), "rs_1m": h.get("rs_1m"), "rs_3m": h.get("rs_3m")})
    return rows, dup


def tree_rows(sector_holdings: dict[str, list[dict]]) -> list[dict]:
    """One row per member of every sector ETF's industry tree, as the board embeds it."""
    rows = []
    for etf, industries in sector_holdings.items():
        sector = ROTATION_ETFS.get(etf, {}).get("name")
        for ind in industries:
            for m in ind.get("holdings") or []:
                rows.append({"sector": sector, "industry": ind.get("industry"), "ticker": m.get("ticker"),
                             "etf": etf, "name": m.get("name"), "market_cap": m.get("market_cap"),
                             "weight": m.get("weight")})
    return rows


def record_rotation(snapshots: list, holdings: dict, sector_holdings: dict, *, run_id: str, run_at: float,
                    uw_health: dict | None = None, benchmark: str = "SPY", window: tuple | None = None,
                    warnings: list | None = None, snapshot_rows: list[dict] | None = None, kept: str | None = None,
                    too_short: list | None = None, stale: str | None = None):
    """Record a Rotation Fetch: a header for every Fetch, with the holdings and tree rows the board embeds and the RRG
    points a live Fetch saved (``snapshot_rows``, the ``rotation_snapshots`` rows ``store.save_snapshot`` wrote).
    ``window`` is the ``(start, end)`` of a range Fetch, which shows stored snapshots: its run is partial, so a live
    Fetch is the session's snapshot. ``kept`` says why a live Fetch showed the stored same-day snapshot instead of its
    own thinner pull (ROT-5); that run is partial too. ``without_holdings`` lists the sector and theme rows with no ETF
    holdings: every sector (its detail is the industry tree) and any theme whose RS failed. ``too_short`` names the
    symbols left off the RRG for too little D01 history: a warning, and not ``missing`` (M-ROT-2). ``stale`` says the
    RRG is an older session's because D01 did not hold this one yet; that run is partial. Returns rows written."""
    expandable = sorted({s.ticker for s in snapshots if getattr(s, "group_name", "") in _EXPANDABLE})
    detailed = sorted({s.ticker for s in snapshots if getattr(s, "group_name", "") in _HOLDINGS_GROUPS})
    hrows, dup = holdings_rows(holdings)
    problems = history.uw_problems(uw_health)
    # RotationEngine.process_all leaves out a symbol with no series (missing) or under MIN_WEEKS weekly closes (short).
    short = list(too_short or [])
    wanted = [t for t in ROTATION_ETFS if t != "SPY"]
    got = {s.ticker for s in snapshots}
    missing = [t for t in wanted if t not in got and t not in short]
    if kept:
        problems.insert(0, kept)
    elif window:
        problems.insert(0, "a range Fetch shows stored snapshots, not this session's live RRG pull")
    elif not snapshots:
        problems.insert(0, "the RRG pull returned no ETF")
    elif missing:
        problems.insert(0, f"{len(missing)} of {len(wanted)} rotation ETFs are missing from the RRG (no stored series "
                           f"up to the session, or a macro read failed): {', '.join(missing)}")
    elif expandable and not holdings:
        problems.insert(0, f"none of the {len(expandable)} theme ETFs got its holdings' relative strength")
    if stale and not kept and not window:
        problems.insert(0, stale)
    warns = list(warnings or []) + ([f"{dup} duplicate ETF holdings kept their first row"] if dup else [])
    if short:
        warns.append(short_warning(short))
    params = rotation_config(benchmark)
    return history.record_run(
        ROTATION_BOARD, run_id=run_id, run_at=run_at, model_version=model_version(params),
        rows={HOLDINGS_TABLE: hrows, TREE_TABLE: tree_rows(sector_holdings),
              SNAPSHOTS_TABLE: list(snapshot_rows or [])}, params=params,
        context={"snapshots": len(snapshots), "expandable": len(expandable), "missing": missing,
                 "without_holdings": [t for t in detailed if not holdings.get(t)],
                 "window": [str(d) for d in window] if window else None},
        warnings=warns or None, partial="; ".join(problems) or None)


def short_warning(short: list[str]) -> str:
    """The job and history line for symbols with too little D01 history for the RRG."""
    return (f"{len(short)} rotation symbol(s) have under {engine.MIN_WEEKS} weeks of stored daily bars and are left "
            f"off the map until their history grows: {', '.join(short)}")


def money_flow_rows(result: dict) -> list[dict]:
    """The leaderboard rows: inflows then outflows, ranked from 1 in board order."""
    return [{"side": side, "rank": rank, "etf": r.get("ticker"), "name": r.get("name"),
             "flow_score": r.get("flow_score"), "as_of": result.get("as_of")}
            for side, key in (("inflow", "inflows"), ("outflow", "outflows"))
            for rank, r in enumerate(result.get(key) or [], start=1)]


def record_money_flow(result: dict, *, run_id: str, run_at: float, days: int, window: int, top_n: int,
                      uw_health: dict | None = None, kept: str | None = None):
    """Record a Money Flow Fetch: a header for every Fetch, with the leaderboard it ranked. ``kept`` names the stored
    leaderboard the runner kept instead (the board did not change). Returns rows written."""
    rows = money_flow_rows(result)
    problems = []
    if result.get("missing"):
        problems.append(f"{len(result['missing'])} ETFs have no stored daily bars up to the session: "
                        f"{', '.join(result['missing'])}")
    if not rows:
        problems.append("no ETF was ranked")
    if kept:
        problems.append(f"the board kept the stored leaderboard from {kept}")
    problems += history.uw_problems(uw_health)
    params = money_flow_config(days, window, top_n)
    return history.record_run(
        MONEY_FLOW_BOARD, run_id=run_id, run_at=run_at, model_version=model_version(params),
        rows={MONEY_FLOW_TABLE: rows}, params=params,
        context={"as_of": result.get("as_of"), "inflows": len(result.get("inflows") or []),
                 "outflows": len(result.get("outflows") or []), "kept": kept},
        partial="; ".join(problems) or None)


def stored_leaderboard() -> dict | None:
    """The newest complete Money Flow session in result history (K2: ``core.history.sessions``) in the board file's
    shape, ``{inflows, outflows, generated_at, as_of}`` plus ``run_id``; ``None`` when no session is recorded. A read
    never creates the history file; a store fault raises (the runner then falls back to the board file)."""
    from datetime import datetime

    from RevenueChaser.core import rawstore
    with rawstore.brief_wait():
        snaps = history.sessions(MONEY_FLOW_BOARD, 1)
        if not snaps:
            return None
        head = snaps[0]
        rows = rawstore.query(f"SELECT side, rank, etf, name, flow_score, as_of FROM {MONEY_FLOW_TABLE} "
                              "WHERE run_id = ? ORDER BY side, rank", [head["run_id"]], db="history")
    if not rows:
        return None
    side = {"inflow": [], "outflow": []}
    for r in rows:
        side.setdefault(r["side"], []).append({"ticker": r["etf"], "name": r["name"] or "",
                                               "flow_score": r["flow_score"]})
    days = [r["as_of"] for r in rows if r["as_of"] is not None]
    as_of = (head.get("context") or {}).get("as_of") or (max(days).isoformat() if days else None)
    when = head.get("run_at")
    return {"inflows": side["inflow"], "outflows": side["outflow"],
            "generated_at": datetime.fromtimestamp(when).isoformat() if when else None,
            "as_of": str(as_of)[:10] if as_of else None, "run_id": head["run_id"]}
