"""Last-good answer per (source, ticker) — the "never blank on failure" guarantee.

``save_results`` persists what the source adapters returned into ``identity_source_cache``
(raw.duckdb). The one rule that matters: **a source that failed or was skipped never
overwrites or deletes a previously cached answer.** A provider outage therefore cannot blank a
field; the rules keep reading the last good value (``last_good``) and the run log records that
the source was down.

* ``ok`` result: every row is upserted, except a row whose own ``status`` is ``error`` (the
  old answer stays). Rows absent from the result are left alone; nothing is ever deleted.
* ``error`` / ``skipped`` result: nothing is overwritten. Rows it still carries (partial
  progress of a run that stopped early) are inserted only for keys that have no cached answer.

Fields readers filter or join on are typed columns (``identity_source_cache`` in
``core/rawstore.py``); the rest of the row is the ``payload`` JSON.
"""
from __future__ import annotations

import json

from .. import rawstore
from .sources import SourceResult

TABLE = "identity_source_cache"
_TEXT_COLS = ("security_type", "security_type2", "sector", "industry", "sub_industry", "cik", "sic")
_BOOL_COLS = ("is_etf", "shell_closed")
_LIFTED = set(_TEXT_COLS) | set(_BOOL_COLS) | {"ticker", "status", "forms"}


def _key(row: dict) -> str:
    """The cache key of a row: its ticker, or its CIK for a per-CIK source row without one."""
    return str(row.get("ticker") or row.get("cik") or "").strip()


def _record(source: str, fetched_at: str, key: str, row: dict) -> dict:
    rec: dict = {"source": source, "ticker": key, "fetched_at": fetched_at,
                 "status": row.get("status") or "ok"}
    for col in _TEXT_COLS:
        v = row.get(col)
        rec[col] = None if v is None else str(v)
    for col in _BOOL_COLS:
        v = row.get(col)
        rec[col] = None if v is None else bool(v)
    forms = row.get("forms")
    rec["forms"] = ",".join(str(f) for f in forms) if isinstance(forms, (list, tuple, set)) else forms
    rec["payload"] = {k: v for k, v in row.items() if k not in _LIFTED}
    return rec


def _cached_keys(source: str) -> set[str]:
    return {r["ticker"] for r in rawstore.query(
        f"SELECT ticker FROM {TABLE} WHERE source = ?", [source])}


def save_results(results: list[SourceResult]) -> dict[str, dict]:
    """Persist source results; returns ``{source: {"saved": n, "note": str | None}}``.

    See the module docstring for the never-blank rule. Safe to call with a mix of ok, error
    and skipped results.
    """
    summary: dict[str, dict] = {}
    for result in results:
        usable = [(k, r) for r in result.rows if r.get("status") != "error" and (k := _key(r))]
        if result.ok:
            to_write = usable
            note = None
        else:
            cached = _cached_keys(result.source)
            to_write = [(k, r) for k, r in usable if k not in cached]
            note = result.error or result.status
        if to_write:
            rawstore.write_rows(TABLE, [_record(result.source, result.fetched_at, k, r) for k, r in to_write])
        summary[result.source] = {"saved": len(to_write), "note": note}
    return summary


def last_good(source: str) -> list[dict]:
    """Every cached row of ``source``, ordered by ticker, with the payload merged back in.

    ``forms`` comes back as a list, booleans as bools. Always the last good answers, because
    failed runs never reach the table.
    """
    out = []
    for rec in rawstore.query(f"SELECT * FROM {TABLE} WHERE source = ? ORDER BY ticker", [source]):
        payload = rec.pop("payload", None)
        if isinstance(payload, str):
            payload = json.loads(payload)
        row = {k: v for k, v in rec.items() if v is not None and k != "source"}
        forms = row.get("forms")
        if isinstance(forms, str):
            row["forms"] = [f for f in forms.split(",") if f]
        row.update(payload or {})
        out.append(row)
    return out
