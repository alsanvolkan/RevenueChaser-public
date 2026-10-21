"""#185: re-slice the stored corpus from its stored documents — dry run, then apply.

Since #116/#200 the slicer has new Item 1/Item 4 patterns, while the stored
``chain_filings`` rows were cut with the old ones. ``dry_run`` re-slices the
stored documents offline and reports how the corpus and the ``chain_mentions``
edges would change — it writes nothing: neither DuckDB file nor the document
tree. ``apply`` is the real re-slice: the same per-row logic, the rows written
through ``engine.store_slices`` (its keep guard), then the edges rebuilt and the
run recorded as a sync does. It holds the ``edgar_sync`` lock and never fetches.

``chain_filings`` and ``chain_mentions`` both live in ``edgar.duckdb``, so the
dry run reads only that store, with ``duckdb.connect(..., read_only=True)``
directly, never through ``core.rawstore``, whose handles are always read-write.
``apply`` must write, so it goes through ``core.rawstore`` like every other
writer, under the lock a sync takes.
"""
from __future__ import annotations

import json
import logging
import os
import time
import zlib
from pathlib import Path

import duckdb

from RevenueChaser.core import config, edgar_docs, job_lock, ledger, rawstore

from . import edgar_text, engine

logger = logging.getLogger(__name__)

# The slice groups, plus ``no_doc`` (a row with nothing to re-slice) and ``error`` (a row whose
# slice raised). ``ok_text_changed`` is ok -> ok only; other same-rank text changes are
# ``same_rank_text_changed`` (short and missing share rank 0).
_GROUPS = ("improve", "kept", "relabel", "ok_text_changed", "same_rank_text_changed", "unchanged")
_EXTRA = ("no_doc", "error")
_EXCERPT = 400
_CONTEXT = 60


def _rank(quality) -> int:
    """``ok`` > ``overrun`` > ``short`` / ``missing`` (the store's keep guard)."""
    return engine._RANK.get(quality, 0)


def _classify(old_quality, old_text: str, new_quality, new_text: str) -> str:
    old_r, new_r = _rank(old_quality), _rank(new_quality)
    same_text = (old_text or "") == (new_text or "")
    if same_text and old_quality == "ok" and new_quality == "overrun":
        # Same slice, relabelled: not a worse slice, so not "kept".
        return "relabel"
    if new_r > old_r:
        return "improve"
    if new_r < old_r:
        return "kept"
    if same_text:
        return "unchanged"
    if old_quality == "ok" and new_quality == "ok":
        return "ok_text_changed"
    return "same_rank_text_changed"


def _written(old_quality, stored_text: str, new_quality, new_text: str) -> tuple:
    """The ``(quality, text)`` the store's guard would leave after the write.

    ``engine.store_slices`` keeps the stored slice against a worse one and writes
    the new one otherwise, so a kept row keeps the stored text; ``relabel`` and
    ``no_doc`` keep it too.
    """
    if _rank(new_quality) >= _rank(old_quality):
        return new_quality, new_text
    return old_quality, stored_text


def _as_text(cell) -> str:
    """A stored ``item_text`` cell: zlib-compressed BLOB in the store, or plain text."""
    if cell is None:
        return ""
    if isinstance(cell, str):
        return cell
    raw = bytes(cell)
    try:
        return zlib.decompress(raw).decode("utf-8")
    except zlib.error:
        return raw.decode("utf-8", errors="replace")


def _doc_html(docs_dir, cik, accession: str) -> str | None:
    """The stored document, read through ``edgar_docs`` with ``docs_dir`` as its root."""
    prev = edgar_docs.EDGAR_DOCS_DIR
    edgar_docs.EDGAR_DOCS_DIR = Path(docs_dir)
    try:
        return edgar_docs.read_html(cik, accession)
    finally:
        edgar_docs.EDGAR_DOCS_DIR = prev


