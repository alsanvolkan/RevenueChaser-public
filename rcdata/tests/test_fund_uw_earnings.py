"""``uw/earnings``: the EPS a company reported, as UW serves it, in its own table.

It is not a ``fundamental_fact`` row on purpose: that table's check refuses a non-preliminary row without an
accession, and ``preliminary`` is what every served view reads as "not the SEC's, do not show". avo, 5 Nov 2026:
UW's reported EPS is a column of its own, source-labelled, never mixed with the SEC's GAAP EPS.
"""
from __future__ import annotations

from datetime import date

from RevenueChaser.rcdata.build.fundamentals import uw_earnings

CIK, TICKER = "789019", "MSFT"
ANSWER = [
    {"report_type": "quarterly", "fiscal_date_ending": "2025-09-30", "report_date": "2025-10-29",
     "reported_eps": "3.72", "estimated_eps": "3.66", "surprise_percentage": "1.64"},
    {"report_type": "quarterly", "fiscal_date_ending": "2025-06-30", "report_date": "2025-07-30",
     "reported_eps": "3.65", "estimated_eps": "3.37", "surprise_percentage": "8.31"},
    {"report_type": "annual", "fiscal_date_ending": "2025-06-30", "report_date": "2025-07-30",
     "reported_eps": "13.64", "estimated_eps": "13.4", "surprise_percentage": "1.79"},
    {"report_type": "quarterly", "fiscal_date_ending": None, "reported_eps": "1.0"},
    {"report_type": "quarterly", "fiscal_date_ending": "2025-03-31", "reported_eps": None},
]


def test_the_quarterly_rows_become_one_row_per_period():
    rows, counts = uw_earnings.rows_of(ANSWER, CIK, TICKER)
    assert [r["period_end"] for r in rows] == [date(2025, 6, 30), date(2025, 9, 30)]   # oldest first
    newest = rows[-1]
    assert newest == {"cik": CIK, "ticker": TICKER, "period_end": date(2025, 9, 30),
                      "report_date": date(2025, 10, 29), "eps_reported": 3.72, "eps_estimate": 3.66,
                      "surprise_pct": 1.64, "source": uw_earnings.NAME}
    assert counts == {"not_quarterly": 1, "no_period_end": 1, "no_reported_eps": 1}


def test_the_surprise_is_uws_own_ratio_never_recomputed():
    """Recomputing it from two numbers of different bases is how a wrong surprise is made."""
    rows, _ = uw_earnings.rows_of([{"report_type": "quarterly", "fiscal_date_ending": "2025-09-30",
                                    "reported_eps": "2.0", "estimated_eps": "1.0",
                                    "surprise_percentage": "7.5"}], CIK, TICKER)
    assert rows[0]["surprise_pct"] == 7.5                      # not (2.0 - 1.0) / 1.0 * 100


def test_an_annual_row_sharing_a_quarters_date_never_wins():
    """The review's S3: an annual and a fourth-quarter row share a fiscal date, so ``report_type`` is read first."""
    rows, counts = uw_earnings.rows_of(
        [{"report_type": "annual", "fiscal_date_ending": "2025-06-30", "reported_eps": "13.64"},
         {"report_type": "quarterly", "fiscal_date_ending": "2025-06-30", "reported_eps": "3.65"}], CIK, TICKER)
    assert [r["eps_reported"] for r in rows] == [3.65] and counts == {"not_quarterly": 1}


def test_the_answer_may_be_a_data_object_or_a_bare_list():
    wrapped, _ = uw_earnings.rows_of({"data": ANSWER}, CIK, TICKER)
    bare, _ = uw_earnings.rows_of(ANSWER, CIK, TICKER)
    assert wrapped == bare and wrapped


def test_a_percent_sign_and_a_dash_are_read_as_uw_writes_them():
    rows, _ = uw_earnings.rows_of([{"report_type": "quarterly", "fiscal_date_ending": "2025-09-30",
                                    "reported_eps": "3.72", "estimated_eps": "-",
                                    "surprise_percentage": "1.64%"}], CIK, TICKER)
    assert rows[0]["eps_estimate"] is None and rows[0]["surprise_pct"] == 1.64


def test_the_served_table_is_its_own_and_the_schema_says_so():
    from RevenueChaser.rcdata.build.fundamentals import tables
    assert "earnings_reported" in tables.SCHEMAS
    assert [c for c, _ in tables.SCHEMAS["earnings_reported"]][:3] == ["cik", "ticker", "period_end"]
    # eps_reported is never a fundamental_fact field: that table is the SEC's values only.
    from RevenueChaser.rcdata.build.fundamentals import facts
    assert "eps_reported" not in facts.FIELDS



def _asked_paths(call):
    """The URL paths a real :class:`UWClient` sends for ``call(client)``, read at the HTTP boundary."""
    import httpx
    from RevenueChaser.core.providers.unusual_whales import UWClient
    seen: list[str] = []

    def answer(request):
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, json=[])

    client = UWClient(key="test", sleep=lambda _s: None)
    client._client = httpx.Client(transport=httpx.MockTransport(answer))
    call(client)
    return seen


