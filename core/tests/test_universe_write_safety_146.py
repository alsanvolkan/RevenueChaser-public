"""#146 (U4 / D6): a partial Yahoo pull never deletes the universe, and two writers never interleave.

- A pull is *full* only when every market-cap chunk ran to its reported total AND the screener quoted at least 90%
  of the stored names that carry a quote (D6). A full pull replaces the table; anything else upserts, flags the pull
  partial (``__fetch__`` provenance row), re-applies the identity master and skips Phase B.
- Membership is the SEC list (#264): the fixture serves one, by default every stored name plus every quoted name, so
  only the tests that say otherwise see a name leave the list.
- ``universe_write_lock`` serialises fetch + write + identity apply + provenance (and Phase B). It is re-entrant
  per thread and gives up at once (no network, no write) when another run holds it.
"""
from __future__ import annotations

import os

import pytest

from RevenueChaser.core import job_lock, rawstore, universe_fetch as uf

SECTOR = "Technology"


def _quote(sym, cap=1e9, **extra):
    q = {"symbol": sym, "quoteType": "EQUITY", "exchange": "NMS", "currency": "USD", "marketCap": cap,
         "regularMarketPrice": 10.0, "shortName": f"{sym} Corp", "sector": SECTOR}
    q.update(extra)
    return q


def _store(*tickers, cap=1e9, quoted=True):
    """Stored rows; ``quoted`` rows carry a Yahoo quote (what a screener pull leaves), the others are SEC-only."""
    uf._save_universe([{"ticker": t, "market_cap": cap, "price": 10.0, "name": f"{t} old", "sector": SECTOR,
                        "quote": {"symbol": t} if quoted else None} for t in tickers])
    uf._save_universe_meta("Old Board", "2026-09-01T00:00:00+00:00")


def _stored() -> set[str]:
    return {r["ticker"] for r in rawstore.query("SELECT ticker FROM equity_universe")}


def _stored_in_order() -> list[str]:
    return [r["ticker"] for r in rawstore.query("SELECT ticker FROM equity_universe")]


@pytest.fixture
def pull(monkeypatch, rawstore_tmp_db):
    """``pull(chunks, members=None)`` installs a fake screener and a fake SEC member list. ``chunks`` is a list;
    each item is a list of quotes served in full, the string ``"dead"`` (every call raises) or
    ``("short", quotes, total)`` (``total`` > len(quotes)). ``members`` are the listed tickers (default: every
    stored ticker and every quoted symbol, so nothing is off the list)."""
    import yfinance
    state = {"screen": 0, "ticker": 0, "sec": 0}
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(uf, "_screener_query", lambda lo, hi: (lo, hi))
    monkeypatch.setattr(uf, "MIN_FULL_UNIVERSE", 1)            # the SEC floor: these lists are tiny

    def install(chunks, members=None):
        monkeypatch.setattr(uf, "MC_CHUNKS", [(i, i + 1) for i in range(len(chunks))])

        def listed():
            state["sec"] += 1
            if members is not None:
                names = list(members)
            else:
                quoted = [q["symbol"] for spec in chunks if spec != "dead"
                          for q in (spec[1] if isinstance(spec, tuple) else spec)]
                names = _stored_in_order() + quoted
            return [{"ticker": t, "name": f"{t} Inc (SEC)"} for t in dict.fromkeys(names)], None

        monkeypatch.setattr(uf, "_sec_member_list", listed)

        def fake_screen(query, offset=None, size=None, **kw):
            state["screen"] += 1
            spec = chunks[query[0]]
            if spec == "dead":
                raise RuntimeError("yahoo is down")
            quotes, total = (spec[1], spec[2]) if isinstance(spec, tuple) else (spec, len(spec))
            start = offset or 0
            return {"total": total, "quotes": quotes[start:start + size]}

        monkeypatch.setattr(yfinance, "screen", fake_screen)

        class _NoTicker:
            def __init__(self, *a, **k):
                state["ticker"] += 1
                raise RuntimeError("no Ticker in these tests")

        monkeypatch.setattr(yfinance, "Ticker", _NoTicker)
        return state

    return install


def _fetch(**kw):
    kw.setdefault("page_size", 2)
    kw.setdefault("max_pages", 10)
    kw.setdefault("retries", 0)
    kw.setdefault("refresh", True)
    kw.setdefault("refreshed_by", "New Board")
    return uf.fetch_universe_stats(**kw)