def _slice_stored(row: dict, docs_dir, fetched_at: float | None = None) -> dict | None:
    """Re-slice one stored ``chain_filings`` row from its stored document (no network).

    The one per-row step dry run and apply share, so they cannot drift. Returns the
    new ``chain_filings`` row ``engine.store_slices`` expects, or ``None`` when the row
    has no stored document. Whatever the slicer raises is the caller's to count.

    The stored ``primary_doc`` is already the full document URL (``slice_document``
    builds it from the file name), so only its last segment is passed back in.
    ``fetched_at`` is the stored document's download time (``edgar_documents``, K6);
    without one the slice time is used, as ``edgar_text.document_slice`` does.
    """
    accession = row.get("accession")
    if not accession:
        return None
    html = _doc_html(docs_dir, row.get("cik"), accession)
    if html is None:
        return None
    filing = {**row, "primary_doc": str(row.get("primary_doc") or "").rsplit("/", 1)[-1]}
    return edgar_text.slice_document(row.get("ticker"), row.get("cik"), filing, html, fetched_at=fetched_at)


def _doc_stamps() -> dict[str, float]:
    """``accession -> fetched_at`` of the stored documents (``edgar_documents``), read once."""
    rows = rawstore.query("SELECT accession, fetched_at FROM edgar_documents WHERE fetched_at IS NOT NULL",
                          db="edgar")
    return {r["accession"]: r["fetched_at"] for r in rows}


def _has_table(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [name]).fetchone() is not None


def _table_rows(conn, name: str, order_by: str | None = None) -> list[dict]:
    """Rows of ``name``; a clear error when the edgar store does not hold it."""
    if not _has_table(conn, name):
        raise RuntimeError(f"table {name} not found in the edgar store (chain_* live in edgar.duckdb)")
    sql = f"SELECT * FROM {name}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _edge_key(edge: dict) -> tuple:
    return (edge.get("src"), edge.get("dst_name"), edge.get("relation"))


def _edge_view(edge: dict) -> dict:
    return {"src": edge.get("src"), "dst_name": edge.get("dst_name"),
            "dst_ticker": edge.get("dst_ticker") or "", "relation": edge.get("relation")}


def _first_diff(before: str, after: str) -> int:
    return len(os.path.commonprefix([before, after]))


def _diff(ticker, group, before: str, after: str) -> dict:
    """A sampled diff: both lengths plus an excerpt from the first differing offset."""
    start = max(0, _first_diff(before, after) - _CONTEXT)
    return {"ticker": ticker, "group": group,
            "before_len": len(before), "after_len": len(after),
            "before": before[start:start + _EXCERPT], "after": after[start:start + _EXCERPT]}


def _edge_diff(stored: dict, new: dict, baseline: dict, sample: int) -> dict:
    added, removed, unchanged = new.keys() - stored.keys(), stored.keys() - new.keys(), new.keys() & stored.keys()
    effect_added, effect_removed = new.keys() - baseline.keys(), baseline.keys() - new.keys()
    drift_added, drift_removed = baseline.keys() - stored.keys(), stored.keys() - baseline.keys()
    return {
        "stored": len(stored), "new": len(new), "baseline": len(baseline),
        "added": len(added), "removed": len(removed), "unchanged": len(unchanged),
        "sample_added": [_edge_view(new[k]) for k in sorted(added)[:sample]],
        "sample_removed": [_edge_view(stored[k]) for k in sorted(removed)[:sample]],
        "sample_unchanged": [_edge_view(new[k]) for k in sorted(unchanged)[:sample]],
        # The re-slice effect: current code on the re-sliced text vs current code on the stored text.
        "reslice_effect": {"added": len(effect_added), "removed": len(effect_removed),
                           "unchanged": len(new.keys() & baseline.keys())},
        # Drift: the stored mentions against the current code on the stored text (should be 0).
        "drift": {"added": len(drift_added), "removed": len(drift_removed),
                  "unchanged": len(baseline.keys() & stored.keys())},
    }


