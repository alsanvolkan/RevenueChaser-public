"""#116 C26 + 40-F + filing choice: misses are recorded once in ``chain_meta``.

A CIK with no usable annual filing (none, or a 40-F) gets a ``miss:<TICKER>`` key
and is not requested again until its CIK changes or ``config.MISS_RETRY_DAYS``
pass. A missing CIK costs no request: it is counted, never stored. A network
error is never recorded as a miss. No network.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_value_chain import config, edgar_text, engine, sync


def _subs(*filings):
    """``filings``: (form, filed, accession) tuples."""
    return {"filings": {"recent": {
        "form": [f[0] for f in filings], "filingDate": [f[1] for f in filings],
        "accessionNumber": [f[2] for f in filings],
        "primaryDocument": ["doc.htm"] * len(filings)}}}


def _miss(ticker):
    row = rawstore.get("chain_meta", "miss:" + ticker)
    if row is None:
        return None
    v = row["value"]
    return json.loads(v) if isinstance(v, str) else v


def _no_download(*a, **k):  # pragma: no cover - must not run
    raise AssertionError("no document download expected")


# ── pick_filing ────────────────────────────────────────────────────────────
def test_pick_filing_prefers_the_newest_annual_form():
    f = edgar_text.pick_filing(_subs(("20-F", "2026-03-01", "a20"), ("10-K", "2019-03-01", "a10")))
    assert f["form"] == "20-F" and f["accession"] == "a20"


def test_pick_filing_amendment_never_outranks_the_10k():
    f = edgar_text.pick_filing(_subs(("10-K/A", "2026-04-01", "amd"), ("10-K", "2026-02-01", "k")))
    assert f["accession"] == "k"


def test_pick_filing_same_day_tie_goes_to_10k():
    f = edgar_text.pick_filing(_subs(("20-F", "2026-03-01", "a20"), ("10-K", "2026-03-01", "a10")))
    assert f["form"] == "10-K"


def test_pick_filing_recognises_a_newer_40f():
    f = edgar_text.pick_filing(_subs(("20-F", "2026-03-01", "a20"), ("10-K", "2019-03-01", "a10"),
                                     ("40-F", "2026-03-02", "a40")))
    assert f["form"] == "40-F"


# ── Fetch path (fetch_corpus) ──────────────────────────────────────────────
@pytest.fixture
def fetch_env(rawstore_tmp_db, monkeypatch):
    env = {"cmap": {"AAA": {"cik": "1"}}, "subs": {}, "calls": [], "error": set()}
    monkeypatch.setattr(engine.sec_edgar, "_cik_map", lambda: env["cmap"])

    def _get_json(url, client=None):
        env["calls"].append(url)
        cik = int(url.rsplit("CIK", 1)[1].split(".")[0])
        if cik in env["error"]:
            raise RuntimeError("503")
        return env["subs"].get(cik, _subs())

    monkeypatch.setattr(edgar_text.sec_edgar, "get_json", _get_json)
    monkeypatch.setattr(edgar_text.sec_edgar, "get_text", _no_download)
    return env


def test_missing_cik_is_counted_not_stored(fetch_env):
    out = engine.fetch_corpus(tickers=["ZZZ"])
    assert out["no_cik"] == 1 and out["failed"] == 0 and out["pending"] == 0
    assert fetch_env["calls"] == [] and _miss("ZZZ") is None


def test_missing_cik_is_retried_when_the_cik_appears(fetch_env):
    engine.fetch_corpus(tickers=["ZZZ"])
    fetch_env["cmap"]["ZZZ"] = {"cik": "77"}
    engine.fetch_corpus(tickers=["ZZZ"])
    assert len(fetch_env["calls"]) == 1 and "CIK0000000077" in fetch_env["calls"][0]
    assert _miss("ZZZ")["reason"] == "no_filing"


def test_fetch_filing_returns_a_reason(fetch_env):
    assert edgar_text.fetch_filing("AAA", "1")["miss"] == "no_filing"
    fetch_env["error"].add(1)
    res = edgar_text.fetch_filing("AAA", "1")
    assert res is not None and "error" in res and "miss" not in res


def test_no_filing_is_recorded_then_not_requested_again(fetch_env):
    out = engine.fetch_corpus(tickers=["AAA"])
    assert out["no_filing"] == 1 and out["failed"] == 0 and len(fetch_env["calls"]) == 1
    assert _miss("AAA")["reason"] == "no_filing" and _miss("AAA")["cik"] == "1"
    engine.fetch_corpus(tickers=["AAA"])
    assert len(fetch_env["calls"]) == 1, "a recorded miss was requested again"


def test_a_miss_is_retried_after_the_interval(fetch_env):
    old = (datetime.now(timezone.utc) - timedelta(days=config.MISS_RETRY_DAYS + 1)).isoformat()
    rawstore.write_rows("chain_meta", [{"key": "miss:AAA", "value": json.dumps(
        {"reason": "no_filing", "cik": "1", "checked": old})}])
    engine.fetch_corpus(tickers=["AAA"])
    assert len(fetch_env["calls"]) == 1
    assert _miss("AAA")["checked"] > old


def test_a_miss_is_retried_when_the_cik_changes(fetch_env):
    rawstore.write_rows("chain_meta", [{"key": "miss:AAA", "value": json.dumps(
        {"reason": "no_filing", "cik": "999", "checked": datetime.now(timezone.utc).isoformat()})}])
    engine.fetch_corpus(tickers=["AAA"])
    assert len(fetch_env["calls"]) == 1


def test_a_network_error_is_not_recorded(fetch_env):
    fetch_env["error"].add(1)
    out = engine.fetch_corpus(tickers=["AAA"])
    assert out["failed"] == 1 and _miss("AAA") is None


def test_40f_filer_is_a_named_miss_without_download(fetch_env):
    fetch_env["subs"][1] = _subs(("40-F", "2026-03-02", "a40"))
    out = engine.fetch_corpus(tickers=["AAA"])
    m = _miss("AAA")
    assert m["reason"] == "form_40f" and m["note"] == "40-F, not supported yet"
    assert out["form_40f"] == 1 and rawstore.get("chain_filings", "AAA") is None


def test_refresh_pending_excludes_misses_and_no_cik_with_reads_only(fetch_env):
    _seed_tickers((("AAA", "1"), ("NEW", "3")))
    engine.fetch_corpus(tickers=["AAA"])
    before = rawstore.get_all("chain_meta")
    fetch_env["cmap"] = None  # the Refresh must not call _cik_map (it may hit SEC)
    out = engine.fetch_corpus(tickers=["AAA", "ZZZ", "NEW"], no_fetch=True)
    assert (out["pending"], out["misses"], out["no_cik"]) == (1, 1, 1)
    assert rawstore.get_all("chain_meta") == before


def test_miss_keys_survive_a_fetch_run(fetch_env):
    rawstore.write_rows("chain_filings", [{
        "ticker": "AAA", "cik": "1", "form": "10-K", "filed": "2026-02-01", "accession": "a",
        "primary_doc": "u", "item_text": "We buy from ACME Corp.", "quality": "ok"}])
    fetch_env["subs"][2] = _subs()
    fetch_env["cmap"]["BBB"] = {"cik": "2"}
    engine.fetch_corpus(tickers=["BBB"])
    assert _miss("BBB") is not None
    engine.run(tickers=["AAA", "BBB"])
    assert _miss("BBB") is not None
    assert rawstore.get("chain_meta", "last_sync") is not None


def test_board_payload_pending_excludes_misses_and_no_cik(rawstore_tmp_db, monkeypatch):
    monkeypatch.setattr("RevenueChaser.core.universe.classification_universe",
                        lambda *a, **k: ["AAA", "BBB", "CCC", "DDD"])
    _seed_tickers((("AAA", "1"), ("BBB", "2"), ("CCC", "3")))
    rawstore.write_rows("chain_meta", [{"key": "miss:BBB", "value": json.dumps(
        {"reason": "no_filing", "cik": "2", "checked": datetime.now(timezone.utc).isoformat()})}])
    payload = engine._board_payload([{"ticker": "AAA"}], [], "2026-10-02T00:00:00+00:00", None)
    assert (payload["pending"], payload["misses"], payload["no_cik"]) == (1, 1, 1)
    # D4 (DF58): a Fetch's own count is the to-do list before it ran, so it still holds the names it just
    # wrote. The tile is derived from the store whatever the Fetch says; its count stays for the job message.
    fetch = {"requested": 4, "cached": 0, "pending": 4, "fetched": 1, "no_cik": 0, "misses": 0}
    payload = engine._board_payload([{"ticker": "AAA"}], [], "2026-10-02T00:00:00+00:00", fetch)
    assert (payload["pending"], payload["misses"], payload["no_cik"]) == (1, 1, 1)
    assert payload["fetch"]["pending"] == 4


def test_board_payload_pending_counts_universe_names_with_no_filing(rawstore_tmp_db, monkeypatch):
    """D4: Pending is the universe names with no filing row, less no-CIK names and recorded misses. A stored
    filing of a name now outside the universe (it fell below the $300M floor) is not one less pending name."""
    monkeypatch.setattr("RevenueChaser.core.universe.classification_universe", lambda *a, **k: ["AAA", "BBB"])
    _seed_tickers((("AAA", "1"), ("BBB", "2"), ("OLD", "9")))
    payload = engine._board_payload([{"ticker": "AAA"}, {"ticker": "OLD"}], [], "2026-10-02T00:00:00+00:00", None)
    assert (payload["pending"], payload["misses"], payload["no_cik"]) == (1, 0, 0)


def test_the_pending_tile_after_a_fetch_matches_the_refresh(fetch_env, monkeypatch):
    """D4 (DF58): right after a Fetch the tile left out the names it just wrote only at the next Refresh. A Fetch
    that writes AAA, fails BBB (retried next run) and has no CIK for CCC leaves BBB pending, on both paths."""
    universe = ["AAA", "BBB", "CCC"]
    monkeypatch.setattr("RevenueChaser.core.universe.classification_universe", lambda *a, **k: list(universe))
    fetch_env["cmap"]["BBB"] = {"cik": "2"}
    _seed_tickers((("AAA", "1"), ("BBB", "2")))

    def _fetch_filing(ticker, cik, client=None):
        if ticker == "BBB":
            return {"error": "503"}
        return {"ticker": ticker, "cik": cik, "form": "10-K", "filed": "2026-02-01", "accession": "a-" + ticker,
                "primary_doc": "u", "item_text": "We buy wafers from TSMC, our foundry supplier.", "quality": "ok"}

    monkeypatch.setattr(engine.edgar_text, "fetch_filing", _fetch_filing)
    fetched = engine.run(tickers=universe, run_id="r-d4")
    assert fetched["fetch"]["fetched"] == 1 and fetched["fetch"]["pending"] == 2   # the job message's count
    assert (fetched["pending"], fetched["no_cik"], fetched["misses"]) == (1, 1, 0)  # BBB only
    refreshed = engine.run(no_fetch=True, tickers=universe)
    assert refreshed["pending"] == fetched["pending"]


# ── sync path ──────────────────────────────────────────────────────────────
@pytest.fixture
def sync_env(rawstore_tmp_db, monkeypatch):
    env = {"subs": _subs(), "calls": 0}
    rawstore.replace_all("equity_universe", [{"ticker": t, "market_cap": 1e9, "name": t}
                                             for t in ("NVDA", "NOCIK")])
    _seed_tickers((("NVDA", "1045810"),))

    def _get_json(url, client=None):
        env["calls"] += 1
        return env["subs"]

    monkeypatch.setattr(sync.sec_edgar, "get_json", _get_json)
    monkeypatch.setattr(sync.edgar_text, "fetch_document", _no_download)
    return env


def test_sync_records_no_filing_and_skips_it_next_time(sync_env):
    out = sync.sync_filings()
    assert out["no_filing"] == 1 and sync_env["calls"] == 1
    assert _miss("NVDA")["reason"] == "no_filing"
    first_check = _miss("NVDA")
    out = sync.sync_filings()
    # The daily submissions sweep (#299) asks every universe CIK again, but a recorded
    # miss skips the document step: no download, the miss is not re-recorded.
    assert sync_env["calls"] == 2 and out["known_miss"] == 1 and out["no_filing"] == 0
    assert _miss("NVDA") == first_check


def test_sync_force_retries_a_recorded_miss(sync_env):
    sync.sync_filings()
    sync.sync_filings(force=True)
    assert sync_env["calls"] == 2


def test_sync_counts_a_missing_cik_without_storing_it(sync_env):
    out = sync.sync_filings()
    assert out["no_cik"] == 1 and out["requested"] == 2 and _miss("NOCIK") is None


def test_sync_records_40f_without_download(sync_env):
    sync_env["subs"] = _subs(("40-F", "2026-03-02", "a40"))
    out = sync.sync_filings()
    assert out["form_40f"] == 1 and _miss("NVDA")["reason"] == "form_40f"


def test_sync_network_error_is_failed_not_a_miss(sync_env, monkeypatch):
    def _boom(url, client=None):
        raise ConnectionError("EDGAR down")
    monkeypatch.setattr(sync.sec_edgar, "get_json", _boom)
    out = sync.sync_filings()
    assert out["failed"] == 1 and out["no_filing"] == 0 and _miss("NVDA") is None


def test_a_written_row_clears_the_miss(sync_env, monkeypatch):
    sync.sync_filings()
    assert _miss("NVDA") is not None
    sync_env["subs"] = _subs(("10-K", "2026-02-01", "acc-1"))
    monkeypatch.setattr(sync.edgar_text, "fetch_document", lambda t, cik, f, client=None: (
        "<html></html>", {"ticker": t, "cik": str(cik), "form": f["form"], "filed": f["filed"],
                          "accession": f["accession"], "primary_doc": "u", "item_text": "x",
                          "quality": "ok"}))
    sync.sync_filings(force=True)
    assert rawstore.get("chain_filings", "NVDA")["accession"] == "acc-1"
    assert _miss("NVDA") is None


def test_pending_tile_names_what_it_leaves_out():
    from RevenueChaser.module_value_chain import report
    html = report.render_value_chain_dashboard({
        "as_of": "2026-10-02T00:00:00+00:00", "filings": [], "edges": [],
        "pending": 4, "misses": 2, "no_cik": 1})
    assert ('<div class="v">4</div><div class="l">Pending</div>'
            '<div class="d">not yet fetched · 2 with no 10-K / 20-F · 1 with no CIK</div>') in html


def test_last_sync_text_is_escaped():
    from RevenueChaser.module_value_chain import report
    html = report.render_value_chain_dashboard({
        "as_of": "<b>x", "filings": [], "edges": [], "pending": 0, "misses": 0, "no_cik": 0})
    assert "Last sync: &lt;b&gt;x</div>" in html and "<b>x" not in html


# ── corpus_miss_summary (#194 Settings note) ───────────────────────────────
def _seed_tickers(rows):
    """The stored SEC exchange file (#301 D21): the store-only CIK map every reader now uses.

    ``sec_company_tickers`` is retired and no longer written, so a test that seeds a CIK map seeds its rows."""
    rawstore.write_rows("sec_exchange_file", [
        {"ticker": t, "cik": c, "name": t, "exchange": "Nasdaq", "valid_from": None, "status": "present",
         "source": "sec_exchange", "fetched_at": 1.0, "run_id": "seed", "run_at": 1.0} for t, c in rows])


def _put_miss(ticker, reason, cik, days_ago=0):
    checked = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    rawstore.write_rows("chain_meta", [{"key": "miss:" + ticker, "value": json.dumps(
        {"reason": reason, "cik": cik, "checked": checked})}])


def test_corpus_miss_summary_splits_by_reason_and_counts_due_and_no_cik(rawstore_tmp_db):
    _seed_tickers((("AAA", "1"), ("BBB", "2"), ("CCC", "3"), ("OLD", "4")))
    _put_miss("AAA", "no_filing", "1")
    _put_miss("BBB", "form_40f", "2")
    _put_miss("CCC", "no_filing", "3", days_ago=config.MISS_RETRY_DAYS + 1)  # due
    _put_miss("GONE", "no_filing", "9")  # left the universe: not counted
    out = engine.corpus_miss_summary(["AAA", "BBB", "CCC", "NOCIK"])
    assert out == {"recorded": 3, "no_filing": 2, "form_40f": 1, "due": 1, "no_cik": 1}


def test_corpus_miss_summary_makes_no_network_call_and_reads_no_cik_as_unknown(
        rawstore_tmp_db, monkeypatch):
    from RevenueChaser.core.providers import sec_edgar

    calls = []
    monkeypatch.setattr(sec_edgar, "get_json", lambda *a, **k: calls.append(("get_json", a)))
    monkeypatch.setattr(sec_edgar, "_cik_map", lambda *a, **k: calls.append(("_cik_map", a)) or {})
    out = engine.corpus_miss_summary(["AAA"])
    assert calls == [], "the helper reached the network path"
    assert out["recorded"] == 0 and out["no_cik"] is None, "an empty CIK map is unknown, not all-no-CIK"


def test_corpus_miss_summary_is_none_when_the_store_cannot_be_read(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("store locked")
    monkeypatch.setattr(rawstore, "get_all", boom)
    monkeypatch.setattr(rawstore, "query", boom)
    assert engine.corpus_miss_summary(["AAA"]) is None
