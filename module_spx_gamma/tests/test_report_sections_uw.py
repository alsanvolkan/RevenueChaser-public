"""The two report sections that need a provider call: §4.1 directional vega flow and §17's VIX option book.

Both read what UW answers and sum it; neither is in the market store, because neither is a board input anything
else reads. A day whose fetch did not get them renders without the rows rather than with blanks.
"""
from __future__ import annotations

from RevenueChaser.module_spx_gamma.normalize import normalize_vega_flow, normalize_vix_greeks


# ── §4.1, the directional vega flow ───────────────────────────────────────────


def _flow(*pairs) -> dict:
    return {"data": [{"timestamp": f"2026-10-30T{14 + i}:00:00Z", "dir_vega_flow": str(d),
                      "total_vega_flow": str(t)} for i, (d, t) in enumerate(pairs)]}


def test_the_flow_is_the_days_buckets_summed():
    """The report reads one cumulative number for the session, over UW's per-minute buckets."""
    got = normalize_vega_flow(_flow((-4_000_000, 10_000_000), (-4_630_000, 20_000_000), (0, 5_000_000)))
    assert got["dir_vega_flow"] == -8_630_000, "the 30 Oct report's -8,63 million"
    assert got["total_vega_flow"] == 35_000_000
    assert got["buckets"] == 3
    assert got["last_bucket"] == "2026-10-30T16:00:00Z"


def test_the_sign_survives_a_mixed_day():
    got = normalize_vega_flow(_flow((5_000_000, 1), (-9_000_000, 1)))
    assert got["dir_vega_flow"] == -4_000_000, "buckets net off, they are not taken as absolutes"


def test_an_unreadable_bucket_is_skipped_not_counted():
    payload = _flow((-1_000, 1))
    payload["data"].append({"timestamp": "2026-10-30T15:00:00Z", "dir_vega_flow": None, "total_vega_flow": "x"})
    got = normalize_vega_flow(payload)
    assert got["dir_vega_flow"] == -1_000 and got["buckets"] == 1


def test_no_flow_is_none_so_the_row_is_left_out():
    for empty in (None, {}, {"data": []}, {"data": [{"timestamp": "t"}]}):
        assert normalize_vega_flow(empty) is None


# ── §17, the VIX index's own option book ──────────────────────────────────────


def _vix_book(*rows) -> dict:
    return {"data": [{"expiry": f"2026-11-{18 + i:02d}", "call_vanna": str(cv), "put_vanna": str(pv),
                      "call_charm": str(cc), "put_charm": str(pc)}
                     for i, (cv, pv, cc, pc) in enumerate(rows)]}


def test_the_book_is_net_vanna_and_net_charm_over_its_expiries():
    """The 30 Oct report: net vanna +137,1 M$ and net charm -1.405,2 M$ over 13 expiries."""
    rows = [(137_100_000 / 13, 0, -1_405_200_000 / 13, 0) for _ in range(13)]
    got = normalize_vix_greeks(_vix_book(*rows))
    assert round(got["net_vanna"]) == 137_100_000
    assert round(got["net_charm"]) == -1_405_200_000
    assert got["expiries"] == 13


def test_calls_and_puts_net_against_each_other():
    got = normalize_vix_greeks(_vix_book((10, -4, 2, -8)))
    assert got["net_vanna"] == 6 and got["net_charm"] == -6


def test_a_book_with_no_greeks_at_all_is_none():
    assert normalize_vix_greeks({"data": [{"expiry": "2026-11-18"}]}) is None


def test_no_book_is_none_so_the_row_is_left_out():
    for empty in (None, {}, {"data": []}):
        assert normalize_vix_greeks(empty) is None