def test_the_request_writes_a_share_class_the_way_uw_does():
    """UW writes a share class with a dot; the SEC's files and the spine write it with a dash. Asked with the
    dash, UW answers 422 (BRK-A, BF-A, AKO-A, CRD-A, MOG-A, the five of 5,304 that failed on 6 Nov 2026).
    Read from the request the dataset actually sends (``stock_earnings_result``), not from a helper beside it."""
    assert _asked_paths(lambda c: c.stock_earnings_result("BRK-A")) == ["/api/stock/BRK.A/earnings"]
    assert _asked_paths(lambda c: c.fundamentals_result("BF-A")) == ["/api/stock/BF.A/financials"]
    assert _asked_paths(lambda c: c.stock_earnings_result("AAPL")) == ["/api/stock/AAPL/earnings"]
    # The recorded endpoint names the same path the request asked.
    from RevenueChaser.rcdata.build.fundamentals import uw_financials
    assert uw_earnings.endpoint("BRK-A") == "/stock/BRK.A/earnings"
    assert uw_financials.endpoint("BRK-A") == "/stock/BRK.A/financials"


def test_the_segment_encoder_stays_a_pure_encoder():
    """``_seg`` percent-encodes and changes no character; the UW spelling is ``uw_symbol``'s job alone."""
    from RevenueChaser.core.providers.unusual_whales import _seg, uw_symbol
    assert (_seg("BRK-A"), _seg("afterhours"), _seg("1d"), _seg("a/b")) == ("BRK-A", "afterhours", "1d", "a%2Fb")
    assert (uw_symbol("BRK-A"), uw_symbol("BRK.B"), uw_symbol("AAPL")) == ("BRK.A", "BRK.B", "AAPL")


def _asked(call, body):
    """``(path?query, ...)`` a real :class:`UWClient` sends for ``call(client)``, each answered with ``body``,
    and what ``call`` returned."""
    import httpx
    from RevenueChaser.core.providers.unusual_whales import UWClient
    seen: list[str] = []

    def answer(request):
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, json=body)

    client = UWClient(key="test", sleep=lambda _s: None)
    client._client = httpx.Client(transport=httpx.MockTransport(answer))
    return seen, call(client)


def test_a_share_class_in_a_query_parameter_is_written_the_way_uw_does():
    """#519 finding 5: the dot rule held only for path segments; ``ticker_symbol`` went out as ``BRK-A``."""
    seen, _ = _asked(lambda c: c.option_trades_result("BRK-A"), [])
    assert "ticker_symbol=BRK.A" in seen[0], seen


def test_a_batched_screener_asks_uws_spelling_and_answers_under_the_callers():
    """#519 finding 5: the comma list went out with dashes, and the result was keyed by UW's own ``ticker``, so a
    caller asking for ``BRK-A`` looked up a key that was never there."""
    seen, res = _asked(lambda c: c.screener_batch_result(["BRK-A", "AAPL"]),
                       {"data": [{"ticker": "BRK.A", "volume": 1}, {"ticker": "AAPL", "volume": 2}]})
    assert "ticker=BRK.A%2CAAPL" in seen[0] or "ticker=BRK.A,AAPL" in seen[0], seen
    assert res.data["BRK-A"]["volume"] == 1 and res.data["AAPL"]["volume"] == 2
    assert "BRK.A" not in res.data


def test_the_short_screener_answers_under_the_callers_spelling_too():
    seen, res = _asked(lambda c: c.short_screener_result(["BF-B"]), {"data": [{"symbol": "BF.B", "short_interest": 5}]})
    assert "BF.B" in seen[0], seen
    assert res.data == {"BF-B": {"symbol": "BF.B", "short_interest": 5}}


def test_the_public_path_helper_is_the_uw_spelling_percent_encoded():
    """#519 finding 9: the datasets imported the private ``_seg``; ``uw_path`` is the public form of the two."""
    from RevenueChaser.core.providers.unusual_whales import uw_path
    assert (uw_path("BRK-A"), uw_path("AAPL"), uw_path("A/B")) == ("BRK.A", "AAPL", "A%2FB")


def test_a_restated_quarter_is_counted_not_dropped_silently():
    """#519 finding 9: two quarterly rows for one period end (a restatement): the last one is kept, as before, and
    the other is now in ``counts``."""
    twice = [{"report_type": "quarterly", "fiscal_date_ending": "2025-09-30", "reported_eps": "3.70"},
             {"report_type": "quarterly", "fiscal_date_ending": "2025-09-30", "reported_eps": "3.72"}]
    rows, counts = uw_earnings.rows_of(twice, CIK, TICKER)
    assert [r["eps_reported"] for r in rows] == [3.72]
    assert counts == {"duplicate_period": 1}


def test_the_newest_answer_per_company_is_one_walk_and_ignores_a_stray_folder(monkeypatch, tmp_path):
    """#519 finding 7: one walk of the raw root gives every company's newest day; a folder that is not a date
    (a stray ``tmp``, or ``2026-11-02-old``, which starts like one) is skipped instead of raising or winning."""
    from RevenueChaser.rcdata.build.fundamentals import common
    monkeypatch.setattr(common, "raw_dir", lambda _name: tmp_path)
    for day, tickers in {"2026-10-23": ["MSFT", "AAPL"], "2026-10-30": ["MSFT"], "tmp": ["MSFT"],
                         "2026-11-02-old": ["MSFT"]}.items():
        (tmp_path / day).mkdir()
        for t in tickers:
            (tmp_path / day / f"{t}.json.gz").write_bytes(b"")
    assert uw_earnings.newest_by_ticker() == {"MSFT": date(2026, 10, 30), "AAPL": date(2026, 10, 23)}
