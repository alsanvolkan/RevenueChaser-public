"""#185: the stored-corpus re-slice dry run (read only, no network).

Since #116/#200 the slicer has new Item 1 patterns; the stored ``chain_filings``
rows were cut with the old ones. ``reslice.dry_run`` re-slices the stored
documents offline and reports how the corpus would change, without writing either
DuckDB file or the document tree.
"""
from __future__ import annotations

import hashlib
import json
import os
import zlib
from pathlib import Path

import duckdb

from RevenueChaser.core import edgar_docs
from RevenueChaser.module_value_chain import __main__ as cli
from RevenueChaser.module_value_chain import edgar_text, reslice, splitter

_BODY = "We make widgets for many industries and sell them worldwide. " * 60
_BODY_B = "We design chips for data centers across the world. " * 60


def _blob(text: str) -> bytes:
    return zlib.compress((text or "").encode("utf-8"), 6)


def _doc(body: str, stop: bool = True) -> str:
    inner = f"<p>{body}</p>"
    if stop:
        inner += "<p><b>Item 1A. Risk Factors</b></p><p>Risks.</p>"
    return f"<html><body><p><b>Item 1. Business</b></p>{inner}</body></html>"


_NO_DOC_HTML = "<html><body><p>Exhibit index only.</p></body></html>"


def _slice_text(doc: str) -> str:
    item, _q = splitter.slice_item(splitter.strip_html(doc), "10-K")
    return item or ""


def _row(ticker, cik, acc, quality, text, form="10-K", filed="2025-01-01"):
    return {"ticker": ticker, "cik": cik, "form": form, "filed": filed, "accession": acc,
            "primary_doc": f"https://www.sec.gov/Archives/{acc}/doc.htm",
            "item_text": text, "quality": quality}


def _edgar_db(path, rows, mentions=()):
    conn = duckdb.connect(str(path))
    conn.execute(
        "CREATE TABLE chain_filings (ticker TEXT PRIMARY KEY, cik TEXT, form TEXT, filed TEXT, "
        "accession TEXT, primary_doc TEXT, item_text BLOB, quality TEXT, fetched_at DOUBLE)")
    conn.execute(
        "CREATE TABLE chain_mentions (src TEXT, dst_name TEXT, dst_ticker TEXT, relation TEXT, "
        "snippet TEXT, filed TEXT)")
    for r in rows:
        conn.execute("INSERT INTO chain_filings VALUES (?,?,?,?,?,?,?,?,?)",
                     [r["ticker"], r["cik"], r["form"], r["filed"], r["accession"],
                      r["primary_doc"], _blob(r["item_text"]), r["quality"], None])
    for m in mentions:
        conn.execute("INSERT INTO chain_mentions VALUES (?,?,?,?,?,?)",
                     [m["src"], m["dst_name"], m.get("dst_ticker", ""), m["relation"],
                      m.get("snippet", ""), m.get("filed", "")])
    conn.close()


def _raw_db(path):
    conn = duckdb.connect(str(path))
    conn.close()


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── the five groups + no_doc ────────────────────────────────────────────────
def test_dry_run_classifies_every_group_and_writes_files(tmp_path, monkeypatch):
    docs_dir = tmp_path / "docs"
    monkeypatch.setattr(edgar_docs, "EDGAR_DOCS_DIR", docs_dir)
    raw, edgar = tmp_path / "raw.duckdb", tmp_path / "edgar.duckdb"
    _raw_db(raw)

    rel_doc = _doc(_BODY_B, stop=False)                 # overrun
    unc_doc = _doc(_BODY_B)                             # ok
    rows = [
        _row("IMP", "1", "acc-imp", "short", "tiny old"),                  # new ok -> improve
        _row("KEP", "2", "acc-kep", "ok", "a stored body"),               # new missing -> kept
        _row("REL", "3", "acc-rel", "ok", _slice_text(rel_doc)),          # ok -> overrun, same text
        _row("CHG", "4", "acc-chg", "ok", "STORED OLD TEXT"),             # ok -> ok, other text
        _row("UNC", "5", "acc-unc", "ok", _slice_text(unc_doc)),          # ok -> ok, same text
        _row("NOD", "6", "acc-nod", "ok", "no document stored"),          # no_doc
    ]
    _edgar_db(edgar, rows)
    edgar_docs.write_html("1", "acc-imp", _doc(_BODY))
    edgar_docs.write_html("2", "acc-kep", _NO_DOC_HTML)
    edgar_docs.write_html("3", "acc-rel", rel_doc)
    edgar_docs.write_html("4", "acc-chg", _doc(_BODY_B))
    edgar_docs.write_html("5", "acc-unc", unc_doc)

    out = tmp_path / "out"
    report = reslice.dry_run(edgar, docs_dir, out, sample=20)

    assert report["counts"] == {"improve": 1, "kept": 1, "relabel": 1,
                                "ok_text_changed": 1, "same_rank_text_changed": 0,
                                "unchanged": 1, "no_doc": 1, "error": 0}
    assert report["total"] == 6
    assert report["transitions"] == {"short -> ok": 1, "ok -> missing": 1, "ok -> overrun": 1, "ok -> ok": 2}
    assert (out / "reslice_dry_run.json").is_file()
    assert (out / "reslice_dry_run.md").is_file()
    saved = json.loads((out / "reslice_dry_run.json").read_text(encoding="utf-8"))
    assert saved["counts"] == report["counts"]


