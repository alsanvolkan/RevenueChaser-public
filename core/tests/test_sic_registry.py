"""SIC registry — cache-first SIC fetch (no live network). GICS is applied by the identity master."""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore, sic_registry as sr


class _Client:
    def __init__(self, payloads):
        self._payloads = dict(payloads)
        self.calls = []
        self.closed = False

    def get(self, url):
        self.calls.append(url)

        class _R:
            status_code = 200
            headers: dict = {}

            def __init__(self, body):
                self.content = body

            def raise_for_status(self):
                pass

        cik = url.rsplit("CIK", 1)[-1].split(".json", 1)[0]
        body = self._payloads.get(cik, {"sic": "", "sicDescription": ""})
        import json as _json
        return _R(_json.dumps(body).encode())

    def close(self):
        self.closed = True


def _seed_universe(tickers=("AAPL", "NVDA", "PRIVATE")):
    rawstore.replace_all("equity_universe", [
        {"ticker": t, "market_cap": 1.0e9, "sector": "", "price": 10.0,
         "name": t + " Inc", "industry": "", "industry_group": "", "sub_industry": "",
         "quote": {"symbol": t}}
        for t in tickers
    ])


def _seed_cik_map(monkeypatch, mapping):
    monkeypatch.setattr(sr.sec_edgar, "_cik_map", lambda: {
        t: {"cik": c, "name": t} for t, c in mapping.items()})


def test_parse_sic():
    row = sr.parse_sic("aapl", "320193", {"sic": "3571", "sicDescription": "Electronic Computers"})
    assert row["ticker"] == "AAPL"
    assert row["cik"] == "320193"
    assert row["sic"] == "3571"
    assert row["sic_description"] == "Electronic Computers"
    assert row["fetched_at"] > 0


def test_fetch_missing_skips_no_cik_and_caches(rawstore_tmp_db, monkeypatch):
    _seed_universe()
    _seed_cik_map(monkeypatch, {"AAPL": "320193", "NVDA": "1045810"})
    client = _Client({
        "0000320193": {"sic": "3571", "sicDescription": "Electronic Computers"},
        "0001045810": {"sic": "3674", "sicDescription": "Semiconductors"},
    })
    res = sr.fetch_missing(client=client)
    assert res["fetched"] == 2 and res["failed"] == 0
    # PRIVATE has no CIK -> never requested
    assert len(client.calls) == 2
    rows = {r["ticker"]: r for r in rawstore.get_all("sec_company_sic")}
    assert rows["AAPL"]["sic"] == "3571" and rows["NVDA"]["sic"] == "3674"


def test_fetch_missing_is_resumable(rawstore_tmp_db, monkeypatch):
    _seed_universe()
    _seed_cik_map(monkeypatch, {"AAPL": "320193", "NVDA": "1045810"})
    rawstore.write_rows("sec_company_sic", [
        {"ticker": "AAPL", "cik": "320193", "sic": "3571",
         "sic_description": "Electronic Computers", "fetched_at": 1.0}])
    client = _Client({"0001045810": {"sic": "3674", "sicDescription": "Semiconductors"}})
    res = sr.fetch_missing(client=client)
    assert res["fetched"] == 1
    assert client.calls == [sr.submissions_url("1045810")]


def _seed_stored_tickers(rows):
    """The stored SEC exchange file (#301 D21): the no-fetch CIK map reads this, not the retired table."""
    rawstore.write_rows("sec_exchange_file",
                        [{"ticker": t, "cik": c, "name": t + " Inc", "exchange": "Nasdaq", "valid_from": None,
                          "status": "present", "source": "sec_exchange", "fetched_at": 1.0, "run_id": "seed",
                          "run_at": 1.0} for t, c in rows.items()])


@pytest.fixture
def offline(monkeypatch):
    """Every way ``fetch_missing`` could reach SEC raises: the CIK-map fetch, JSON, httpx."""
    def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("no_fetch must not touch the network")

    monkeypatch.setattr(sr.sec_edgar, "_cik_map", _boom)
    monkeypatch.setattr(sr.sec_edgar, "get_json", _boom)
    monkeypatch.setattr(sr.sec_edgar, "get_bytes", _boom)
    monkeypatch.setattr(sr.httpx, "Client", _boom)
    monkeypatch.setattr(sr.httpx, "get", _boom)


def test_no_fetch_makes_no_request(rawstore_tmp_db, offline):
    _seed_universe()
    _seed_stored_tickers({"AAPL": "320193"})
    res = sr.fetch_missing(no_fetch=True)
    assert res["no_fetch"] is True
    assert res["fetched"] == 0 and res["pending"] == 1


def test_no_fetch_reads_pending_from_the_stored_ticker_map(rawstore_tmp_db, offline):
    """AAPL and NVDA have a CIK; NVDA already has a SIC; PRIVATE has no CIK: only AAPL is pending."""
    _seed_universe()
    _seed_stored_tickers({"AAPL": "320193", "NVDA": "1045810"})
    rawstore.write_rows("sec_company_sic", [
        {"ticker": "NVDA", "cik": "1045810", "sic": "3674",
         "sic_description": "Semiconductors", "fetched_at": 1.0}])
    res = sr.fetch_missing(no_fetch=True)
    assert (res["cached"], res["pending"], res["fetched"]) == (1, 1, 0)


def test_no_fetch_with_no_stored_ticker_map_does_not_download_it(rawstore_tmp_db, offline):
    """Before: an empty map made ``_cik_map()`` download SEC's whole ticker list during a no-fetch check."""
    _seed_universe()
    res = sr.fetch_missing(no_fetch=True)
    assert res["pending"] == 0 and res["fetched"] == 0
    assert res["cik_map_loaded"] is False                # pending 0 here means "unknown", not "none"


