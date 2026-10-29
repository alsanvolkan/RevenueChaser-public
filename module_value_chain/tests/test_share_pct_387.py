"""Value Chain D7 (#387, DF57): the share a concentration sentence states is kept on the edge.

``concentration.find`` parsed the share ("Apple accounted for 29.8% of our net sales") and ``filing_edges`` dropped it.
Now it is a ``share_pct`` column on ``chain_mentions`` and on its history mirror ``value_chain_edges_history``, rides on
every ``focal_graph`` link, shows on the evidence row and sorts stated-share edges first. The column is additive: a
store from before gains it on the first call (``_reconcile_table``), old rows read NULL, and no schema bump is needed.
The selection version moves to 2, because a run's rows now carry the share. Offline: temp stores, no SEC request.
"""
from __future__ import annotations

import json

import duckdb
import pytest

from RevenueChaser.core import ledger, rawstore
from RevenueChaser.core.tests.board_js import run_board_js
from RevenueChaser.module_value_chain import engine, history as vc_history, report

_TABLES = ("chain_mentions", "value_chain_edges_history")
_SHARE_TEXT = ("We compete with suppliers such as Apple in some markets. Direct sales to Apple "
               "accounted for 29.8% of our net sales.")


def _forget_reconciliation():
    # A second ensure in the same process is a no-op unless the memos are reset.
    rawstore._TABLE_READY.clear()
    rawstore._ENSURED.clear()
    rawstore._HISTORY_READY.clear()


def _columns(db: str, table: str) -> dict[str, str]:
    con = duckdb.connect(str(rawstore._db_path(db)), read_only=True)
    try:
        return dict(con.execute("SELECT column_name, data_type FROM information_schema.columns "
                                "WHERE table_name = ?", [table]).fetchall())
    finally:
        con.close()


def _edge(**kw):
    return {"src": "AMKR", "dst_name": "Apple", "dst_ticker": "AAPL", "relation": "customer",
            "snippet": "Direct sales to Apple accounted for 29.8% of our net sales.", "filed": "2026-02-01", **kw}


# ── the MANIFEST and the store ──

def test_chain_mentions_and_its_mirror_declare_the_share_as_a_double():
    for table in _TABLES:
        decl = {c.split()[0]: c for c in rawstore.MANIFEST[table]["columns"]}
        assert decl.get("share_pct") == "share_pct DOUBLE", table


@pytest.fixture
def old_store(isolate_rawstore):
    """The store as it is before D7: both tables built without ``share_pct`` and holding a row each."""
    mp = pytest.MonkeyPatch()
    try:
        for table in _TABLES:
            spec = rawstore.MANIFEST[table]
            mp.setitem(rawstore.MANIFEST, table, {
                **spec, "columns": [c for c in spec["columns"] if c.split()[0] != "share_pct"]})
        _forget_reconciliation()
        rawstore.ensure_all_tables()
        rawstore.replace_all("chain_mentions", [_edge()])
        vc_history.record([_edge()], run_id="old", run_at=1.0, counts={})
    finally:
        mp.undo()
    for table, db in zip(_TABLES, ("edgar", "history")):
        assert "share_pct" not in _columns(db, table)
    _forget_reconciliation()
    ledger.take_notes()
    return isolate_rawstore


def test_the_first_store_call_adds_the_column_to_both_tables_and_old_rows_read_null(old_store):
    rawstore.ensure_all_tables()                       # what the dashboard's first store call does
    assert _columns("edgar", "chain_mentions")["share_pct"] == "DOUBLE"
    assert _columns("history", "value_chain_edges_history")["share_pct"] == "DOUBLE"
    assert rawstore.get_all("chain_mentions") == [{**_edge(), "share_pct": None}]
    (old,) = rawstore.query("SELECT * FROM value_chain_edges_history WHERE run_id = 'old'", db="history")
    assert old["share_pct"] is None and old["src"] == "AMKR"
    assert rawstore.schema_version() == "12", "the column came without a bump"