# ── read only ───────────────────────────────────────────────────────────────
def test_dry_run_leaves_both_duckdb_files_untouched(tmp_path, monkeypatch):
    docs_dir = tmp_path / "docs"
    monkeypatch.setattr(edgar_docs, "EDGAR_DOCS_DIR", docs_dir)
    raw, edgar = tmp_path / "raw.duckdb", tmp_path / "edgar.duckdb"
    _raw_db(raw)
    doc = _doc(_BODY)
    _edgar_db(edgar, [_row("UNC", "5", "acc-unc", "ok", _slice_text(doc))])
    edgar_docs.write_html("5", "acc-unc", doc)

    raw_hash, edgar_hash = _hash(raw), _hash(edgar)
    raw_mtime, edgar_mtime = os.stat(raw).st_mtime_ns, os.stat(edgar).st_mtime_ns

    reslice.dry_run(edgar, docs_dir, tmp_path / "out")

    assert _hash(raw) == raw_hash and _hash(edgar) == edgar_hash
    assert os.stat(raw).st_mtime_ns == raw_mtime
    assert os.stat(edgar).st_mtime_ns == edgar_mtime


def test_dry_run_opens_the_edgar_file_read_only(tmp_path, monkeypatch):
    """Unchanged bytes do not prove a read-only open (a writable one that writes nothing passes too)."""
    docs_dir = tmp_path / "docs"
    monkeypatch.setattr(edgar_docs, "EDGAR_DOCS_DIR", docs_dir)
    edgar = tmp_path / "edgar.duckdb"
    _edgar_db(edgar, [])
    modes: list[bool] = []
    real = reslice.duckdb.connect

    def spy(path, *a, **k):
        modes.append(bool(k.get("read_only")))
        return real(path, *a, **k)

    monkeypatch.setattr(reslice.duckdb, "connect", spy)
    reslice.dry_run(edgar, docs_dir, tmp_path / "out")
    assert modes == [True]


# ── edge diff ───────────────────────────────────────────────────────────────
def test_dry_run_edge_diff_counts(tmp_path, monkeypatch):
    docs_dir = tmp_path / "docs"
    monkeypatch.setattr(edgar_docs, "EDGAR_DOCS_DIR", docs_dir)
    raw, edgar = tmp_path / "raw.duckdb", tmp_path / "edgar.duckdb"
    _raw_db(raw)

    # Bodies long enough to slice ``ok`` (>= MIN_ITEM_CHARS), so the re-slice is written, not kept.
    tsmc = "We purchase wafers from Taiwan Semiconductor Manufacturing Company Limited. " * 40
    intel = "We purchase wafers from Intel Corporation. " * 80
    apple = "We sell to Apple Inc. " * 120
    rows = [
        _row("AAA", "10", "acc-aaa", "ok", tsmc),
        _row("BBB", "11", "acc-bbb", "ok", intel),
    ]
    mentions = [
        {"src": "AAA", "dst_name": "Taiwan Semiconductor", "dst_ticker": "TSM", "relation": "supplier"},
        {"src": "BBB", "dst_name": "Intel", "dst_ticker": "INTC", "relation": "supplier"},
    ]
    _edgar_db(edgar, rows, mentions)
    edgar_docs.write_html("10", "acc-aaa", _doc(tsmc))
    edgar_docs.write_html("11", "acc-bbb", _doc(apple))

    report = reslice.dry_run(edgar, docs_dir, tmp_path / "out")

    edges = report["edges"]
    assert (edges["stored"], edges["new"]) == (2, 2)
    assert (edges["added"], edges["removed"], edges["unchanged"]) == (1, 1, 1)
    assert edges["sample_added"][0]["dst_name"] == "Apple"
    assert edges["sample_removed"][0]["dst_name"] == "Intel"
    # The baseline is the current code on the stored text: it matches the stored mentions (drift 0).
    assert edges["baseline"] == 2
    assert edges["drift"] == {"added": 0, "removed": 0, "unchanged": 2}
    assert edges["reslice_effect"] == {"added": 1, "removed": 1, "unchanged": 1}


