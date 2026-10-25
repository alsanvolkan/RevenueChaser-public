"""The hit-rate readability gate (#25 HR-2): can a family's hit rate be read yet, and against what.

``core/outcomes.py`` records what happened after each frozen pick and ``core/baselines.py`` what the same day's
universe did. This module reads both, at read time, and says per (family, ``model_version``) whether there is enough
behind a rate to print one. It is a pure read: no table, no write, no schema change; it can be recomputed on every
page load, and changing a rule below is one edit with nothing to migrate.

Rules (avo's decisions on #25; every number is a constant here):
- **A group is one (family, model_version).** The family is the ledger's ``source``; the version is the selection
  version the board froze with (``forecast_ledger.model_version``). Groups are never pooled: every selection-version
  bump restarts the count.
- **Dedupe.** Within a group, the same ticker frozen again within ``DEDUPE_DAYS`` calendar days of the record that was
  kept is a duplicate, and the earliest record is the one kept (whatever its outcome). The window is anchored on the
  kept record, so a ticker frozen every week is one record per 90 days, not one ever.
- **Records.** A kept record counts once its ``HEADLINE_HORIZON``-day outcome is ``closed`` with an MFE, on the current
  ``outcomes.MODEL_VERSION`` (a row of an older rule is not final: the nightly recomputes it). An open window, a pick
  with no outcome row yet and a 90-day row are not records.
- **Dirty records** are kept records the outcomes writer could not measure: ``split_check`` (the bars mix two price
  bases), ``no_bars``, or ``closed`` without an MFE. They are listed by name and are not records.
- **The gate:** at least ``MIN_RECORDS`` records over at least ``MIN_FREEZE_DATES`` distinct freeze dates. Before it is
  met there is no rate, only the counts; with no record at all the state says "no closed window yet".
- **The headline** (pre-registered, avo 2026-10-15): a hit is an MFE on the High of at least ``HIT_THRESHOLD`` within
  ``HEADLINE_HORIZON`` days, compared with the matched-volatility peers. Every other cut is exploratory.
- **Matched peers** (the reference app's definition, on HR-1's deciles): a record's peers are the closed
  ``forecast_baselines`` decile of its freeze date whose ``[vol_lo, vol_hi)`` holds the pick's own trailing vol (the
  vol HR-1 places names by). A record with no vol, no closed decile or fewer than ``MIN_PEERS`` peers leaves the
  comparison. The peer rate is the mean of the records' own peer rates, and the family rate printed beside it is
  recomputed on the same subset, so the two always rest on the same records. The universe comparison works the same
  way on the ``universe`` row; ``n = 0`` there means no membership snapshot that day, so no baseline.

Reads ``forecast_ledger``, ``forecast_outcomes``, ``forecast_baselines`` and (for the default vol) the adjusted daily
bars, through ``rawstore.query`` and ``prices.daily_bars_bases``; never writes, never calls a provider.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

from RevenueChaser.core import baselines, outcomes, prices, rawstore

HEADLINE_HORIZON = 30
HIT_THRESHOLD = 0.10            # MFE >= 10%, inclusive (the ``ge10`` column of the baselines)
MIN_RECORDS = 30
MIN_FREEZE_DATES = 3
DEDUPE_DAYS = 90
MIN_PEERS = 21                  # a peer group of fewer names is dropped (the reference app's rule)

NO_CLOSED_WINDOW, GATE_NOT_MET, GATE_MET = "no_closed_window", "gate_not_met", "gate_met"
DIRTY_SPLIT, DIRTY_NO_BARS, DIRTY_NO_MFE = "split_check", "no_bars", "no_mfe"

VolOf = Callable[[str, date], "float | None"]


def _picks() -> list[dict]:
    return rawstore.query(
        f"SELECT source, forecast_date, subject, model_version FROM {outcomes.LEDGER} WHERE target_type = ? "
        "ORDER BY source, model_version, subject, forecast_date", [outcomes.PICK])


def _outcomes() -> dict[tuple, dict]:
    rows = rawstore.query(
        f"SELECT source, forecast_date, subject, status, mfe FROM {outcomes.TABLE} "
        "WHERE target_type = ? AND horizon_days = ? AND model_version = ?",
        [outcomes.PICK, HEADLINE_HORIZON, outcomes.MODEL_VERSION])
    return {(r["source"], r["forecast_date"], r["subject"]): r for r in rows}


def _baselines() -> dict[date, list[dict]]:
    rows = rawstore.query(
        f"SELECT forecast_date, basis, n, ge10, vol_lo, vol_hi FROM {baselines.TABLE} "
        "WHERE horizon_days = ? AND model_version = ? AND status = ?",
        [HEADLINE_HORIZON, baselines.MODEL_VERSION, outcomes.CLOSED])
    out: dict[date, list[dict]] = {}
    for r in rows:
        out.setdefault(r["forecast_date"], []).append(r)
    return out


def dedupe(picks: list[dict]) -> tuple[list[dict], int]:
    """The kept picks of one group and the number dropped: per ticker, a pick within ``DEDUPE_DAYS`` of the last kept
    one is a duplicate. ``picks`` are of one (family, model_version)."""
    kept: list[dict] = []
    last: dict[str, date] = {}
    dropped = 0
    for p in sorted(picks, key=lambda p: (p["subject"], p["forecast_date"])):
        prev = last.get(p["subject"])
        if prev is not None and p["forecast_date"] - prev < timedelta(days=DEDUPE_DAYS):
            dropped += 1
            continue
        last[p["subject"]] = p["forecast_date"]
        kept.append(p)
    return kept, dropped


def _dirty_reason(o: dict | None) -> str | None:
    if o is None:
        return None
    if o["status"] == outcomes.SPLIT_CHECK:
        return DIRTY_SPLIT
    if o["status"] == outcomes.NO_BARS:
        return DIRTY_NO_BARS
    if o["status"] == outcomes.CLOSED and o["mfe"] is None:
        return DIRTY_NO_MFE
    return None


def _gate(records: int, dates: int) -> dict:
    if not records:
        return {"met": False, "reason": "no closed window yet"}
    short = []
    if records < MIN_RECORDS:
        short.append(f"{records} of {MIN_RECORDS} records")
    if dates < MIN_FREEZE_DATES:
        short.append(f"{dates} of {MIN_FREEZE_DATES} freeze dates")
    if short:
        return {"met": False, "reason": ", ".join(short)}
    return {"met": True, "reason": f"{records} records over {dates} freeze dates"}


def _peer_row(rows: list[dict], vol: float | None) -> dict | None:
    if vol is None:
        return None
    for r in rows:
        if r["basis"] == baselines.UNIVERSE:
            continue
        lo, hi = r["vol_lo"], r["vol_hi"]
        if (lo is None or vol >= lo) and (hi is None or vol < hi):
            return r
    return None


def _universe_row(rows: list[dict]) -> dict | None:
    return next((r for r in rows if r["basis"] == baselines.UNIVERSE), None)


def _compare(records: list[dict], baseline_of: Callable[[dict], "dict | None"], min_n: int) -> dict:
    """The family rate and the baseline rate on the records that have a baseline row of at least ``min_n`` names."""
    matched = []
    for r in records:
        row = baseline_of(r)
        if row is not None and (row["n"] or 0) >= min_n:
            matched.append((r, row["ge10"] / row["n"]))
    out = {"records": len(matched), "freeze_dates": len({r["forecast_date"] for r, _ in matched}),
           "dropped": len(records) - len(matched), "rate": None, "baseline": None, "gap": None}
    if matched:
        out["rate"] = sum(1 for r, _ in matched if r["hit"]) / len(matched)
        out["baseline"] = sum(b for _, b in matched) / len(matched)
        out["gap"] = out["rate"] - out["baseline"]
    return out


def trailing_vols(pairs) -> dict[tuple[str, date], float | None]:
    """``{(ticker, day): trailing vol}`` from the stored adjusted closes, by the rule HR-1 places its peers by
    (``baselines.realised_vol`` over the 21 closes ending on ``day``). ``None``: too few closes."""
    days_of: dict[str, set[date]] = {}
    for t, d in pairs:
        days_of.setdefault(t, set()).add(d)
    out: dict[tuple[str, date], float | None] = {}
    if not days_of:
        return out
    tickers = sorted(days_of)
    start = min(min(v) for v in days_of.values()) - timedelta(days=baselines.VOL_LOOKBACK_DAYS)
    end = max(max(v) for v in days_of.values())
    for i in range(0, len(tickers), baselines.CHUNK):
        chunk = tickers[i:i + baselines.CHUNK]
        frame = prices.daily_bars_bases(chunk, start=start, end=end, bases=("adjusted",))["adjusted"]
        bars = baselines._by_ticker(frame)
        for t in chunk:
            days = sorted(days_of[t])
            dates, closes = baselines._usable_closes(bars.get(t, []))
            for d, v in zip(days, baselines._vols_at(days, dates, closes)):
                out[(t, d)] = v
    return out


def summary(*, vol_of: VolOf | None = None) -> list[dict]:
    """One dict per (family, model_version), sorted by both; see the module rules.

    Keys: ``family``, ``model_version``; ``frozen`` / ``frozen_dates`` (kept picks and their dates, open ones too),
    ``duplicates``; ``records`` / ``freeze_dates`` (closed, clean, the gate's counts); ``open``; ``gate``
    (``met``, ``reason``); ``state`` (``NO_CLOSED_WINDOW`` / ``GATE_NOT_MET`` / ``GATE_MET``); ``headline`` (``None``
    before the gate: no rate is ever handed out before it); ``dirty`` (``subject``, ``forecast_date``, ``reason``).
    ``vol_of(ticker, day)``: a pick's trailing vol; default ``trailing_vols`` over the records that need one.
    """
    picks = _picks()
    if not picks:
        return []
    outs = _outcomes()
    groups: dict[tuple[str, str], list[dict]] = {}
    for p in picks:
        groups.setdefault((str(p["source"]), str(p["model_version"])), []).append(p)

    result = []
    for (family, version), members in sorted(groups.items()):
        kept, duplicates = dedupe(members)
        records, dirty, still_open = [], [], 0
        for p in kept:
            o = outs.get((p["source"], p["forecast_date"], p["subject"]))
            reason = _dirty_reason(o)
            if reason:
                dirty.append({"subject": p["subject"], "forecast_date": p["forecast_date"], "reason": reason})
            elif o is not None and o["status"] == outcomes.CLOSED:
                records.append({**p, "hit": o["mfe"] >= HIT_THRESHOLD})
            else:
                still_open += 1
        dates = len({r["forecast_date"] for r in records})
        gate = _gate(len(records), dates)
        result.append({
            "family": family, "model_version": version,
            "frozen": len(kept), "frozen_dates": len({p["forecast_date"] for p in kept}), "duplicates": duplicates,
            "records": len(records), "freeze_dates": dates, "open": still_open,
            "gate": gate,
            "state": GATE_MET if gate["met"] else GATE_NOT_MET if records else NO_CLOSED_WINDOW,
            "headline": records if gate["met"] else None,
            "dirty": sorted(dirty, key=lambda d: (d["forecast_date"], d["subject"])),
        })

    need = [(r["subject"], r["forecast_date"]) for g in result if g["headline"] for r in g["headline"]]
    if vol_of is None:
        vols = trailing_vols(need)
        vol_of = lambda t, d: vols.get((t, d))       # noqa: E731 - a lookup, named by the parameter
    rows_of = _baselines() if need else {}
    for g in result:
        if g["headline"] is None:
            continue
        records = g["headline"]
        hits = sum(1 for r in records if r["hit"])
        g["headline"] = {
            "horizon_days": HEADLINE_HORIZON, "threshold": HIT_THRESHOLD,
            "hits": hits, "records": len(records), "freeze_dates": g["freeze_dates"], "rate": hits / len(records),
            "peers": _compare(records, lambda r: _peer_row(rows_of.get(r["forecast_date"], []),
                                                           vol_of(r["subject"], r["forecast_date"])), MIN_PEERS),
            "universe": _compare(records, lambda r: _universe_row(rows_of.get(r["forecast_date"], [])), 1),
        }
    return result
