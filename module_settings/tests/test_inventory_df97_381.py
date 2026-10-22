"""Settings / Data health: the corpus row's "holes" stops counting recorded misses (#381, DF97).

A ticker whose CIK has no usable annual filing is recorded in ``chain_meta`` as ``miss:<TICKER>``
(``module_value_chain.engine``), so it has no ``chain_filings`` row on purpose. The row's note
already reports those names; the hole count must not count them a second time. Store reads only.
"""
from __future__ import annotations

import json

from RevenueChaser.core import rawstore
from RevenueChaser.module_settings import inventory

CHECKED = "2026-10-20T00:00:00+00:00"


def _rows(payload=None) -> dict:
    return {r["key"]: r for r in (payload or inventory.collect())["rows"]}


def _universe(*tickers: str) -> None:
    rawstore.write_rows("equity_universe",
                        [{"ticker": t, "market_cap": 1e9, "name": t} for t in tickers])


def _filing(ticker: str, cik: str = "1") -> dict:
    return {"ticker": ticker, "cik": cik, "form": "10-K", "filed": "2026-01-15"}


def _miss(ticker: str, reason: str = "no_filing", cik: str = "2", checked: str = CHECKED) -> dict:
    return {"key": f"miss:{ticker}", "value": json.dumps(
        {"reason": reason, "note": "no 10-K / 20-F", "cik": cik, "checked": checked})}


def test_a_recorded_miss_is_not_a_hole(rawstore_tmp_db):
    """1 filing, 1 recorded miss, 1 never settled -> one hole, and the tooltip states the rule."""
    _universe("AAA", "BBB", "CCC")
    rawstore.write_rows("chain_filings", [_filing("AAA")])
    rawstore.write_rows("chain_meta", [_miss("BBB")])
    row = _rows()["corpus"]
    assert row["holes"] == 1  # CCC; BBB is settled, not missing
    assert row["hole_label"] == "no filing, excluding recorded misses"


def test_a_miss_key_outside_the_universe_changes_nothing(rawstore_tmp_db):
    """A ``miss:`` key survives a universe exit (``corpus_miss_summary``'s docstring), so it is not a hole."""
    _universe("AAA", "BBB")
    rawstore.write_rows("chain_filings", [_filing("AAA")])
    rawstore.write_rows("chain_meta", [_miss("ZZZ")])
    row = _rows()["corpus"]
    assert row["holes"] == 1  # BBB, the only unsettled universe name
    assert "recorded misses" not in row["note"]  # ZZZ is not a universe name, so the note says nothing


def test_a_lower_case_miss_key_still_excludes_its_ticker(rawstore_tmp_db):
    """The exclusion compares upper-cased, the way ``corpus_miss_summary`` looks a miss up."""
    _universe("AAA", "BBB")
    rawstore.write_rows("chain_filings", [_filing("AAA")])
    rawstore.write_rows("chain_meta", [_miss("bbb")])
    assert _rows()["corpus"]["holes"] == 0
    # the other direction: a key as stored (upper) against a ticker written lower-case
    assert inventory._missing(["bbb"], "chain_filings", exclude={"BBB"}) == 0


def test_missing_without_exclude_is_unchanged(rawstore_tmp_db):
    """The six other ``_missing`` callers keep counting exactly what they counted before."""
    _universe("AAA", "BBB", "CCC")
    rawstore.write_rows("swing_ohlc", [{"ticker": "AAA", "ts": "2026-02-01", "close": 1.0}])
    rawstore.write_rows("chain_meta", [_miss("BBB")])
    assert inventory._missing(["AAA", "BBB", "CCC"], "swing_ohlc") == 2
    assert inventory._missing(["AAA", "BBB", "CCC"], "swing_ohlc", exclude={"BBB"}) == 1
    assert _rows()["ohlc"]["holes"] == 2  # the ohlc row is not the corpus row: BBB still counts


def test_an_unreadable_store_is_unknown_not_zero(rawstore_tmp_db, monkeypatch):
    """``_missing``'s existing ``except`` path: a failed read is ``None``, never a clean 0."""
    _universe("AAA", "BBB")
    rawstore.write_rows("swing_ohlc", [{"ticker": "AAA", "ts": "2026-02-01", "close": 1.0}])
    real = rawstore.query

    def boom(sql, *args, **kwargs):
        if "DISTINCT ticker FROM swing_ohlc" in str(sql):
            raise RuntimeError("disk")
        return real(sql, *args, **kwargs)

    monkeypatch.setattr(rawstore, "query", boom)
    assert inventory._missing(["AAA", "BBB"], "swing_ohlc") is None
    payload = inventory.collect()  # must not raise
    assert _rows(payload)["ohlc"]["holes"] is None
    assert any(e.get("table") == "swing_ohlc" and e.get("where") == "_missing"
               for e in payload["errors"])


def test_the_row_and_its_note_describe_the_same_set(rawstore_tmp_db):
    """DF97: the number and the note under it must not contradict each other."""
    _universe("AAA", "BBB", "CCC", "DDD")
    rawstore.write_rows("chain_filings", [_filing("AAA")])
    rawstore.write_rows("chain_meta", [_miss("BBB"), _miss("CCC", reason="form_40f")])
    row = _rows()["corpus"]
    assert row["holes"] == 1  # DDD, and only DDD
    assert "2 recorded misses (1 no 10-K/20-F, 1 40-F;" in row["note"]
    assert row["holes"] + 2 == 3  # every universe name with no chain_filings row, counted once


def test_a_failed_miss_read_keeps_the_old_count_and_the_old_label(rawstore_tmp_db, monkeypatch):
    """``_recorded_misses`` failing leaves the hole count as it was and the tooltip must not claim an exclusion."""
    _universe("AAA", "BBB")
    rawstore.write_rows("chain_filings", [_filing("AAA")])
    rawstore.write_rows("chain_meta", [_miss("BBB")])

    from RevenueChaser.module_value_chain import engine as vc_engine

    def boom():
        raise RuntimeError("disk")

    monkeypatch.setattr(vc_engine, "stored_misses", boom)
    assert inventory._recorded_misses() is None
    payload = inventory.collect()  # must not raise
    row = _rows(payload)["corpus"]
    assert row["holes"] == 1                      # BBB is counted again: the exclusion could not be read
    assert row["hole_label"] == "no filing"       # and the label does not say it was excluded
    assert any(e.get("table") == "chain_meta" and e.get("where") == "_recorded_misses"
               for e in payload["errors"])