# ── CLI ─────────────────────────────────────────────────────────────────────
def test_cli_reslice_dry_run_parses_and_calls(monkeypatch, capsys):
    seen = {}

    def fake(edgar, docs, out, sample=20):
        seen.update(edgar=edgar, docs=docs, out=out)
        return {"counts": {"improve": 1}, "total": 1, "edges": {}}

    monkeypatch.setattr(cli.reslice, "dry_run", fake)
    code = cli.main(["--reslice-dry-run", "--edgar", "e.duckdb", "--docs", "docs", "--out", "out"])
    assert code == 0
    assert seen["edgar"] == Path("e.duckdb")
    assert seen["docs"] == Path("docs") and seen["out"] == Path("out")
    assert "improve" in capsys.readouterr().out


# ── a row whose slice raised keeps its edges (review follow-up) ─────────────
def test_an_error_row_keeps_its_stored_edges(tmp_path, monkeypatch):
    docs_dir = tmp_path / "docs"
    monkeypatch.setattr(edgar_docs, "EDGAR_DOCS_DIR", docs_dir)
    edgar = tmp_path / "edgar.duckdb"
    doc = _doc(_BODY_B)
    text = "Our largest customer is Microsoft Corporation and our supplier is Intel Corporation. " * 3
    stored = reslice.engine.filing_edges("ERR", text, "2025-01-01")
    _edgar_db(edgar, [_row("ERR", "7", "acc-err", "ok", text), _row("UNC", "5", "acc-unc", "ok", _slice_text(doc))],
              mentions=[{"src": e["src"], "dst_name": e["dst_name"], "relation": e["relation"]} for e in stored])
    edgar_docs.write_html("7", "acc-err", doc)
    edgar_docs.write_html("5", "acc-unc", doc)
    real = edgar_text.slice_document

    def boom(ticker, *a, **k):
        if ticker == "ERR":
            raise ValueError("bad document")
        return real(ticker, *a, **k)

    monkeypatch.setattr(reslice.edgar_text, "slice_document", boom)
    report = reslice.dry_run(edgar, docs_dir, tmp_path / "out")
    assert report["counts"]["error"] == 1
    assert stored                             # the error row does carry edges
    assert report["edges"]["removed"] == 0    # its stored mentions are not reported as removed
    assert report["edges"]["reslice_effect"]["removed"] == 0


# ── the dry run runs before the --floor / --tickers selection (stack R1, SHOULD 5) ──
def test_the_dry_run_never_reads_the_selection(monkeypatch, capsys, tmp_path):
    from RevenueChaser.module_value_chain import __main__ as cli

    def selection(_args):
        raise AssertionError("--reslice-dry-run must not resolve --floor through the live store")

    monkeypatch.setattr(cli, "_selection", selection)
    monkeypatch.setattr(cli.reslice, "dry_run",
                        lambda edgar, docs, out: {"counts": {"unchanged": 1}, "total": 1, "edges": {"added": 0}})
    assert cli.main(["--reslice-dry-run", "--edgar", str(tmp_path / "e.duckdb"), "--docs", str(tmp_path),
                     "--floor", "50"]) == 0
    assert '"unchanged": 1' in capsys.readouterr().out
