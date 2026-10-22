"""SEC 13F ownership breadth (#375 F4, Chen-Hong-Stein 2002).

Breadth is the share of a quarter's 13F filers that hold the name: ``holders / filers``, as a percentage with
four decimals (a few hundred holders against thousands of filers reads as 0.0 as a fraction). ``breadth_chg``
is the quarter-over-quarter change in percentage points, ``None`` in the first quarter. The denominator is the
distinct ``cik`` in ``sec13f_holdings`` for that ``period`` — the same rows the numerator is counted from — and
a period with no filer total gives ``None``, never 0.

Offline: the store is the per-test temp DuckDB (``rawstore_tmp_db``), no SEC or UW request.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import history as core_history, ledger, rawstore
from RevenueChaser.module_sec_13f import engine, history as sec13f_history, report

Q1, Q2 = "2026-03-31", "2026-06-30"
RUN_AT = 1759701600.0        # a fixed instant after Q1/Q2 above, never "today"
AAA, BBB, CCC = "111111111", "222222222", "333333333"


def _filing(accession, cik, period):
    return {"accession": accession, "cik": cik, "period": period, "filing_date": period,
            "type": "13F-HR"}


def _holding(accession, cik, period, cusip, shares, line_no=1):
    return {"accession": accession, "line_no": line_no, "cusip": cusip, "cik": cik,
            "shares": float(shares), "value": float(shares) * 10.0, "period": period,
            "sshprnamttype": "SH", "putcall": ""}


@pytest.fixture
def store(rawstore_tmp_db):
    """Q1: two filers (c1, c2). Q2: four (c1..c4).

    AAA: 1 holder in Q1, 2 in Q2 → 50.0 then 50.0. BBB: 1 holder both quarters → 50.0 then 25.0.
    CCC: held by both Q1 filers and two of the four Q2 filers → 100.0 then 50.0.
    """
    rawstore.write_rows("sec13f_submissions", [
        _filing("a1", "c1", Q1), _filing("a2", "c2", Q1),
        _filing("b1", "c1", Q2), _filing("b2", "c2", Q2), _filing("b3", "c3", Q2), _filing("b4", "c4", Q2),
    ])
    rawstore.write_rows("sec13f_holdings", [
        _holding("a1", "c1", Q1, AAA, 100, 1), _holding("a1", "c1", Q1, BBB, 50, 2),
        _holding("a1", "c1", Q1, CCC, 10, 3),
        _holding("a2", "c2", Q1, CCC, 10, 1),
        _holding("b1", "c1", Q2, AAA, 100, 1), _holding("b1", "c1", Q2, BBB, 50, 2),
        _holding("b1", "c1", Q2, CCC, 10, 3),
        _holding("b2", "c2", Q2, AAA, 100, 1), _holding("b2", "c2", Q2, CCC, 10, 2),
        _holding("b3", "c3", Q2, "999999999", 5, 1),   # filers of the quarter that do not hold AAA/BBB/CCC
        _holding("b4", "c4", Q2, "999999999", 5, 1),
    ])
    engine.save_cusip_ticker({AAA: "AAA", BBB: {"ticker": "BBB"}, CCC: {"ticker": "CCC"}})
    return rawstore_tmp_db


def _totals():
    totals, _factors = engine.compute_totals(engine.load_cusip_ticker())
    return totals


def test_breadth_is_holders_over_that_quarters_filers(store):
    """Test 1: 2 filers in Q1 and 4 in Q2; 1→2 holders gives 50.0 and 50.0, 1→1 gives 50.0 and 25.0."""
    totals = _totals()
    assert totals["AAA"][Q1]["breadth"] == pytest.approx(50.0)
    assert totals["AAA"][Q2]["breadth"] == pytest.approx(50.0)
    assert totals["BBB"][Q1]["breadth"] == pytest.approx(50.0)
    assert totals["BBB"][Q2]["breadth"] == pytest.approx(25.0)
    # Four decimals: a name held by one filer of three thousand is 0.0333, not 0.0.
    m = engine.compute_period_metrics({Q1: {"shares": 1.0, "value": 10.0, "cik": {"c1": 1.0}}}, filers={Q1: 3000})
    assert m[Q1]["breadth"] == 0.0333


def test_breadth_chg_is_none_in_the_first_quarter_and_points_afterwards(store):
    """Test 2: the first quarter has no prior breadth, exactly as ``new_holders`` has none."""
    totals = _totals()
    assert totals["AAA"][Q1]["breadth_chg"] is None
    assert totals["BBB"][Q1]["breadth_chg"] is None
    assert totals["AAA"][Q2]["breadth_chg"] == pytest.approx(0.0)
    assert totals["BBB"][Q2]["breadth_chg"] == pytest.approx(-25.0)


def test_an_unknown_filer_total_is_none_not_zero():
    """Test 3: a period the filer read did not cover is unknown, never 0 %."""
    periods = {Q1: {"shares": 100.0, "value": 1000.0, "cik": {"c1": 100.0}},
               Q2: {"shares": 100.0, "value": 1000.0, "cik": {"c1": 100.0}}}
    m = engine.compute_period_metrics(periods, filers={Q1: 2})
    assert m[Q1]["breadth"] == pytest.approx(50.0)
    assert m[Q2]["breadth"] is None
    assert m[Q2]["breadth_chg"] is None          # unknown, so no change either
    # And no filers at all is unknown too, not a division by zero.
    m = engine.compute_period_metrics(periods)
    assert m[Q1]["breadth"] is None and m[Q1]["breadth_chg"] is None


def test_the_denominator_is_per_period_never_global(store):
    """Test 4: Q1's count must not leak into Q2. CCC is held by both Q1 filers and half the Q2 filers."""
    totals = _totals()
    assert totals["CCC"][Q1]["holders"] == 2
    assert totals["CCC"][Q1]["breadth"] == pytest.approx(100.0)
    assert totals["CCC"][Q2]["breadth"] == pytest.approx(50.0)
    assert totals["CCC"][Q2]["breadth_chg"] == pytest.approx(-50.0)