def test_no_fetch_does_not_write_the_store(rawstore_tmp_db, offline):
    from RevenueChaser.core import rawstore as rs
    _seed_universe()
    _seed_stored_tickers({"AAPL": "320193"})
    before = (rs.count("sec_company_sic"), rs.count("sec_exchange_file"), rs.count("sec_company_tickers"))
    sr.fetch_missing(no_fetch=True)
    assert (rs.count("sec_company_sic"), rs.count("sec_exchange_file"),
            rs.count("sec_company_tickers")) == before


# --- P19: rows are flushed as the run goes, so an interrupt keeps what was fetched ----

def _many(n):
    tickers = [f"T{i:04d}" for i in range(n)]
    return tickers, {t: str(1000 + i) for i, t in enumerate(tickers)}


def _fake_get_json(monkeypatch, action=None):
    """Replace ``sec_edgar.get_json``; ``action(call_no)`` may raise or inspect the store."""
    calls = []

    def _get(url, client=None):
        calls.append(url)
        if action:
            action(len(calls))
        return {"sic": "3571", "sicDescription": "Electronic Computers"}

    monkeypatch.setattr(sr.sec_edgar, "get_json", _get)
    return calls


def test_rows_are_flushed_every_200_while_the_run_is_going(rawstore_tmp_db, monkeypatch):
    tickers, ciks = _many(450)
    _seed_cik_map(monkeypatch, ciks)
    stored_at = {}

    def _look(n):
        if n in (150, 201, 401, 450):
            stored_at[n] = rawstore.count("sec_company_sic")

    _fake_get_json(monkeypatch, _look)
    res = sr.fetch_missing(tickers=tickers, client=object())
    # call n looks at the store *before* its own row exists: 0 until 200 rows are in, then 200, then 400
    assert stored_at == {150: 0, 201: 200, 401: 400, 450: 400}
    assert res["fetched"] == 450 and rawstore.count("sec_company_sic") == 450


def test_an_interrupt_keeps_every_row_fetched_so_far(rawstore_tmp_db, monkeypatch):
    tickers, ciks = _many(450)
    _seed_cik_map(monkeypatch, ciks)

    def _interrupt(n):
        if n == 321:
            raise KeyboardInterrupt

    _fake_get_json(monkeypatch, _interrupt)
    with pytest.raises(KeyboardInterrupt):
        sr.fetch_missing(tickers=tickers, client=object())
    assert rawstore.count("sec_company_sic") == 320       # 200 flushed in the loop + 120 in the finally


def test_the_interrupted_run_resumes_where_it_stopped(rawstore_tmp_db, monkeypatch):
    tickers, ciks = _many(250)
    _seed_cik_map(monkeypatch, ciks)

    def _interrupt(n):
        if n == 101:
            raise KeyboardInterrupt

    calls = _fake_get_json(monkeypatch, _interrupt)
    with pytest.raises(KeyboardInterrupt):
        sr.fetch_missing(tickers=tickers, client=object())
    assert rawstore.count("sec_company_sic") == 100
    calls.clear()
    monkeypatch.setattr(sr.sec_edgar, "get_json", lambda url, client=None: (
        calls.append(url) or {"sic": "3571", "sicDescription": "x"}))
    res = sr.fetch_missing(tickers=tickers, client=object())
    assert res["fetched"] == 150 and len(calls) == 150    # only the 150 not yet stored
    assert rawstore.count("sec_company_sic") == 250


def test_fetched_counts_what_was_written_and_failures_are_not_written(rawstore_tmp_db, monkeypatch):
    tickers, ciks = _many(30)
    _seed_cik_map(monkeypatch, ciks)

    def _flaky(n):
        if n % 10 == 0:
            raise RuntimeError("503 from SEC")

    _fake_get_json(monkeypatch, _flaky)
    res = sr.fetch_missing(tickers=tickers, client=object())
    assert (res["fetched"], res["failed"]) == (27, 3)
    assert rawstore.count("sec_company_sic") == 27


def test_the_client_is_closed_after_an_interrupt(rawstore_tmp_db, monkeypatch):
    tickers, ciks = _many(5)
    _seed_cik_map(monkeypatch, ciks)
    closed = []

    class _Own:
        def close(self):
            closed.append(True)

    monkeypatch.setattr(sr.httpx, "Client", lambda **k: _Own())
    _fake_get_json(monkeypatch, lambda n: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        sr.fetch_missing(tickers=tickers)
    assert closed == [True]


def test_the_sic_backfill_is_gone(rawstore_tmp_db):
    """R3 (#86): ``security_master`` is the single writer of GICS in equity_universe, so the SIC
    backfill (and the ``_RANGES`` rules it carried) no longer exists."""
    assert not hasattr(sr, "backfill_universe_gics")
    with pytest.raises(SystemExit):
        sr.main(["--backfill"])                   # the CLI flag went with it


def test_fetching_sic_never_writes_equity_universe(rawstore_tmp_db, monkeypatch):
    """The SIC cache feeds the identity master (step 4); it must not touch the universe's GICS columns."""
    _seed_universe()
    before = rawstore.query("SELECT * FROM equity_universe ORDER BY ticker")
    _seed_cik_map(monkeypatch, {"AAPL": "320193"})
    client = _Client({"0000320193": {"sic": "3571", "sicDescription": "Electronic Computers"}})
    assert sr.fetch_missing(client=client)["fetched"] == 1
    assert rawstore.query("SELECT * FROM equity_universe ORDER BY ticker") == before


def test_quote_obj_handles_double_encoding():
    assert sr._quote_obj('{"a": 1}') == {"a": 1}
    assert sr._quote_obj('"{\\"a\\": 1}"') == {"a": 1}
    assert sr._quote_obj(None) == {}
    assert sr._quote_obj("not json") == {}