def _flag() -> str | None:
    row = rawstore.get("equity_universe_provenance", "__fetch__")
    return row.get("refreshed_by") if row else None


# ── U4 / D6: full vs partial ──────────────────────────────────────────────────

def test_a_partial_pull_keeps_the_stored_names_it_missed(pull):
    names = [f"S{i}" for i in range(10)]
    _store(*names)
    pull([[_quote("S0"), _quote("S1"), _quote("S2")]])          # 3 of 10: well below 90%
    rows, stats = _fetch()
    assert _stored() == set(names), "a partial pull deleted stored names"
    assert {r["ticker"] for r in rows} == set(names)
    assert stats["partial"] is True and stats["fetched"] == 3 and stats["stored_before"] == 10
    assert stats["not_seen"] == 7
    assert (stats["chunks_complete"], stats["chunks_total"]) == (1, 1)
    assert _flag() == "partial"


def test_a_partial_pull_still_updates_the_names_it_did_return(pull):
    _store("AAA", "BBB", "CCC", "DDD")
    pull([[_quote("AAA", shortName="AAA renamed")]])
    _fetch()
    assert rawstore.get("equity_universe", "AAA")["name"] == "AAA renamed"
    assert rawstore.get("equity_universe", "BBB")["name"] == "BBB old"


def test_a_dead_chunk_keeps_the_table_even_when_the_rest_is_big_enough(pull):
    names = [f"S{i}" for i in range(10)]
    _store(*names)
    pull([[_quote(n) for n in names[:9]], "dead"])             # 9 of 10 is >= 90%, but chunk 2 never answered
    _, stats = _fetch()
    assert "S9" in _stored(), "an incomplete pull replaced the table"
    assert stats["partial"] is True and (stats["chunks_complete"], stats["chunks_total"]) == (1, 2)


def test_every_chunk_complete_and_at_least_90_percent_replaces_the_table(pull, monkeypatch):
    names = [f"S{i}" for i in range(10)]
    _store(*names)
    backfills = []
    monkeypatch.setattr(uf, "backfill_universe_caps", lambda **kw: backfills.append(kw) or {"requested": 0})
    pull([[_quote(n) for n in names[:5]], [_quote(n) for n in names[5:9]]],
         members=names[:9])                                     # 9 of 10, both chunks complete; S9 left the list
    rows, stats = _fetch()
    assert _stored() == set(names[:9]), "the delisted name should leave on a full pull"
    assert stats["partial"] is False and stats["not_seen"] == 1
    assert _flag() == "full"
    assert backfills == [{"refreshed_by": "New Board"}], "Phase B runs after a full pull"
    assert {r["ticker"] for r in rows} == set(names[:9])


def test_every_chunk_complete_but_below_90_percent_is_partial(pull):
    names = [f"S{i}" for i in range(10)]
    _store(*names)
    pull([[_quote(n) for n in names[:8]]])                      # 8 of 10 = 80%
    _, stats = _fetch()
    assert _stored() == set(names)
    assert stats["partial"] is True and _flag() == "partial"


def test_the_90_percent_boundary_is_inclusive(pull, monkeypatch):
    names = [f"S{i}" for i in range(10)]
    _store(*names)
    monkeypatch.setattr(uf, "backfill_universe_caps", lambda **kw: {})
    pull([[_quote(n) for n in names[:9]]])                      # exactly 9 of 10
    _, stats = _fetch()
    assert stats["partial"] is False


def test_running_out_of_pages_is_incomplete(pull):
    names = [f"S{i}" for i in range(6)]
    _store(*names)
    pull([("short", [_quote(n) for n in names], 6)])
    _, stats = _fetch(page_size=2, max_pages=2)                 # 4 of the 6 reported rows were read
    assert stats["partial"] is True and stats["chunks_complete"] == 0
    assert _stored() == set(names)


def test_an_empty_slice_that_reports_total_zero_is_complete(pull, monkeypatch):
    _store("AAA", "BBB")
    monkeypatch.setattr(uf, "backfill_universe_caps", lambda **kw: {})
    pull([[_quote("AAA"), _quote("BBB")], []])                  # the second market-cap slice is genuinely empty
    _, stats = _fetch()
    assert (stats["chunks_complete"], stats["chunks_total"]) == (2, 2)
    assert stats["partial"] is False


def test_an_empty_page_without_a_total_is_incomplete(pull, monkeypatch):
    import yfinance
    _store("AAA", "BBB")
    pull([[_quote("AAA"), _quote("BBB")]])
    monkeypatch.setattr(yfinance, "screen", lambda *a, **k: {"quotes": []})   # no ``total`` key
    _, stats = _fetch()
    assert stats["partial"] is True and stats["chunks_complete"] == 0


