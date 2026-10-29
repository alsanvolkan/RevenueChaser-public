"""The prices store's other sources: Alpaca's corporate actions, the SEC cover shares per class, Yahoo's float and
Unusual Whales' ``total_float``, each mocked at its boundary (HTTP; Yahoo at its one lookup function)."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.core.providers import unusual_whales
from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.prices import actions, cover_shares, floats, stage

from .prices_fixture import (ALPHABET, GOOG_ACCESSION, SPINE, aapl_instance, goog_instance, master_index,
                             submissions, write_spine)

TUE = date(2026, 10, 27)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "test-secret")
    monkeypatch.setenv("RC_ALPACA_BASE", "https://alpaca.test")
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")
    monkeypatch.setattr(unusual_whales, "BASE", "https://uw.test/api")
    from RevenueChaser.core.providers import sec_edgar
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec_edgar, "SEC_DATA_BASE", "https://data.sec.test")
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 27, 21, 0, tzinfo=timezone.utc))
    monkeypatch.setattr("RevenueChaser.rcdata.build.prices.ROTATION", ())
    monkeypatch.setattr("RevenueChaser.rcdata.build.prices.BENCHMARKS", ("SPY",))
    monkeypatch.setattr("RevenueChaser.rcdata.read.watched", lambda kind: set())
    write_spine(SPINE + ALPHABET)


def _ctx(dataset, run_id="r1", sleeps=None):
    clock = [0.0]

    def sleep(s):
        (sleeps if sleeps is not None else []).append(s)
        clock[0] += s
    return base.RunContext(run_id=run_id, dataset=dataset, sleep=sleep, clock=lambda: clock[0])


def _query(request) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}


# ── Alpaca corporate actions ─────────────────────────────────────────────────────────────────────────────────

ACTIONS = {
    "reverse_splits": [{"id": "rs-1", "symbol": "FLG", "old_rate": 3, "new_rate": 1, "old_cusip": "649445103",
                        "new_cusip": "649445202", "process_date": "2026-10-22", "ex_date": "2026-10-22",
                        "record_date": None, "payable_date": "2026-10-22", "currency": "USD"}],
    "forward_splits": [{"id": "fs-1", "symbol": "BRK.B", "old_rate": 1, "new_rate": 2, "cusip": "084670702",
                        "process_date": "2026-10-20", "ex_date": "2026-10-20", "record_date": "2026-10-16",
                        "payable_date": "2026-10-19"}],
    "cash_dividends": [{"id": "cd-1", "symbol": "AAPL", "cusip": "037833100", "rate": 0.26, "special": False,
                        "foreign": False, "process_date": "2026-10-24", "ex_date": "2026-10-23",
                        "record_date": "2026-10-23", "payable_date": "2026-11-12", "currency": "USD"},
                       {"id": "cd-0", "symbol": "AAPL", "cusip": "037833100", "rate": 0.25, "special": False,
                        "foreign": False, "process_date": "2025-12-30", "ex_date": "2025-12-29",
                        "currency": "USD"}],
    "name_changes": [{"id": "nc-1", "old_symbol": "NYCB", "new_symbol": "FLG", "old_cusip": "649445103",
                      "new_cusip": "649445202", "process_date": "2026-10-21"}],
}


def test_corporate_actions_one_window_on_the_process_date_typed_and_kept_once(http_mock):
    http_mock.add("/v1/corporate-actions", {"corporate_actions": ACTIONS, "next_page_token": None})
    ctx = _ctx(actions.NAME)
    out = actions.DATASET.to_parquet_all(ctx, actions.DATASET.fetch(ctx, TUE))
    q = _query(http_mock.calls("/v1/corporate-actions")[0])
    assert (q["start"], q["end"], q["limit"]) == ("2026-10-13", "2026-10-27", "1000") and "symbols" not in q
    assert sorted(p.parent.name for p in out) == ["year=2025", "year=2026"]
    rows = {r["action_id"]: r for p in out for r in stage.read_rows(p)}
    assert rows["rs-1"]["action_type"] == "reverse_split" and (rows["rs-1"]["old_rate"], rows["rs-1"]["new_rate"]) \
        == (3.0, 1.0) and rows["rs-1"]["ex_date"] == date(2026, 10, 22) and rows["rs-1"]["symbol"] == "FLG"
    assert rows["cd-1"]["rate"] == 0.26 and rows["cd-1"]["special"] is False and rows["cd-1"]["currency"] == "USD"
    assert rows["nc-1"]["symbol"] == "NYCB" and rows["nc-1"]["new_symbol"] == "FLG" and rows["nc-1"]["ex_date"] is None
    assert json.loads(rows["fs-1"]["payload"])["record_date"] == "2026-10-16"
    actions.DATASET.fetch(ctx, TUE)                                       # the raw file is the resume marker
    assert len(http_mock.calls("/v1/corporate-actions")) == 1


def test_the_actions_backfill_asks_windows_of_its_own(http_mock):
    http_mock.add("/v1/corporate-actions", {"corporate_actions": {}, "next_page_token": None})
    ctx = _ctx(actions.NAME)
    actions.DATASET.backfill(ctx, date(2026, 1, 1), date(2026, 6, 30))
    windows = [(q["start"], q["end"]) for q in map(_query, http_mock.calls("/v1/corporate-actions"))]
    assert windows == [("2026-01-01", "2026-03-31"), ("2026-04-01", "2026-06-29"), ("2026-06-30", "2026-06-30")]
    actions.DATASET.backfill(ctx, date(2026, 1, 1), date(2026, 6, 30))
    assert len(http_mock.calls("/v1/corporate-actions")) == 3


# ── SEC cover shares ─────────────────────────────────────────────────────────────────────────────────────────

GOOG_URL = "/Archives/edgar/data/1652044/000165204426000071/goog-20260630_htm.xml"
AAPL_URL = "/Archives/edgar/data/320193/000032019326000090/aapl-20260926_htm.xml"


def _sec_routes(http_mock):
    http_mock.add("daily-index/2026/QTR4/master.20261026.idx", master_index([
        ("1652044", "Alphabet Inc.", "10-Q", "20261026", f"edgar/data/1652044/{GOOG_ACCESSION}.txt"),
        ("320193", "Apple Inc.", "8-K", "20261026", "edgar/data/320193/0000320193-26-000100.txt"),
        ("999999", "NOT OURS INC", "10-Q", "20261026", "edgar/data/999999/0000999999-26-000001.txt")]))
    http_mock.add("submissions/CIK0001652044.json", submissions("1652044", [
        (GOOG_ACCESSION, "10-Q", "2026-10-26", "2026-06-30", "goog-20260630.htm", 1)]))
    http_mock.add("submissions/CIK0000320193.json", submissions("320193", [
        ("0000320193-26-000100", "8-K", "2026-10-26", "", "aapl-8k.htm", 0),
        ("0000320193-26-000090", "10-K", "2026-10-24", "2026-09-26", "aapl-20260926.htm", 1)]))
    http_mock.add("submissions/CIK0000910073.json", submissions("910073", [
        ("0000910073-26-000010", "10-Q", "2026-08-05", "2026-06-30", "flg-10q.htm", 0)]))
    http_mock.add(GOOG_URL, goog_instance())
    http_mock.add(AAPL_URL, aapl_instance())


def test_cover_shares_from_the_daily_index_and_the_fill(http_mock, monkeypatch):
    monkeypatch.setattr(cover_shares, "FILL_PER_RUN", 2)
    _sec_routes(http_mock)
    ctx = _ctx(cover_shares.NAME)
    raws = cover_shares.DATASET.fetch(ctx, TUE)
    assert [p.name for p in raws] == ["0000320193-26-000090.json.gz", f"{GOOG_ACCESSION}.json.gz"]
    asked = [urlparse(str(r.url)).path for r in http_mock.requests]
    assert sum("master." in a for a in asked) == 5                        # the 5 weekdays before as_of
    assert "submissions/CIK0000999999.json" not in " ".join(asked)        # not a spine CIK
    assert "submissions/CIK0001067983.json" not in " ".join(asked)        # over the fill's 2 a run
    assert GOOG_URL in asked and AAPL_URL in asked and not any("flg-10q" in a for a in asked)
    rows = {(r["cik"], r["class_member"]): r for p in cover_shares.DATASET.to_parquet_all(ctx, raws)
            for r in stage.read_rows(p)}
    assert rows[("1652044", "us-gaap:CommonClassAMember")]["symbol"] == "GOOGL"
    assert rows[("1652044", "goog:CapitalClassCMember")]["shares"] == 5_450_000_000
    b = rows[("1652044", "us-gaap:CommonClassBMember")]
    assert b["symbol"] is None and b["as_of"] == date(2026, 7, 17) and b["form"] == "10-Q"
    assert b["filed"] == date(2026, 10, 26) and b["period_end"] == date(2026, 6, 30)
    assert rows[("320193", "")]["symbol"] == "AAPL" and rows[("320193", "")]["shares"] == 14_800_000_000
    assert len([k for k in rows if k[0] == "1652044"]) == 3               # the co-registrant's fact is not Alphabet's
    assert not any(k[0] == "910073" for k in rows)                        # no inline XBRL: a marker, no rows
    assert any(i["kind"] == "no_inline_xbrl" for i in ops.open_issues(cover_shares.NAME)[1])


def test_a_stored_filing_is_never_asked_again(http_mock, monkeypatch):
    monkeypatch.setattr(cover_shares, "FILL_PER_RUN", 2)
    _sec_routes(http_mock)
    cover_shares.DATASET.fetch(_ctx(cover_shares.NAME), TUE)
    http_mock.requests.clear()
    cover_shares.DATASET.fetch(_ctx(cover_shares.NAME, "r2"), TUE)
    asked = [urlparse(str(r.url)).path for r in http_mock.requests]
    assert not any(a.endswith("_htm.xml") for a in asked)
    assert not any("master." in a for a in asked)                          # the index files are kept too
    assert "submissions/CIK0001067983.json" in " ".join(asked)            # the fill moves on


def test_sec_requests_are_spaced_under_five_a_second(http_mock, monkeypatch):
    monkeypatch.setattr(cover_shares, "FILL_PER_RUN", 2)
    monkeypatch.setattr("RevenueChaser.core.sec_throttle.pace", lambda: True)
    _sec_routes(http_mock)
    sleeps = []
    cover_shares.DATASET.fetch(_ctx(cover_shares.NAME, sleeps=sleeps), TUE)
    n = len(http_mock.requests)
    assert n > 5 and len(sleeps) == n - 1 and min(sleeps) >= 0.2           # under 5 a second, every gap


def test_cover_facts_of_an_instance():
    facts = cover_shares.cover_facts(goog_instance())
    assert facts["document_type"] == "10-Q" and facts["period_end"] == "2026-06-30"
    assert [(f["class_member"], f["symbol"], f["shares"], f["as_of"]) for f in facts["shares"]] == [
        ("us-gaap:CommonClassAMember", "GOOGL", 5_800_000_000.0, "2026-07-17"),
        ("us-gaap:CommonClassBMember", None, 860_000_000.0, "2026-07-17"),
        ("goog:CapitalClassCMember", "GOOG", 5_450_000_000.0, "2026-07-17")]


# ── Yahoo float ──────────────────────────────────────────────────────────────────────────────────────────────

class YahooStop(Exception):
    pass


def test_yahoo_float_paced_one_and_a_half_seconds_and_stopped_on_429(monkeypatch):
    asked = []

    def lookup(ticker):
        asked.append(ticker)
        if ticker == "FLG":
            raise YahooStop("Too Many Requests. Rate limited. HTTP 429")
        return {"floatShares": 1000.0 * len(asked), "sharesOutstanding": 2000.0, "quoteType": "EQUITY",
                "currency": "USD"}
    monkeypatch.setattr(floats, "_lookup", lookup)
    sleeps = []
    ctx = _ctx(floats.YAHOO, sleeps=sleeps)
    out = floats.YAHOO_FLOAT.to_parquet_all(ctx, floats.YAHOO_FLOAT.fetch(ctx, TUE))
    assert asked == ["AAPL", "BRK-B", "FLG"]                              # stopped at the 429: GAPX never asked
    assert sleeps and min(sleeps) >= 1.5 - 1e-9 and len(sleeps) == 2
    rows = {r["ticker"]: r for p in out for r in stage.read_rows(p)}
    assert set(rows) == {"AAPL", "BRK-B"} and rows["AAPL"]["float_shares"] == 1000.0
    assert rows["BRK-B"]["security_id"] == "072726:02" and rows["AAPL"]["source_as_of"] == TUE
    assert any("429" in n for n in ctx.notes) and ctx.calls["yfinance"][2] == 1


def test_yahoo_float_asks_the_never_asked_first(monkeypatch):
    monkeypatch.setattr(floats, "_lookup", lambda t: {"floatShares": 5.0})
    monkeypatch.setattr(floats, "SLICE", 2)
    ctx = _ctx(floats.YAHOO)
    floats.YAHOO_FLOAT.to_parquet_all(ctx, floats.YAHOO_FLOAT.fetch(ctx, date(2026, 10, 26)))
    asked = []
    monkeypatch.setattr(floats, "_lookup", lambda t: asked.append(t) or {"floatShares": 6.0})
    ctx2 = _ctx(floats.YAHOO, "r2")
    floats.YAHOO_FLOAT.to_parquet_all(ctx2, floats.YAHOO_FLOAT.fetch(ctx2, TUE))
    assert asked == ["FLG", "GAPX"]                                        # by ticker among the never asked


class FakeYahoo:
    """``yfinance`` as it answered this cloud host on 28 Oct 2026 (1.7.0): a refused lookup is no exception; the
    library logs the 401 on its ``yfinance`` logger and ``get_info`` returns ``{'trailingPegRatio': None}``."""

    def __init__(self, refuse=(), empty=()):
        self.refuse, self.empty, self.asked = set(refuse), set(empty), []

    def Ticker(self, ticker):
        import logging
        fake = self

        class _T:
            def get_info(self):
                fake.asked.append(ticker)
                if ticker in fake.refuse:
                    log = logging.getLogger("yfinance")
                    log.warning("Crumb fetch rate-limited (HTTP 429), continuing without crumb")
                    log.error('HTTP Error 401: Unauthorized{"finance":{"result":null,"error":{"code":"Unauthorized",'
                              '"description":"Invalid Crumb"}}}')
                    return {"trailingPegRatio": None}
                if ticker in fake.empty:
                    return {"trailingPegRatio": None}
                return {"floatShares": 7.0, "sharesOutstanding": 9.0, "quoteType": "EQUITY", "currency": "USD"}
        return _T()


def test_a_401_yahoo_only_logs_stops_the_run_and_an_empty_answer_is_an_error_row(monkeypatch):
    import sys
    fake = FakeYahoo(refuse={"FLG"}, empty={"BRK-B"})
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    ctx = _ctx(floats.YAHOO)
    out = floats.YAHOO_FLOAT.to_parquet_all(ctx, floats.YAHOO_FLOAT.fetch(ctx, TUE))
    assert fake.asked == ["AAPL", "BRK-B", "FLG"]                         # stopped at the 401: GAPX never asked
    rows = {r["ticker"]: r for p in out for r in stage.read_rows(p)}
    assert rows["AAPL"]["float_shares"] == 7.0 and rows["AAPL"]["error"] is None
    assert rows["BRK-B"]["float_shares"] is None and rows["BRK-B"]["error"] == floats.EMPTY_ANSWER
    assert "FLG" not in rows and any("yfinance stopped at FLG" in n and "401" in n for n in ctx.notes)
    assert ctx.calls["yfinance"][:2] == [3, 2]                            # three asked, two failed


# ── Unusual Whales total_float ───────────────────────────────────────────────────────────────────────────────

def test_uw_total_float_one_request_per_hundred(http_mock):
    http_mock.add("/short_screener", {"data": [{"symbol": "AAPL", "total_float": "14700000000"},
                                               {"symbol": "BRK.B", "total_float": 1300000000},
                                               {"symbol": "NOTASKED", "total_float": 5}]})
    ctx = _ctx(floats.UW)
    out = floats.UW_FLOAT.to_parquet_all(ctx, floats.UW_FLOAT.fetch(ctx, TUE))
    q = _query(http_mock.calls("/short_screener")[0])
    assert sorted(q["tickers"].split(",")) == ["AAPL", "BRK-B", "FLG", "GAPX", "GOOG", "GOOGL"]
    rows = {r["ticker"]: r for p in out for r in stage.read_rows(p)}
    assert rows["AAPL"]["total_float"] == 14_700_000_000.0 and rows["BRK-B"]["total_float"] == 1_300_000_000.0
    assert set(rows) == {"AAPL", "BRK-B"} and ctx.calls["uw"][0] == 1


def test_uw_total_float_failed_answer_stores_nothing(http_mock, monkeypatch):
    monkeypatch.setattr(unusual_whales, "_sleep", lambda s: None)
    http_mock.add("/short_screener", b"down", status=500)
    ctx = _ctx(floats.UW)
    with pytest.raises(base.SourceError):
        floats.UW_FLOAT.fetch(ctx, TUE)
    assert not floats.UW_FLOAT.raw_path(TUE).exists()


# ── Each share and float source through the builder ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [floats.YAHOO, floats.UW, cover_shares.NAME])
def test_each_share_and_float_source_runs_through_the_builder_to_its_check(http_mock, monkeypatch, name):
    # The server's sample (29 Oct 2026) lost both float runs in their check, which no test ran: it iterated the
    # DuckDB connection, not the query's result. This drives the queue's whole path, fetch to check.
    monkeypatch.setattr(floats, "_lookup", lambda t: {"floatShares": 5.0} if t != "GAPX" else {})
    monkeypatch.setattr(cover_shares, "FILL_PER_RUN", 2)
    http_mock.add("/short_screener", {"data": [{"symbol": "AAPL", "total_float": 7}, {"symbol": "BRK.B",
                                                                                     "total_float": 8}]})
    _sec_routes(http_mock)
    row = base.run_request({"dataset": name, "params": {"as_of": TUE.isoformat()}})
    assert row["result"] != "failed", row
    # Tier A: six names (GAPX unanswered by Yahoo), five issuers (covers of Apple and Alphabet this run)
    assert row["coverage"] == {floats.YAHOO: 0.8333, floats.UW: 0.3333, cover_shares.NAME: 0.4}[name], row