def dry_run(edgar_db, docs_dir, out_dir, sample: int = 20) -> dict:
    """Re-slice the stored corpus offline and write ``reslice_dry_run.{json,md}``.

    Classifies each ``chain_filings`` row into a group by comparing its stored slice
    with a fresh slice of its stored document, and diffs the ``chain_mentions`` edges
    against the edges the re-sliced corpus gives. Reads ``edgar.duckdb`` read-only and
    writes nothing back.
    """
    out_dir = Path(out_dir)
    conn = duckdb.connect(str(edgar_db), read_only=True)
    try:
        rows = _table_rows(conn, "chain_filings", order_by="ticker")
        mention_rows = _table_rows(conn, "chain_mentions")
    finally:
        conn.close()

    counts = {g: 0 for g in _GROUPS}
    counts.update({k: 0 for k in _EXTRA})
    transitions: dict[str, int] = {}
    diffs_by_group: dict[str, list[dict]] = {}
    new_keys: dict[tuple, dict] = {}
    baseline_keys: dict[tuple, dict] = {}
    for row in rows:
        ticker = row.get("ticker")
        old_quality = row.get("quality")
        stored_text = _as_text(row.get("item_text"))
        new_quality, new_text = None, None
        try:
            new = _slice_stored(row, docs_dir)
            if new is None:
                group = "no_doc"
            else:
                new_text = new.get("item_text") or ""
                new_quality = new.get("quality")
                group = _classify(old_quality, stored_text, new_quality, new_text)
                key = f"{old_quality} -> {new_quality}"
                transitions[key] = transitions.get(key, 0) + 1
        except Exception as exc:  # noqa: BLE001 - one bad document must not stop the run
            # A row whose slice failed keeps its stored slice, so its edges stay in both sets.
            logger.warning("reslice dry run: %s slice failed: %s", ticker, exc)
            group, new_quality, new_text = "error", None, None
        counts[group] += 1

        # The store's guard keeps the stored slice against a worse one; model what it would write.
        if new_quality is None:
            written_quality, written_text = old_quality, stored_text
        else:
            written_quality, written_text = _written(old_quality, stored_text, new_quality, new_text)
        if written_quality != "missing" and written_text:
            for edge in engine.filing_edges(ticker, written_text, row.get("filed") or ""):
                new_keys[_edge_key(edge)] = edge
        if old_quality != "missing" and stored_text:
            for edge in engine.filing_edges(ticker, stored_text, row.get("filed") or ""):
                baseline_keys[_edge_key(edge)] = edge

        if group not in ("unchanged", "no_doc", "error"):
            diffs_by_group.setdefault(group, []).append(
                _diff(ticker, group, stored_text, new_text or ""))

    if rows and counts["no_doc"] == len(rows):
        raise RuntimeError(
            f"no filing could be re-sliced: no document found for any of the {len(rows)} rows "
            f"under {docs_dir}")

    # Spread the sample over the groups (round-robin), not the first rows in storage order.
    order = [g for g in _GROUPS if g != "unchanged"]
    diffs: list[dict] = []
    idx = {g: 0 for g in order}
    while len(diffs) < sample:
        progressed = False
        for g in order:
            bucket = diffs_by_group.get(g) or []
            if idx[g] < len(bucket):
                diffs.append(bucket[idx[g]])
                idx[g] += 1
                progressed = True
                if len(diffs) >= sample:
                    break
        if not progressed:
            break

    stored_by_key = {_edge_key(m): m for m in mention_rows}
    report = {"counts": counts, "total": len(rows), "transitions": transitions,
              "diffs": diffs, "edges": _edge_diff(stored_by_key, new_keys, baseline_keys, sample)}

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "reslice_dry_run.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "reslice_dry_run.md").write_text(_markdown(report), encoding="utf-8")
    return report


