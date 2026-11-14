"""#185: the live apply of the stored-corpus re-slice (writes the store, no network).

``reslice.apply`` runs the dry run's per-row step over the live ``chain_filings``, writes
the fresh slices through the store's keep guard (``engine.store_slices``), rebuilds
``chain_mentions`` and records the run as a sync does. It holds the ``edgar_sync`` lock and
never fetches.
"""
from __future__ import annotations

import socket
import time

import pytest

from RevenueChaser.core import edgar_docs, job_lock, rawstore, sec_submissions
from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.module_value_chain import __main__ as cli
from RevenueChaser.module_value_chain import edgar_text, reslice

_BODY = "We make widgets for many industries and sell them worldwide. " * 60
_WAFER = "We purchase wafers from Taiwan Semiconductor Manufacturing Company Limited. "
_NO_DOC_HTML = "<html><body><p>Exhibit index only.</p></body></html>"


def _doc(body: str, stop: bool = True) -> str:
    inner = f"<p>{body}</p>"
    if stop:
        inner += "<p><b>Item 1A. Risk Factors</b></p><p>Risks.</p>"
    return f"<html><body><p><b>Item 1. Business</b></p>{inner}</body></html>"


def _frow(ticker, cik, acc, quality, text, form="10-K", filed="2025-01-01"):
    return {"ticker": ticker, "cik": cik, "form": form, "filed": filed, "accession": acc,
            "primary_doc": f"https://www.sec.gov/Archives/{acc}/doc.htm",
            "item_text": text, "quality": quality}


def _docs(tmp_path, monkeypatch):
    docs_dir = tmp_path / "docs"
    monkeypatch.setattr(edgar_docs, "EDGAR_DOCS_DIR", docs_dir)
    return docs_dir


# ── the write + the keep guard ──────────────────────────────────────────────
def test_apply_reslices_a_short_row_and_keeps_a_worse_one(rawstore_tmp_db, tmp_path, monkeypatch):
    docs_dir = _docs(tmp_path, monkeypatch)
    body = (_WAFER * 40).strip()
    short = _frow("AAA", "10", "acc-a", "short", "tiny old")
    keep = _frow("BBB", "11", "acc-b", "ok", "a stored body " * 20)
    rawstore.write_rows("chain_filings", [short, keep])
    edgar_docs.write_html("10", "acc-a", _doc(body))
    edgar_docs.write_html("11", "acc-b", _NO_DOC_HTML)

    res = reslice.apply(docs_dir=docs_dir)

    assert res["written"] == 1 and res["kept"] == 1
    assert res["improve"] == 1 and res["rows"] == 2
    a = rawstore.get("chain_filings", "AAA")
    assert a["quality"] == "ok" and "Taiwan Semiconductor" in a["item_text"]
    b = rawstore.get("chain_filings", "BBB")
    assert (b["quality"], b["item_text"], b["accession"]) == ("ok", keep["item_text"], "acc-b")
    # chain_mentions is rebuilt from the freshly written slice.
    assert any(m["src"] == "AAA" for m in rawstore.get_all("chain_mentions"))


def test_apply_writes_the_counts_file_when_an_out_dir_is_given(rawstore_tmp_db, tmp_path, monkeypatch):
    import json

    docs_dir = _docs(tmp_path, monkeypatch)
    rawstore.write_rows("chain_filings", [_frow("AAA", "10", "acc-a", "short", "tiny old")])
    edgar_docs.write_html("10", "acc-a", _doc((_WAFER * 40).strip()))

    out = tmp_path / "out"
    res = reslice.apply(docs_dir=docs_dir, out_dir=out)

    saved = json.loads((out / "reslice_apply.json").read_text(encoding="utf-8"))
    assert saved == res and saved["written"] == 1


# ── a row with nothing to re-slice is left untouched ────────────────────────
def test_a_row_with_no_stored_document_is_untouched_and_counted(rawstore_tmp_db, tmp_path, monkeypatch):
    docs_dir = _docs(tmp_path, monkeypatch)
    rawstore.write_rows("chain_filings", [_frow("NOD", "6", "acc-nod", "ok", "no document stored"),
                                          _frow("AAA", "10", "acc-a", "short", "tiny old")])
    edgar_docs.write_html("10", "acc-a", _doc((_WAFER * 40).strip()))

    res = reslice.apply(docs_dir=docs_dir)

    assert res["no_doc"] == 1 and res["written"] == 1 and res["kept"] == 0 and res["error"] == 0
    got = rawstore.get("chain_filings", "NOD")
    assert (got["quality"], got["item_text"], got["accession"]) == ("ok", "no document stored", "acc-nod")