def test_a_page_without_a_total_never_counts_as_complete(pull, monkeypatch):
    import yfinance
    _store("AAA", "BBB")
    pull([[_quote("AAA"), _quote("BBB")]])
    served = []

    def screen(query, offset=None, size=None, **kw):
        quotes = [_quote("AAA"), _quote("BBB")][offset or 0:(offset or 0) + size]
        served.append(len(quotes))
        return {"quotes": quotes}                               # Yahoo omitted ``total``

    monkeypatch.setattr(yfinance, "screen", screen)
    _, stats = _fetch()
    assert stats["partial"] is True, "completeness cannot be proven without a reported total"


def test_nothing_fetched_changes_nothing(pull):
    _store("AAA", "BBB")
    before = rawstore.query("SELECT * FROM equity_universe_provenance ORDER BY key")
    pull(["dead"])
    rows, stats = _fetch()
    assert {r["ticker"] for r in rows} == {"AAA", "BBB"}
    assert stats["partial"] is True and stats["fetched"] == 0
    assert rawstore.query("SELECT * FROM equity_universe_provenance ORDER BY key") == before
    assert _flag() is None


# ── the SEC member list and the quotes (#264) ─────────────────────────────────

def test_a_partial_pull_keeps_the_quote_of_a_stored_name_it_missed(pull):
    _store("AAA", "BBB", "CCC", "DDD")
    pull([[_quote("AAA", regularMarketPrice=11.0)]])
    _, stats = _fetch()
    assert stats["partial"] is True
    assert rawstore.get("equity_universe", "BBB")["quote"] == {"symbol": "BBB"}, "a missed name lost its quote"
    assert rawstore.get("equity_universe", "AAA")["quote"]["regularMarketPrice"] == 11.0


def test_a_partial_pull_adds_a_listed_name_that_is_not_stored_yet(pull):
    _store("AAA", "BBB", "CCC", "DDD")
    pull([[_quote("AAA")]], members=["AAA", "BBB", "CCC", "DDD", "NEWCO"])
    _, stats = _fetch()
    assert stats["partial"] is True
    new = rawstore.get("equity_universe", "NEWCO")
    assert new is not None and new["quote"] is None and new["name"] == "NEWCO Inc (SEC)"
    assert _stored() == {"AAA", "BBB", "CCC", "DDD", "NEWCO"}


def test_stored_sec_only_rows_are_not_in_the_90_percent_denominator(pull, monkeypatch):
    """Rows no screener ever quoted (funds, warrants) cannot be expected back: only quoted rows count."""
    monkeypatch.setattr(uf, "backfill_universe_caps", lambda **kw: {})
    uf._save_universe([{"ticker": t, "market_cap": 1e9, "sector": SECTOR, "quote": {"symbol": t}}
                       for t in ("Q1", "Q2")]
                      + [{"ticker": f"N{i}", "sector": SECTOR} for i in range(8)])     # eight with no quote
    pull([[_quote("Q1"), _quote("Q2")]])                       # 2 of the 2 quoted rows, 2 of the 10 stored rows
    _, stats = _fetch()
    assert stats["partial"] is False and (stats["stored_before"], stats["stored_quoted"]) == (10, 2)
    assert _stored() == {"Q1", "Q2"} | {f"N{i}" for i in range(8)}, "a listed SEC-only name was dropped"


def test_the_90_percent_counts_only_the_stored_quoted_names_it_returned(pull, monkeypatch):
    """B1: names with no stored quote (SEC-only rows quoted this time, new listings) do not count towards the 90%."""
    backfills = []
    monkeypatch.setattr(uf, "backfill_universe_caps", lambda **kw: backfills.append(kw) or {})
    quoted = [f"S{i}" for i in range(10)]
    _store(*quoted)
    uf._save_universe([{"ticker": f"N{i}", "sector": SECTOR} for i in range(5)], replace=False)   # SEC-only
    pull([[_quote(n) for n in quoted[:7]] + [_quote("N0"), _quote("N1"), _quote("NEWCO")]])
    _, stats = _fetch()                                         # 10 quotes back, but only 7 of the 10 stored quoted
    assert stats["partial"] is True and _flag() == "partial", "70% overlap was classed as a full pull"
    assert backfills == []
    for t in quoted[7:]:
        assert rawstore.get("equity_universe", t)["quote"] == {"symbol": t}, f"{t} lost its stored quote"
    assert _stored() == set(quoted) | {f"N{i}" for i in range(5)} | {"NEWCO"}


