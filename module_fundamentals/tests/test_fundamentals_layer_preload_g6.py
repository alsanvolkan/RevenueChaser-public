"""A Fundamentals Fetch on the layer reads the statements one scan chunk at a time (Opus review S5, G6).

On the fundamentals layer each ``sec_edgar.get_fundamentals`` call opens the served DuckDB file twice, about 57 ms a
name on the 29 Oct 2026 real-data sample against 11 ms a name read in one batch of 15. So a Fetch reads the layer's
records of the names it kept in the scan's own chunks (``screen_common.BARS_CHUNK``), one read per chunk, and keeps
only the current chunk in memory. Off the layer, or on a Refresh, every name reads its own row as before.
"""
from __future__ import annotations

from RevenueChaser.core import fundamentals_layer, screen_common
from RevenueChaser.module_fundamentals import pipeline


def _records(calls):
    def records_many(tickers):
        calls.append(list(tickers))
        return {t: ([{"fiscal_year": "2025", "revenue": 1.0}], [], {"layer": True}) for t in tickers if t != "GONE"}
    return records_many


def test_the_layer_is_read_once_per_scan_chunk(monkeypatch):
    calls = []
    monkeypatch.setattr(fundamentals_layer, "records_many", _records(calls))
    monkeypatch.setattr(screen_common, "BARS_CHUNK", 2)
    chunks = pipeline._LayerChunks(["A", "B", "C", "GONE"])
    assert chunks.get("A")[0] and chunks.get("B")[0]
    assert calls == [["A", "B"]]
    assert chunks.get("C")[0] and calls == [["A", "B"], ["C", "GONE"]]
    assert chunks.get("GONE") == ([], [], fundamentals_layer._unavailable())     # the layer holds no company
    assert chunks.get("NOT-KEPT") is None                                        # read on its own, as before
    assert len(calls) == 2


def _captured_scan(monkeypatch, layer: bool, no_fetch: bool):
    seen = {}
    monkeypatch.setattr(fundamentals_layer, "reads_layer", lambda: layer)
    monkeypatch.setattr(pipeline, "prefilter", lambda rows: screen_common.Prefiltered(kept=rows))
    monkeypatch.setattr(pipeline, "_compute_row", lambda row, spy_df, **kw: seen.setdefault("layer", kw["layer"]))

    def run_screen(**kw):
        kw["prefilter"]([{"ticker": "A"}, {"ticker": "b "}])
        kw["compute_row"]({"ticker": "A"}, None, no_fetch=no_fetch)
        return {}
    monkeypatch.setattr(screen_common, "run_screen", run_screen)
    monkeypatch.setattr(pipeline, "_note_non_usd", lambda *a: None)
    pipeline.run_scan(no_fetch=no_fetch)
    return seen["layer"]


def test_a_fetch_on_the_layer_scores_from_the_chunks(monkeypatch):
    chunks = _captured_scan(monkeypatch, layer=True, no_fetch=False)
    assert isinstance(chunks, pipeline._LayerChunks) and chunks._tickers == ["A", "B"]


def test_off_the_layer_and_on_a_refresh_each_name_reads_its_own_row(monkeypatch):
    assert _captured_scan(monkeypatch, layer=False, no_fetch=False) is None
    assert _captured_scan(monkeypatch, layer=True, no_fetch=True) is None


def test_compute_row_takes_the_chunk_record_and_never_asks_sec_edgar(monkeypatch):
    class Chunks:
        def get(self, ticker):
            return [], [], {"layer": True, "available": False}
    asked = []
    monkeypatch.setattr(pipeline.sec_edgar, "get_fundamentals", lambda *a, **k: asked.append(a) or ([], [], {}))
    monkeypatch.setattr(screen_common, "load_bars", lambda t: _bars())
    monkeypatch.setattr(pipeline, "ohlc_drop_reason", lambda df: None)
    drops = []
    assert pipeline._compute_row({"ticker": "A"}, None, spine={}, drops=drops, layer=Chunks()) is None
    assert drops == ["no_edgar_annual"] and asked == []


def _bars():
    import pandas as pd
    return pd.DataFrame({"close": [1.0]}), True