def apply(docs_dir=None, out_dir=None, run_id: str | None = None) -> dict:
    """The real #185 re-slice: write every stored filing's fresh slice, rebuild the edges.

    Runs the dry run's per-row step (``_slice_stored``) over the live ``chain_filings``
    through ``core.rawstore``, hands every fresh row to ``engine.store_slices`` (its keep
    guard still refuses a worse slice), rebuilds ``chain_mentions`` and records the run as
    a sync does. Holds the ``edgar_sync`` lock for the whole write, so it never overlaps a
    sync or another apply, and the ``relatedness`` lock, which the 12:30 Relatedness run holds;
    if either is held, returns ``{"locked": True}`` and writes nothing. A dashboard Value Chain
    Fetch holds no lock, so the operator checks ``/api/jobs`` before a live run. Refuses (``RuntimeError``) when no row has a stored document under ``docs_dir``,
    before any write. Never touches the network. ``docs_dir`` defaults to ``config.EDGAR_DOCS_DIR``;
    the counts are written to ``reslice_apply.json`` under ``out_dir`` when one is given.
    """
    docs_dir = Path(docs_dir) if docs_dir is not None else config.EDGAR_DOCS_DIR
    with job_lock.acquire("edgar_sync", wait=0) as got, job_lock.acquire("relatedness", wait=0) as got_rel:
        if not got:
            return {"locked": True, "lock": "edgar_sync"}
        if not got_rel:
            return {"locked": True, "lock": "relatedness"}
        run_at = time.time()
        rid = run_id or ledger.new_run_id()
        rows = rawstore.get_all("chain_filings")
        counts = {g: 0 for g in _GROUPS}
        counts.update({k: 0 for k in _EXTRA})
        transitions: dict[str, int] = {}
        stamps = _doc_stamps()
        to_write: list[dict] = []
        for row in rows:
            old_quality = row.get("quality")
            stored_text = _as_text(row.get("item_text"))
            try:
                new = _slice_stored(row, docs_dir, stamps.get(row.get("accession")))
                if new is None:
                    counts["no_doc"] += 1
                    continue
                counts[_classify(old_quality, stored_text,
                                 new.get("quality"), new.get("item_text") or "")] += 1
                key = f"{old_quality} -> {new.get('quality')}"
                transitions[key] = transitions.get(key, 0) + 1
                to_write.append(new)
            except Exception as exc:  # noqa: BLE001 - one bad document must not stop the run
                logger.warning("reslice apply: %s slice failed: %s", row.get("ticker"), exc)
                counts["error"] += 1
        if rows and counts["no_doc"] == len(rows):
            raise RuntimeError(f"no filing could be re-sliced: no document found for any of the {len(rows)} "
                               f"rows under {docs_dir}")

        edges_before = rawstore.get_all("chain_mentions")
        written, kept = engine.store_slices(to_write)
        info: dict = {}
        edges = engine.build_edges(info)
        engine.record_history(edges, info, run_id=rid, run_at=run_at, counts=counts)

        result = {**counts, "rows": len(rows), "written": written, "kept": kept, "transitions": transitions,
                  "edges_before": len(edges_before), "edges_after": len(edges),
                  "edges_kept": bool(info.get("kept")), "run_id": rid}
        if out_dir is not None:
            out_dir = Path(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "reslice_apply.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result


def _markdown(report: dict) -> str:
    c, e = report["counts"], report["edges"]
    lines = ["# Value Chain re-slice dry run (#185)", "",
             f"- filings: {report['total']}"]
    for g in _GROUPS:
        lines.append(f"- {g}: {c[g]}")
    for k in _EXTRA:
        lines.append(f"- {k}: {c[k]}")
    lines += ["", "## Quality transitions", ""]
    if report["transitions"]:
        for key in sorted(report["transitions"]):
            lines.append(f"- {key}: {report['transitions'][key]}")
    else:
        lines.append("- _none_")
    if "short -> missing" in report["transitions"]:
        # short and missing share rank 0, so store_slices would write this and erase the short text.
        lines.append("")
        lines.append(f"- short -> missing: {report['transitions']['short -> missing']} — same rank, so the "
                     "store's guard would write this and erase the slice text")
    lines += ["", "## Filing diffs", ""]
    if not report["diffs"]:
        lines.append("_none_")
    for d in report["diffs"]:
        lines += [f"### {d['ticker']} — {d['group']}", "",
                  f"before ({d['before_len']} chars):", "", "```", d["before"] or "(empty)", "```", "",
                  f"after ({d['after_len']} chars):", "", "```", d["after"] or "(empty)", "```", ""]
    lines += ["## Edge diff", "",
              f"- stored: {e['stored']}, new: {e['new']}, baseline: {e['baseline']}, "
              f"added: {e['added']}, removed: {e['removed']}, unchanged: {e['unchanged']}",
              f"- re-slice effect (new vs baseline): added {e['reslice_effect']['added']}, "
              f"removed {e['reslice_effect']['removed']}, unchanged {e['reslice_effect']['unchanged']}",
              f"- drift (stored vs baseline): added {e['drift']['added']}, "
              f"removed {e['drift']['removed']}, unchanged {e['drift']['unchanged']}", ""]
    for label in ("added", "removed", "unchanged"):
        lines.append(f"### {label}")
        sample = e[f"sample_{label}"]
        lines += [f"- {x['src']} -> {x['dst_name']} ({x['relation']})" for x in sample] or ["- _none_"]
        lines.append("")
    return "\n".join(lines) + "\n"