def test_the_history_row_carries_both_fields(store):
    """Test 5a: ``metric_rows`` → ``record`` writes breadth and its change."""
    totals = _totals()
    _quarter, rows = sec13f_history.metric_rows(totals, {}, {}, engine.load_cusip_ticker())
    by_ticker = {r["ticker"]: r for r in rows}
    assert by_ticker["BBB"]["breadth"] == pytest.approx(25.0)
    assert by_ticker["BBB"]["breadth_chg"] == pytest.approx(-25.0)
    sec13f_history.record({"totals": totals, "meta": {}, "enrichment": {}}, run_id="r1", run_at=RUN_AT)
    stored = {r["ticker"]: r for r in rawstore.query(
        "SELECT ticker, breadth, breadth_chg FROM sec13f_metrics_history WHERE run_id = 'r1'", db="history")}
    assert stored["BBB"]["breadth"] == pytest.approx(25.0)
    assert stored["BBB"]["breadth_chg"] == pytest.approx(-25.0)


def test_the_metric_set_bump_changes_the_model_version(monkeypatch):
    """Test 5b: the added metrics are a selection change the config hash cannot see, so the version says 5."""
    now = sec13f_history.model_version()
    assert now == ledger.model_version(sec13f_history.SELECTION_VERSION, sec13f_history.effective_config())
    monkeypatch.setattr(sec13f_history, "SELECTION_VERSION", "4")
    assert sec13f_history.model_version() != now


def test_the_board_shows_breadth_between_new_exit_and_top10_with_the_lag_legend():
    """Test 6: the column and the 'filed up to 45 days later' label the signal depends on."""
    totals = {"AAA": {Q1: {"shares": 100.0, "value": 1000.0, "holders": 1, "new_holders": None,
                           "exited_holders": None, "top10_pct": 100.0, "hhi": 10000.0,
                           "buy_flow": None, "sell_flow": None, "breadth": 50.0, "breadth_chg": None},
                      Q2: {"shares": 100.0, "value": 1000.0, "holders": 2, "new_holders": 1,
                           "exited_holders": 0, "top10_pct": 100.0, "hhi": 5000.0,
                           "buy_flow": 100.0, "sell_flow": 0.0, "breadth": 50.0, "breadth_chg": 0.0}}}
    payload = report.board_payload(totals, {"AAA": {"name": "Aaa"}}, {})
    cols = payload["cols"]
    assert cols.index("breadth") == cols.index("new_exit") + 1 == cols.index("top10") - 1
    cells = dict(zip(cols, payload["rows"][0][3]))
    assert cells["breadth"] == ["50.00% (+0.00)", 50.0, "zero"]
    html = report.render_sec13f_dashboard(totals, {"AAA": {"name": "Aaa"}}, {})
    assert "<b>Breadth</b>" in html
    assert "filed up to 45 days later" in html
    assert "holders ÷ filers" in html


def test_a_row_without_breadth_renders_an_empty_cell():
    totals = {"AAA": {Q1: {"shares": 100.0, "value": 1000.0, "holders": 1,
                           "top10_pct": 100.0, "hhi": 10000.0}}}
    payload = report.board_payload(totals, {"AAA": {"name": "Aaa"}}, {})
    assert dict(zip(payload["cols"], payload["rows"][0][3]))["breadth"] == ["—", None, "zero"]


def test_the_two_additive_columns_reach_a_table_created_before_them(store):
    """Test 7: the gate's own safety net. A store made before this change has neither column; the first write
    must ALTER them in (``_reconcile_table``), not fail with a Binder Error."""
    totals = _totals()
    rawstore.ensure_history()

    def _columns(conn):
        return {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'sec13f_metrics_history'"
        ).fetchall()}

    with rawstore.connect(db="history") as conn:
        for col in sorted({"breadth", "breadth_chg"} & _columns(conn)):
            conn.execute(f"ALTER TABLE sec13f_metrics_history DROP COLUMN {col}")
        live = _columns(conn)
    assert not {"breadth", "breadth_chg"} & live
    rawstore._TABLE_READY.clear()                       # a fresh process would meet the old table
    assert sec13f_history.record({"totals": totals, "meta": {}, "enrichment": {}},
                                 run_id="r1", run_at=RUN_AT) == {"sec13f_metrics_history": 3}
    with rawstore.connect(db="history") as conn:
        after = _columns(conn)
    assert {"breadth", "breadth_chg"} <= after
    stored = {r["ticker"]: r for r in rawstore.query(
        "SELECT ticker, breadth, breadth_chg FROM sec13f_metrics_history WHERE run_id = 'r1'", db="history")}
    assert stored["BBB"]["breadth"] == pytest.approx(25.0)
    assert stored["BBB"]["breadth_chg"] == pytest.approx(-25.0)
    assert core_history.tables_of("module_sec_13f") == ["sec13f_metrics_history"]