def test_a_full_pull_keeps_a_listed_name_the_screener_did_not_return(pull, monkeypatch):
    """Only the SEC list removes a name: the screener missing it leaves a row with no quote, not a deleted row."""
    monkeypatch.setattr(uf, "backfill_universe_caps", lambda **kw: {})
    names = [f"S{i}" for i in range(10)]
    _store(*names)
    pull([[_quote(n) for n in names[:9]]])                      # S9 is listed, the screener did not return it
    _, stats = _fetch()
    assert stats["partial"] is False
    assert _stored() == set(names)
    assert rawstore.get("equity_universe", "S9")["quote"] is None
    assert rawstore.get("equity_universe", "S0")["quote"] is not None


def test_a_failed_sec_list_changes_nothing_and_reports_the_stored_flag(pull, monkeypatch):
    _store("AAA", "BBB")
    uf._save_fetch_state(True, "2026-09-02T00:00:00+00:00")
    state = pull([[_quote("AAA")]])
    monkeypatch.setattr(uf, "_sec_member_list", lambda: ([], "SEC down"))
    rows, stats = _fetch()
    assert {r["ticker"] for r in rows} == {"AAA", "BBB"} and state["screen"] == 0
    assert stats["sec_error"] == "SEC down" and stats["partial"] is True
    assert uf.load_fetch_state()["generated_at"] == "2026-09-02T00:00:00+00:00"


# ── the first load ────────────────────────────────────────────────────────────

def test_the_first_load_saves_what_it_got_and_flags_a_dead_chunk(pull):
    pull([[_quote("AAA"), _quote("BBB")], "dead"])
    rows, stats = _fetch()
    assert _stored() == {"AAA", "BBB"} and {r["ticker"] for r in rows} == {"AAA", "BBB"}
    assert stats["partial"] is True and stats["stored_before"] == 0
    assert _flag() == "partial"
    assert rawstore.get("equity_universe_provenance", "__meta__")["refreshed_by"] == "New Board"
    assert stats["universe_refreshed_by"] == "New Board"


def test_the_first_load_with_every_chunk_complete_is_full(pull, monkeypatch):
    monkeypatch.setattr(uf, "backfill_universe_caps", lambda **kw: {})
    pull([[_quote("AAA"), _quote("BBB")]])
    _, stats = _fetch()
    assert stats["partial"] is False and _flag() == "full"


# ── what the upsert keeps, and what runs after it ─────────────────────────────

def test_the_upsert_keeps_the_stored_cap_price_and_sector(pull):
    uf._save_universe([{"ticker": "AAA", "market_cap": 5e9, "price": 77.0, "name": "AAA old",
                        "sector": "Technology", "industry": "Consumer Electronics",
                        "quote": {"symbol": "AAA"}},
                       {"ticker": "BBB", "market_cap": 1e9, "quote": {"symbol": "BBB"}},
                       {"ticker": "CCC", "market_cap": 1e9, "quote": {"symbol": "CCC"}},
                       {"ticker": "DDD", "market_cap": 1e9, "quote": {"symbol": "DDD"}}])
    pull([[{"symbol": "AAA", "quoteType": "EQUITY", "exchange": "NMS", "currency": "USD", "shortName": "AAA new"}]])
    _, stats = _fetch()
    assert stats["partial"] is True
    row = rawstore.get("equity_universe", "AAA")
    assert (row["market_cap"], row["price"]) == (5e9, 77.0), "carry-forward lost on the upsert path"
    assert row["sector"] == "Technology" and row["industry"] == "Consumer Electronics"
    assert row["name"] == "AAA new"


def test_the_identity_master_is_applied_after_the_upsert(pull, monkeypatch):
    names = ["AAA", "BBB", "CCC", "DDD"]
    _store(*names)
    seen = []
    monkeypatch.setattr(uf, "_apply_identity_master",
                        lambda: seen.append(rawstore.get("equity_universe", "AAA")["name"]) or {})
    pull([[_quote("AAA", shortName="AAA fresh")]])
    _fetch()
    assert seen == ["AAA fresh"], "the identity master must run once, after the upsert"