def test_a_row_whose_slice_raises_is_left_untouched(rawstore_tmp_db, tmp_path, monkeypatch):
    docs_dir = _docs(tmp_path, monkeypatch)
    rawstore.write_rows("chain_filings", [_frow("ERR", "7", "acc-err", "ok", "the stored slice")])
    edgar_docs.write_html("7", "acc-err", _doc(_BODY))
    real = edgar_text.slice_document

    def boom(ticker, *a, **k):
        if ticker == "ERR":
            raise ValueError("bad document")
        return real(ticker, *a, **k)

    monkeypatch.setattr(reslice.edgar_text, "slice_document", boom)
    res = reslice.apply(docs_dir=docs_dir)

    assert res["error"] == 1 and res["written"] == 0 and res["kept"] == 0
    got = rawstore.get("chain_filings", "ERR")
    assert (got["quality"], got["item_text"], got["accession"]) == ("ok", "the stored slice", "acc-err")


# ── no network ──────────────────────────────────────────────────────────────
def test_apply_makes_no_network_call(rawstore_tmp_db, tmp_path, monkeypatch):
    docs_dir = _docs(tmp_path, monkeypatch)
    rawstore.write_rows("chain_filings", [_frow("AAA", "10", "acc-a", "short", "tiny old")])
    edgar_docs.write_html("10", "acc-a", _doc((_WAFER * 40).strip()))

    def boom(*a, **k):
        raise AssertionError("network call")

    monkeypatch.setattr(edgar_text, "fetch_filing", boom)
    monkeypatch.setattr(sec_edgar, "get_json", boom)
    monkeypatch.setattr(sec_submissions, "record", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)

    res = reslice.apply(docs_dir=docs_dir)

    assert res["written"] == 1 and not res.get("locked")
    assert rawstore.get("chain_filings", "AAA")["quality"] == "ok"


# ── the edgar_sync lock ─────────────────────────────────────────────────────
def test_apply_is_locked_out_and_writes_nothing(rawstore_tmp_db, tmp_path, monkeypatch):
    docs_dir = _docs(tmp_path, monkeypatch)
    rawstore.write_rows("chain_filings", [_frow("AAA", "10", "acc-a", "short", "tiny old")])
    edgar_docs.write_html("10", "acc-a", _doc((_WAFER * 40).strip()))
    out = tmp_path / "out"

    with job_lock.acquire("edgar_sync", wait=0) as got:
        assert got
        res = reslice.apply(docs_dir=docs_dir, out_dir=out)

    assert res == {"locked": True, "lock": "edgar_sync"}
    assert rawstore.get("chain_filings", "AAA")["quality"] == "short"
    assert not (out / "reslice_apply.json").exists()


# ── the result history run ──────────────────────────────────────────────────
def test_apply_records_a_result_history_run(rawstore_tmp_db, tmp_path, monkeypatch):
    docs_dir = _docs(tmp_path, monkeypatch)
    rawstore.write_rows("chain_filings", [_frow("AAA", "10", "acc-a", "short", "tiny old")])
    edgar_docs.write_html("10", "acc-a", _doc((_WAFER * 40).strip()))

    res = reslice.apply(docs_dir=docs_dir, run_id="rid-185")

    assert res["run_id"] == "rid-185"
    heads = rawstore.query("SELECT * FROM result_runs WHERE board = ?", ["module_value_chain"], db="history")
    assert len(heads) == 1 and heads[0]["run_id"] == "rid-185"
    rows = rawstore.query("SELECT * FROM value_chain_edges_history WHERE run_id = ?", ["rid-185"], db="history")
    assert rows and all(r["run_id"] == "rid-185" for r in rows)


# ── CLI ─────────────────────────────────────────────────────────────────────
def test_cli_reslice_apply_parses_and_calls(monkeypatch, capsys):
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return {"locked": False, "written": 2, "run_id": "x"}

    monkeypatch.setattr(cli.reslice, "apply", fake)
    code = cli.main(["--reslice-apply", "--out", "out"])

    assert code == 0 and seen["out_dir"]
    assert '"written": 2' in capsys.readouterr().out


def test_cli_reslice_apply_locked_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(cli.reslice, "apply", lambda **kw: {"locked": True})
    assert cli.main(["--reslice-apply"]) == 1
    assert '"locked": true' in capsys.readouterr().out


@pytest.mark.parametrize("extra", [["--reslice-dry-run"], ["--tickers", "AAA"],
                                   ["--floor", "50"], ["--fetch"], ["--sync-filings"]])