def test_the_first_fetch_on_an_old_store_writes_the_share_on_both_tables(old_store):
    rawstore.write_rows("chain_filings", [{
        "ticker": "AMKR", "cik": "1", "form": "10-K", "filed": "2026-02-01", "accession": "a",
        "primary_doc": "http://x", "quality": "ok", "item_text": _SHARE_TEXT}])
    engine.run(no_fetch=False, tickers=["AMKR"], run_id="new")
    stored = {(r["dst_ticker"], r["relation"]): r["share_pct"] for r in rawstore.get_all("chain_mentions")}
    assert stored == {("AAPL", "customer"): 29.8, ("AAPL", "other"): None}
    rows = rawstore.query("SELECT dst_ticker, relation, share_pct FROM value_chain_edges_history "
                          "WHERE run_id = 'new' ORDER BY row_no", db="history")
    assert [(r["dst_ticker"], r["relation"], r["share_pct"]) for r in rows] == [
        ("AAPL", "customer", 29.8), ("AAPL", "other", None)]
    assert ledger.take_notes() == [], "the history write failed"


# ── the selection version ──

def test_the_selection_version_is_2_so_the_model_version_changes():
    assert vc_history.SELECTION_VERSION == "3"      # 2 for #387 D7, 3 since G6 (XBRL customers)
    assert vc_history.model_version() != ledger.model_version("1", vc_history.effective_config())


# ── the focal graph ──

def test_a_focal_graph_link_carries_the_share_in_both_directions():
    edges = [_edge(share_pct=29.8),
             {"src": "AMKR", "dst_name": "Qualcomm", "dst_ticker": "QCOM", "relation": "customer",
              "snippet": "we sell to Qualcomm", "filed": "2026-02-01", "share_pct": None}]
    out = {l["dst"]: l["share_pct"] for l in engine.focal_graph(edges, "AMKR")["links"]}
    assert out == {"AAPL": 29.8, "QCOM": None}
    (incoming,) = engine.focal_graph(edges, "AAPL")["links"]
    assert incoming["incoming"] is True and incoming["share_pct"] == 29.8


def test_a_link_from_an_edge_with_no_share_key_reads_none():
    """A payload stored before D7 (a history payload, an old board file) has edges with no ``share_pct``."""
    legacy = {k: v for k, v in _edge().items()}
    (link,) = engine.focal_graph([legacy], "AMKR")["links"]
    assert link["share_pct"] is None


# ── the evidence row ──

def _page() -> str:
    return report.render_value_chain_dashboard({
        "as_of": "2026-09-17T00:00:00+00:00",
        "filings": [{"ticker": "AMKR", "name": "Amkor", "form": "10-K", "filed": "2026-02-01", "quality": "ok"}],
        "edges": [_edge(share_pct=29.8)], "universe_total": 1, "pending": 0})


def _drawn(tmp_path, links: list[dict]) -> str:
    tail = """
drawEdges(%s);
console.log(JSON.stringify(document.getElementById('edges').innerHTML));
""" % json.dumps(links)
    return run_board_js(_page(), tail, tmp_path)


def _link(dst, relation, share=None, hop=1, src="AMKR"):
    return {"src": src, "dst": dst, "relation": relation, "hop": hop, "snippet": "s", "share_pct": share}


def test_the_evidence_row_shows_the_stated_share_of_the_filers_sales_or_purchases(tmp_path):
    html = _drawn(tmp_path, [_link("AAPL", "customer", 29.8), _link("DELL", "supplier", 11.0)])
    assert '<span class="chip share">29.8% of AMKR&#39;s sales</span>' in html
    assert '<span class="chip share">11% of AMKR&#39;s purchases</span>' in html


def test_an_edge_with_no_share_shows_no_share_chip(tmp_path):
    html = _drawn(tmp_path, [_link("QCOM", "customer")])
    assert "% of" not in html


def test_stated_share_edges_sort_first_largest_first_within_a_hop(tmp_path):
    links = [_link("INTC", "supplier"), _link("QCOM", "customer", 11.1), _link("MSFT", "customer"),
             _link("AAPL", "customer", 29.8), _link("NVDA", "supplier", 40.0, hop=2, src="QCOM")]
    html = _drawn(tmp_path, links)
    order = [t for t in ("AAPL", "QCOM", "INTC", "MSFT", "NVDA") if t in html]
    assert sorted(order, key=html.index) == ["AAPL", "QCOM", "INTC", "MSFT", "NVDA"]


def test_the_share_is_escaped_like_every_other_field(tmp_path):
    html = _drawn(tmp_path, [_link("AAPL", "customer", "<img src=x onerror=__x(1)>")])
    assert "<img" not in html