def test_phase_b_is_skipped_after_a_partial_pull(pull, monkeypatch):
    _store("AAA", "BBB", "CCC", "DDD")
    calls = []
    monkeypatch.setattr(uf, "backfill_universe_caps", lambda **kw: calls.append(kw) or {})
    pull([[_quote("AAA")]])
    _, stats = _fetch()
    assert calls == [] and "cap_backfill" not in stats


def test_a_partial_pull_leaves_the_membership_provenance_alone(pull):
    _store("AAA", "BBB", "CCC", "DDD")
    pull([[_quote("AAA")]])
    _, stats = _fetch()
    meta = rawstore.get("equity_universe_provenance", "__meta__")
    assert (meta["generated_at"], meta["refreshed_by"]) == ("2026-09-01T00:00:00+00:00", "Old Board")
    assert stats["universe_refreshed_by"] == "Old Board"


def test_saving_the_membership_provenance_keeps_the_other_keys(rawstore_tmp_db):
    """F10: ``_save_universe_meta`` used ``replace_all`` and deleted ``__stats__`` and ``__fetch__``."""
    uf._save_stats_meta("Settings", "2026-09-02T00:00:00+00:00")
    uf._save_fetch_state(True, "2026-09-02T00:00:00+00:00")
    uf._save_universe_meta("Board", "2026-09-03T00:00:00+00:00")
    keys = {r["key"] for r in rawstore.query("SELECT key FROM equity_universe_provenance")}
    assert keys == {"__meta__", "__stats__", "__fetch__"}


def test_the_fetch_state_round_trips(rawstore_tmp_db):
    assert uf.load_fetch_state() == {}
    uf._save_fetch_state(True, "2026-09-02T00:00:00+00:00")
    assert uf.load_fetch_state() == {"partial": True, "generated_at": "2026-09-02T00:00:00+00:00"}
    uf._save_fetch_state(False, "2026-09-03T00:00:00+00:00")
    assert uf.load_fetch_state()["partial"] is False


def test_the_cached_path_reports_the_stored_partial_flag(rawstore_tmp_db):
    _store("AAA")
    assert uf.fetch_universe_stats()[1]["partial"] is False
    uf._save_fetch_state(True, "2026-09-02T00:00:00+00:00")
    assert uf.fetch_universe_stats()[1]["partial"] is True
    assert uf.fetch_universe_stats(no_fetch=True)[1]["partial"] is True


# ── the lock ──────────────────────────────────────────────────────────────────

def test_a_second_fetch_while_the_lock_is_held_makes_no_screener_call(pull):
    names = ["AAA", "BBB"]
    _store(*names)
    state = pull([[_quote("ZZZ")]])
    with job_lock.acquire("universe", wait=0) as got:
        assert got
        rows, stats = _fetch()
    assert state["screen"] == 0, "a locked fetch reached the screener"
    assert state["sec"] == 0, "a locked fetch reached the SEC file"
    assert stats["locked"] == os.getpid()
    assert {r["ticker"] for r in rows} == set(names) and _stored() == set(names)
    assert _flag() is None, "a locked fetch wrote provenance"


def test_a_locked_backfill_returns_zeros_and_touches_no_ticker(pull, monkeypatch):
    uf._save_universe([{"ticker": "AAA", "sector": SECTOR}])
    state = pull([[_quote("AAA")]])
    with job_lock.acquire("universe", wait=0):
        res = uf.backfill_universe_caps(refreshed_by="Settings")
    assert res["requested"] == 0 and res["filled"] == 0 and res["locked"] == os.getpid()
    assert state["ticker"] == 0
    assert rawstore.get("equity_universe_provenance", "__stats__") is None