def test_cli_reslice_apply_is_exclusive(extra):
    with pytest.raises(SystemExit):
        cli.main(["--reslice-apply", *extra])


def test_the_apply_never_reads_the_selection(monkeypatch, capsys):
    def selection(_args):
        raise AssertionError("--reslice-apply must not resolve --floor through the live store")

    monkeypatch.setattr(cli, "_selection", selection)
    monkeypatch.setattr(cli.reslice, "apply",
                        lambda **kw: {"locked": False, "written": 0, "edges_after": 0})
    assert cli.main(["--reslice-apply"]) == 0
    assert '"written": 0' in capsys.readouterr().out


# ── review fixes (Opus review of ae6a5b5) ───────────────────────────────────
def test_apply_keeps_the_document_url_flat(rawstore_tmp_db, tmp_path, monkeypatch):
    """MUST 1: the stored primary_doc is already a full URL; the re-slice must not nest it into a second one."""
    docs_dir = _docs(tmp_path, monkeypatch)
    rawstore.write_rows("chain_filings", [_frow("AAA", "10", "acc-a", "short", "tiny old")])
    edgar_docs.write_html("10", "acc-a", _doc((_WAFER * 40).strip()))

    reslice.apply(docs_dir=docs_dir)

    url = rawstore.get("chain_filings", "AAA")["primary_doc"]
    assert url == edgar_text.document_url("10", "acc-a", "doc.htm")
    assert url.count("https://") == 1


def test_apply_keeps_the_document_download_time(rawstore_tmp_db, tmp_path, monkeypatch):
    """MUST 2 (K6): a re-slice carries the stored document's edgar_documents.fetched_at, not the time of the apply."""
    docs_dir = _docs(tmp_path, monkeypatch)
    rawstore.write_rows("chain_filings", [_frow("AAA", "10", "acc-a", "short", "tiny old"),
                                          _frow("CCC", "12", "acc-c", "short", "tiny old")])
    edgar_docs.write_html("10", "acc-a", _doc((_WAFER * 40).strip()))
    edgar_docs.write_html("12", "acc-c", _doc((_WAFER * 40).strip()))
    rawstore.write_rows("edgar_documents", [{"accession": "acc-a", "cik": "10", "form": "10-K",
                                             "filed": "2025-01-01", "fetched_at": 1_700_000_000.0}])

    before = time.time()
    reslice.apply(docs_dir=docs_dir)

    assert rawstore.get("chain_filings", "AAA")["fetched_at"] == 1_700_000_000.0
    assert rawstore.get("chain_filings", "CCC")["fetched_at"] >= before      # no metadata row: the slice time


def test_apply_is_locked_out_while_relatedness_runs(rawstore_tmp_db, tmp_path, monkeypatch):
    """SHOULD (a): the 12:30 Relatedness run and a dashboard Value Chain Fetch hold the relatedness lock."""
    docs_dir = _docs(tmp_path, monkeypatch)
    rawstore.write_rows("chain_filings", [_frow("AAA", "10", "acc-a", "short", "tiny old")])
    edgar_docs.write_html("10", "acc-a", _doc((_WAFER * 40).strip()))

    with job_lock.acquire("relatedness", wait=0) as got:
        assert got
        res = reslice.apply(docs_dir=docs_dir)

    assert res.get("locked") is True
    assert rawstore.get("chain_filings", "AAA")["quality"] == "short"


def test_apply_reports_transitions_and_kept_edges(rawstore_tmp_db, tmp_path, monkeypatch):
    """SHOULD (b): the result carries the quality transitions and whether build_edges kept the stored edges."""
    docs_dir = _docs(tmp_path, monkeypatch)
    rawstore.write_rows("chain_filings", [_frow("AAA", "10", "acc-a", "short", "tiny old")])
    edgar_docs.write_html("10", "acc-a", _doc((_WAFER * 40).strip()))

    res = reslice.apply(docs_dir=docs_dir)

    assert res["transitions"] == {"short -> ok": 1}
    assert "edges_kept" in res


def test_apply_refuses_when_no_row_has_a_document(rawstore_tmp_db, tmp_path, monkeypatch):
    """SHOULD (b): a wrong docs folder (every row no_doc) is an error, never a silent no-op run."""
    docs_dir = _docs(tmp_path, monkeypatch)
    rawstore.write_rows("chain_filings", [_frow("NOD", "6", "acc-nod", "ok", "no document stored")])

    with pytest.raises(RuntimeError):
        reslice.apply(docs_dir=docs_dir)
    assert rawstore.query("SELECT * FROM result_runs", db="history") == []
