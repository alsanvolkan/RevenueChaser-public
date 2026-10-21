"""#300 (D20): a vendor capture made with ``ownership=True`` (the universe / AG Fetch) also stores the ownership
fields of each fresh yfinance ``.info`` in ``ownership_snapshot``, under the same rule B as the valuation (#297).

A default capture (Earnings, Financials) stores none: D20's yfinance writer is the universe job only
(``phase2/06_interface.md`` §3 D20)."""
from __future__ import annotations

from datetime import date, datetime, timezone

from RevenueChaser.core import ledger, ownership, rawstore, vendor_data

UTC = timezone.utc
COMPLETE = datetime(2026, 10, 8, 20, 35, tzinfo=UTC).timestamp()      # Thu 16:35 EDT: session 10-08, complete
INTRADAY = datetime(2026, 10, 8, 15, 0, tzinfo=UTC).timestamp()       # Thu 11:00 EDT: intraday

_INFO = {"sharesOutstanding": 1.5e9, "heldPercentInsiders": 0.05, "heldPercentInstitutions": 0.7,
         "enterpriseValue": 5.0e9, "currency": "USD"}


def _owned():
    return rawstore.query("SELECT * FROM ownership_snapshot ORDER BY ticker")


def test_an_ownership_capture_stores_the_info_fields_with_the_run(rawstore_tmp_db):
    cap = vendor_data.Capture(run_at=COMPLETE, ownership=True)
    cap.add_yfinance("aaa", {"ticker": "AAA", "info": _INFO, "annual": [], "info_only": True})
    cap.add_yfinance("BBB", {"ticker": "BBB", "info": {"currency": "USD"}, "annual": []})   # no ownership field
    res = cap.flush(run_id="job-ag")
    assert res["valuations_written"] == 2 and res["ownership_written"] == 1
    (row,) = _owned()
    assert (row["ticker"], row["source"], row["session_date"], row["run_id"], row["run_at"]) == \
        ("AAA", "yfinance", date(2026, 10, 8), "job-ag", COMPLETE)
    assert (row["shares_outstanding"], row["insider_pct"], row["institutional_pct"]) == (1.5e9, 0.05, 0.7)


def test_a_default_capture_stores_no_ownership(rawstore_tmp_db):
    cap = vendor_data.Capture(run_at=COMPLETE)
    cap.add_yfinance("AAA", {"ticker": "AAA", "info": _INFO, "annual": []})
    cap.add_valuation("PEER", _INFO)
    res = cap.flush(run_id="job-q")
    assert res["valuations_written"] == 2 and "ownership_written" not in res
    assert _owned() == []


def test_a_peer_valuation_is_not_an_ownership_row(rawstore_tmp_db):
    cap = vendor_data.Capture(run_at=COMPLETE, ownership=True)
    cap.add_valuation("PEER", _INFO)
    cap.flush(run_id="j")
    assert _owned() == []


def test_a_stale_payload_stores_no_ownership(rawstore_tmp_db):
    cap = vendor_data.Capture(run_at=COMPLETE, ownership=True)
    cap.add_yfinance("AAA", {"ticker": "AAA", "info": _INFO, "annual": [], "_stale": True})
    assert cap.flush(run_id="j") == vendor_data.EMPTY_RESULT
    assert _owned() == []


def test_an_intraday_capture_stores_no_ownership(rawstore_tmp_db):
    cap = vendor_data.Capture(run_at=INTRADAY, ownership=True)
    cap.add_yfinance("AAA", {"ticker": "AAA", "info": _INFO, "annual": []})
    res = cap.flush(run_id="j")
    assert res["ownership_written"] == 0 and _owned() == []


def test_an_ownership_store_failure_is_one_note_and_keeps_the_valuation(rawstore_tmp_db, monkeypatch):
    ledger.take_notes()

    def boom(*a, **k):
        raise rawstore.RawStoreError("locked")

    monkeypatch.setattr(ownership, "record", boom)
    cap = vendor_data.Capture(run_at=COMPLETE, ownership=True)
    for t in ("AAA", "BBB"):
        cap.add_yfinance(t, {"ticker": t, "info": _INFO, "annual": []})
        res = cap.flush(run_id="j", label="Asymmetric Growth")
        assert res["ownership_error"] == "RawStoreError: locked" and res["valuations_written"] == 1
    notes = [n for n in ledger.take_notes() if "ownership" in n]
    assert len(notes) == 1 and "Asymmetric Growth ownership snapshot was not stored" in notes[0]
    assert len(rawstore.query("SELECT * FROM vendor_valuation")) == 2