def test_the_lock_is_released_after_an_error(pull, monkeypatch):
    _store("AAA", "BBB", "CCC", "DDD")
    pull([[_quote("AAA")]])
    monkeypatch.setattr(uf, "_apply_identity_master", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        _fetch()
    with job_lock.acquire("universe", wait=0) as got:
        assert got, "the universe lock leaked after an exception"


def test_the_lock_is_re_entrant_within_a_thread(rawstore_tmp_db):
    with uf.universe_write_lock() as outer:
        assert outer is None
        with uf.universe_write_lock() as inner:
            assert inner is None
        assert ("universe", os.getpid()) in job_lock.holders(), "the inner exit released the outer lock"
    assert ("universe", os.getpid()) not in job_lock.holders()


def test_a_nested_exit_keeps_the_outer_hold_for_the_next_nested_call(rawstore_tmp_db):
    """After an inner block ends, the outer one still owns the lock: a second inner call must not see it as held."""
    with uf.universe_write_lock() as outer:
        assert outer is None
        with uf.universe_write_lock():
            with uf.universe_write_lock() as deepest:
                assert deepest is None
        with uf.universe_write_lock() as again:
            assert again is None, "the nested exit dropped the outer hold"
    assert ("universe", os.getpid()) not in job_lock.holders()


def test_another_thread_sees_the_lock_as_held(rawstore_tmp_db):
    import threading
    seen = []
    with uf.universe_write_lock():
        t = threading.Thread(target=lambda: seen.append(_other()))
        t.start()
        t.join()
    assert seen == [os.getpid()]


def _other():
    with uf.universe_write_lock() as other:
        return other


def test_a_full_fetch_runs_phase_b_inside_its_own_lock(pull, monkeypatch):
    """Phase B takes the same lock: if it were not re-entrant the nested call would report ``locked``."""
    import yfinance

    class _Fast:
        shares, last_price = 1e6, 10.0

    class _Ticker:
        def __init__(self, t):
            self.fast_info = _Fast()

    pull([[_quote("AAA", cap=None)]])
    monkeypatch.setattr(yfinance, "Ticker", _Ticker)
    monkeypatch.setattr(uf, "_CAP_BACKFILL_SLEEP", 0)
    _, stats = _fetch()
    assert stats["partial"] is False
    assert "locked" not in stats["cap_backfill"] and stats["cap_backfill"]["requested"] == 1


# ── the visible flag ──────────────────────────────────────────────────────────

def test_the_provenance_line_says_partial_fetch_and_escapes_the_board_name():
    from RevenueChaser.core.reporting_common import universe_provenance_html
    plain = universe_provenance_html("2026-10-03T00:00:00+00:00", "Swing Potential")
    flagged = universe_provenance_html("2026-10-03T00:00:00+00:00", "Swing Potential", partial=True)
    assert "partial fetch" not in plain and "partial fetch" in flagged
    hostile = universe_provenance_html("2026-10-03T00:00:00+00:00", "<img src=x>", partial=True)
    assert "<img src=x>" not in hostile


def test_the_boards_show_the_stored_partial_flag(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.module_fundamentals import pipeline as fp
    from RevenueChaser.module_fundamentals.report import render_fundamental_dashboard
    from RevenueChaser.module_swing_potential import pipeline as sp
    from RevenueChaser.module_swing_potential.report import render_swing_dashboard
    # A Fetch over the stored universe (a Refresh shows that Fetch's payload, K4 #383 R1); no SPY download.
    _store("AAA")
    for mod, render in ((fp, render_fundamental_dashboard), (sp, render_swing_dashboard)):
        monkeypatch.setattr(mod, "prefilter", lambda r: mod.screen_common.Prefiltered([]))
        monkeypatch.setattr(mod, "load_ohlc", lambda *a, **k: None)
        assert mod.run_scan()["universe_partial"] is False
    uf._save_fetch_state(True, "2026-10-03T00:00:00+00:00")
    for mod, render in ((fp, render_fundamental_dashboard), (sp, render_swing_dashboard)):
        payload = mod.run_scan()
        assert payload["universe_partial"] is True
        assert "partial fetch" in render(payload)


def test_the_asymmetric_growth_board_shows_the_stored_partial_flag(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.module_asymmetric_growth import engine
    from RevenueChaser.module_asymmetric_growth.report import render_asymmetric_growth_hunter_html
    _store("AAA")
    monkeypatch.setattr(engine, "enrich_and_score", lambda *a, **k: None)
    uf._save_fetch_state(True, "2026-10-03T00:00:00+00:00")
    result = engine.run_asymmetric_growth_hunter(None, no_fetch=True)
    assert result["universe_partial"] is True
    assert "partial fetch" in render_asymmetric_growth_hunter_html(result)


def test_the_settings_inventory_lists_the_fetch_row(rawstore_tmp_db):
    from RevenueChaser.module_settings import inventory
    uf._save_universe([{"ticker": "AAA", "sector": SECTOR, "market_cap": 1e9}])
    uf._save_universe_meta("Board", "2026-10-03T00:00:00+00:00")
    uf._save_fetch_state(True, "2026-10-03T00:00:00+00:00")
    rows = {r["key"]: r for r in inventory.collect()["rows"]}
    assert "__fetch__" in rows["universe"]["note"] and "partial" in rows["universe_provenance"]["note"]
    assert rows["universe_provenance"]["rows"] == 2
