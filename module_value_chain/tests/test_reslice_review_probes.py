"""#185 review probes (Opus review of b21239d). Copy into module_value_chain/tests/.

Red on b21239d: the first four. The last one is green on b21239d and kills the
"docs_dir ignored" mutant that the branch's own tests let survive.
"""
from RevenueChaser.core import edgar_docs
from RevenueChaser.module_value_chain import engine, reslice
from RevenueChaser.module_value_chain.tests.test_reslice_185 import (
    _BODY, _NO_DOC_HTML, _doc, _edgar_db, _raw_db, _row, _slice_text)

_TSMC = "We purchase wafers from Taiwan Semiconductor Manufacturing Company Limited. "


def _run(tmp_path, monkeypatch, rows, docs, mentions=(), global_dir=None):
    docs_dir = tmp_path / "docs"
    monkeypatch.setattr(edgar_docs, "EDGAR_DOCS_DIR", docs_dir)
    edgar = tmp_path / "edgar.duckdb"
    _edgar_db(edgar, rows, mentions)
    for cik, acc, html in docs:
        edgar_docs.write_html(cik, acc, html)
    if global_dir is not None:
        monkeypatch.setattr(edgar_docs, "EDGAR_DOCS_DIR", global_dir)
    return reslice.dry_run(edgar, docs_dir, tmp_path / "out")


def test_a_kept_row_keeps_its_edges(tmp_path, monkeypatch):
    # store_slices keeps the stored ok slice against a worse re-slice (engine.py:71), so
    # build_edges keeps reading the stored text: the dry run must not count its edges as removed.
    stored = (_TSMC * 40).strip()
    mentions = engine.filing_edges("AAA", stored, "2025-01-01")
    rep = _run(tmp_path, monkeypatch, [_row("AAA", "10", "acc-a", "ok", stored)],
               [("10", "acc-a", _NO_DOC_HTML)], mentions)
    assert rep["counts"]["kept"] == 1
    assert (rep["edges"]["removed"], rep["edges"]["new"]) == (0, len(mentions))


def test_short_to_missing_is_not_an_ok_text_change(tmp_path, monkeypatch):
    # same rank (0), so store_slices WOULD write it and erase the short text: report it apart.
    rep = _run(tmp_path, monkeypatch, [_row("SHM", "20", "acc-s", "short", "a short old slice")],
               [("20", "acc-s", _NO_DOC_HTML)])
    assert rep["counts"]["ok_text_changed"] == 0


def test_missing_to_short_is_not_an_ok_text_change(tmp_path, monkeypatch):
    html = ("<html><body><p><b>Item 1. Business</b></p><p>We make widgets.</p>"
            "<p><b>Item 1A. Risk Factors</b></p></body></html>")
    rep = _run(tmp_path, monkeypatch, [_row("MIS", "21", "acc-m", "missing", "")], [("21", "acc-m", html)])
    assert rep["counts"]["ok_text_changed"] == 0


def test_a_diff_shows_a_change_at_the_end_of_the_slice(tmp_path, monkeypatch):
    doc = _doc(_BODY)
    stored = _slice_text(doc) + " An extra closing paragraph that the old stop pattern let through."
    rep = _run(tmp_path, monkeypatch, [_row("TAIL", "22", "acc-t", "ok", stored)], [("22", "acc-t", doc)])
    d = rep["diffs"][0]
    assert d["group"] == "ok_text_changed" and d["before"] != d["after"]


def test_the_docs_argument_is_the_tree_that_is_read(tmp_path, monkeypatch):
    # The branch's tests point edgar_docs.EDGAR_DOCS_DIR at the same folder they pass as docs_dir,
    # so a dry run that ignored docs_dir passed them. Here the global points at an empty folder.
    doc = _doc(_BODY)
    rep = _run(tmp_path, monkeypatch, [_row("UNC", "5", "acc-u", "ok", _slice_text(doc))],
               [("5", "acc-u", doc)], global_dir=tmp_path / "elsewhere")
    assert rep["counts"]["unchanged"] == 1 and rep["counts"]["no_doc"] == 0
