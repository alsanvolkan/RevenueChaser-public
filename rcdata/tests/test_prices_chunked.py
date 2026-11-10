"""#654: a whole ``prices`` build runs the WRDS history in chunks of securities and serves what one pass serves.

The proof is metamorphic, as for the build on the live copy (``test_prices_incremental``): the same tree built whole
with ``RC_PRICES_CHUNKS=1`` (the single pass) and with more chunks serves the same rows in every table and view, the
WRDS difference report included, and leaves the same build issues open. Two chunks cut the fixture's securities in
half; 64 give every security a chunk of its own, so the classes of one issuer (GOOGL, GOOG) and a fund and its aliased
Compustat series sit on chunk edges.
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.rcdata.build import dirty
from RevenueChaser.rcdata.build.prices import stage

from .test_prices_incremental import (FIGI, SPY_DAYS, SPYX, _note, _report, _serve, _snapshot, _spy,  # noqa: F401
                                      _spy_spine, alphabet, live)
from .test_prices_wrds import _q, env, fake  # noqa: F401


def _whole(monkeypatch, n: int) -> tuple[dict, dict]:
    monkeypatch.setenv(dirty.WHOLE_ENV, "1")
    monkeypatch.setenv(stage.CHUNKS_ENV, str(n))
    row = _serve()
    assert "built whole" in _note(row), _note(row)
    return row, {**_snapshot(), "wrds_price_diff": _report()}


def _same_in_chunks(monkeypatch, n: int) -> None:
    one_row, one = _whole(monkeypatch, 1)
    assert "chunked whole build" not in _note(one_row)
    row, got = _whole(monkeypatch, n)
    assert "chunked whole build: " in _note(row), _note(row)
    for name in one:
        if got[name] != one[name]:
            print("DIFF", name, "\n chunked-only", sorted(set(got[name]) - set(one[name]), key=repr)[:20],
                  "\n one-pass-only", sorted(set(one[name]) - set(got[name]), key=repr)[:20])
        assert got[name] == one[name], name


@pytest.mark.parametrize("n", [2, 64])
def test_chunks_serve_what_one_pass_serves(live, monkeypatch, n):
    """AAPL with its split, its dividends and an old year; OLDCO outside the universe."""
    _same_in_chunks(monkeypatch, n)


def test_an_issuer_whose_classes_sit_in_two_chunks_keeps_its_issuer_cap(alphabet, monkeypatch):
    _same_in_chunks(monkeypatch, 64)
    caps = _q("SELECT market_cap, issuer_market_cap FROM market_cap_daily WHERE security_id = '160329:01' "
              "AND session_date = '2026-10-27'")
    assert caps and caps[0]["issuer_market_cap"] > caps[0]["market_cap"]          # GOOG's cap of the 23rd in it


@pytest.mark.parametrize("extra", [(), (SPYX,)], ids=["no-cik", "with-cik"])
def test_a_fund_and_its_aliased_series_are_built_in_one_chunk(fake, monkeypatch, extra):
    """SPY's FIGI security and its Compustat series ``001000:01`` (an alias) sort far apart: the series is staged in
    the FIGI's chunk, never in its own."""
    _spy(fake, monkeypatch, extra)
    _spy_spine(date(1993, 1, 22), extra)
    _same_in_chunks(monkeypatch, 64)
    assert _q("SELECT 1 FROM price_bar_daily WHERE security_id = ?", [FIGI])
    assert not _q("SELECT 1 FROM price_bar_daily WHERE security_id = '001000:01'")


def test_two_series_of_one_fund_serve_the_lower_series_in_any_chunking(fake, monkeypatch):
    """Review F3 of #654: two Compustat series aliased to one fund price the same sessions; the served bar is the
    lower series' (``001000:01``), not whichever ``rowid`` came first."""
    from .test_prices_wrds import _secd
    conn = fake[0]
    conn.data["comp.secd"] = conn.data["comp.secd"] + [
        _secd("001000", "03", d, "SPY", 600.0 + i, cusip="78462F103", exchg=17) for i, d in enumerate(SPY_DAYS)]
    _spy(fake, monkeypatch)
    _spy_spine(date(1993, 1, 22))
    _same_in_chunks(monkeypatch, 64)
    closes = [r["close"] for r in _q("SELECT close FROM price_bar_daily WHERE security_id = ? ORDER BY session_date",
                                     [FIGI])]
    assert closes and closes[0] == 500.0


@pytest.mark.parametrize("value, want", [("", stage.CHUNKS_DEFAULT), ("4", 4), ("0", stage.CHUNKS_DEFAULT),
                                         ("x", stage.CHUNKS_DEFAULT)])
def test_the_chunk_count_comes_from_the_environment(monkeypatch, value, want):
    monkeypatch.setenv(stage.CHUNKS_ENV, value)
    assert stage.chunks() == want
