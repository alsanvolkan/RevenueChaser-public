"""The weekly index lists step (#302, D24): every stored slug fetched once, each list that clears its floor written.

Under history redesign C the live ``index_constituents`` keeps its key and its readers, and the store appends the
rule-A rows to ``index_constituents_changes`` in the write's transaction (``rawstore._capture_changes``). These tests
pin what the step adds on top: the floors, the SmallCap 600 list from the last accepted S&P 1500 snapshot, its
first-fill gate, the provider stamp and the run id on the change rows. Temp stores and fake fetchers only.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import index_registry as ir
from RevenueChaser.core import job_lock, rawstore


def _members(prefix: str, n: int) -> set[str]:
    return {f"{prefix}{i:04d}" for i in range(n)}


@pytest.fixture
def lists(rawstore_tmp_db, monkeypatch):
    """Fake fetchers over a mutable ``state``; ``calls`` records which slugs were fetched."""
    state = {"sp500": _members("S", 450), "dow": _members("D", 30), "nasdaq": _members("N", 1100),
             "sp600": _members("M", 590)}
    calls: list[str] = []

    def fetcher(slug):
        def f():
            calls.append(slug)
            v = state[slug]
            if isinstance(v, Exception):
                raise v
            return set(v)
        return f

    monkeypatch.setattr(ir, "_FETCHERS", {s: fetcher(s) for s in state})
    return state, calls


def _changes(slug: str | None = None) -> list[dict]:
    where, params = ("WHERE slug = ?", [slug]) if slug else ("", [])
    return rawstore.query(f"SELECT slug, ticker, status, source, run_id, run_at FROM index_constituents_changes "
                          f"{where} ORDER BY run_at, ticker", params)


def _statuses(res: dict) -> dict[str, str]:
    return {slug: r["status"] for slug, r in res["slugs"].items()}


# ── floors and slugs ────────────────────────────────────────────────────────────────────────────────────────────
def test_every_stored_slug_has_its_floor():
    assert ir.STORED == ("sp500", "dow", "nasdaq", "sp600")
    assert ir.MIN_INDEX == {"sp500": 400, "dow": 25, "nasdaq": 1000, "sp600": 480}
    assert ir.SOURCE == {"sp500": "uw", "dow": "uw", "nasdaq": "uw", "sp600": "sp1500"}


def test_sp600_has_a_label_but_is_not_fetched_from_a_board():
    """#214 (Common K1): sp600 is labelled, but never joins the lists ``fetch_all_indexes`` walks."""
    assert ir.INDEX_LABELS == ["S&P 500", "Dow Jones", "Nasdaq Composite"]
    assert "sp600" not in dict(ir.INDEXES)
    assert ir.LABELS["sp600"] == "S&P SmallCap 600"


def test_an_sp600_member_gets_its_label_on_a_card(lists):
    ir.refresh_lists(run_id="r1", first_fill=["sp600"])
    cards = ir.annotate_cards([{"ticker": "M0001"}, {"ticker": "S0001"}])
    assert cards[0]["index"] == ["S&P SmallCap 600"] and cards[1]["index"] == ["S&P 500"]


# ── the weekly run ──────────────────────────────────────────────────────────────────────────────────────────────
def test_first_run_writes_the_uw_lists_and_sp600_waits_for_its_first_fill(lists):
    _, calls = lists
    res = ir.refresh_lists(run_id="r1")
    assert _statuses(res) == {"sp500": "written", "dow": "written", "nasdaq": "written",
                              "sp600": "awaiting_first_fill"}
    assert calls == ["sp500", "dow", "nasdaq"]          # three UW requests; the gated list is not even read
    assert ir.load_index("sp600") is None
    assert len(ir.load_index("sp500")) == 450
    assert res["slugs"]["sp500"]["members"] == 450 and res["slugs"]["sp500"]["appended"] == 450


def test_the_first_fill_run_writes_sp600_and_later_runs_keep_it(lists):
    res = ir.refresh_lists(run_id="r1", first_fill=["sp600"])
    assert res["slugs"]["sp600"]["status"] == "written" and len(ir.load_index("sp600")) == 590
    res = ir.refresh_lists(run_id="r2")
    assert res["slugs"]["sp600"] == {"status": "written", "members": 590, "floor": 480, "appended": 0}


def test_an_unchanged_list_appends_nothing(lists):
    ir.refresh_lists(run_id="r1", first_fill=["sp600"])
    before = len(_changes())
    res = ir.refresh_lists(run_id="r2")
    assert {s: r["appended"] for s, r in res["slugs"].items()} == {"sp500": 0, "dow": 0, "nasdaq": 0, "sp600": 0}
    assert len(_changes()) == before


def test_a_dropped_member_gets_one_absent_row(lists):
    state, _ = lists
    ir.refresh_lists(run_id="r1")
    state["sp500"] = (state["sp500"] - {"S0000"}) | {"S9999"}
    res = ir.refresh_lists(run_id="r2")
    assert res["slugs"]["sp500"]["appended"] == 2 and res["slugs"]["dow"]["appended"] == 0
    ir.refresh_lists(run_id="r3")
    rows = [(r["status"], r["run_id"]) for r in _changes("sp500") if r["ticker"] == "S0000"]
    assert rows == [("present", "r1"), ("absent", "r2")]
    assert [(r["status"], r["run_id"]) for r in _changes("sp500") if r["ticker"] == "S9999"] == [("present", "r2")]
    assert "S0000" not in ir.load_index("sp500")


def test_a_list_under_its_floor_writes_nothing(lists):
    state, _ = lists
    ir.refresh_lists(run_id="r1")
    before = len(_changes())
    state["sp500"] = _members("S", 399)
    res = ir.refresh_lists(run_id="r2")
    assert res["slugs"]["sp500"] == {"status": "below_floor", "members": 399, "floor": 400, "appended": 0}
    assert len(ir.load_index("sp500")) == 450 and len(_changes()) == before
    assert res["slugs"]["dow"]["status"] == "written"


def test_a_list_at_its_floor_is_written(lists):
    state, _ = lists
    state["dow"] = _members("D", 25)
    assert ir.refresh_lists(run_id="r1")["slugs"]["dow"]["status"] == "written"


def test_a_first_list_under_its_floor_leaves_the_slug_empty(lists):
    state, _ = lists
    state["nasdaq"] = _members("N", 999)
    assert ir.refresh_lists(run_id="r1")["slugs"]["nasdaq"]["status"] == "below_floor"
    assert ir.load_index("nasdaq") is None


def test_change_rows_carry_the_provider_and_one_run_id(lists):
    ir.refresh_lists(run_id="r1", first_fill=["sp600"])
    by = {(r["slug"], r["source"], r["run_id"]) for r in _changes()}
    assert by == {("sp500", "uw", "r1"), ("dow", "uw", "r1"), ("nasdaq", "uw", "r1"), ("sp600", "sp1500", "r1")}


def test_a_failing_fetcher_is_an_error_and_the_others_still_write(lists):
    state, _ = lists
    ir.refresh_lists(run_id="r1")
    state["dow"] = RuntimeError("boom")
    state["sp500"] = state["sp500"] | {"S9999"}
    res = ir.refresh_lists(run_id="r2")
    assert res["slugs"]["dow"]["status"] == "error" and "boom" in res["slugs"]["dow"]["error"]
    assert res["slugs"]["sp500"]["appended"] == 1
    assert len(ir.load_index("dow")) == 30


def test_a_second_run_while_one_holds_the_lock_fetches_nothing(lists):
    _, calls = lists
    with job_lock.acquire("index_lists", wait=0) as got:
        assert got
        res = ir.refresh_lists(run_id="r1")
    assert res["locked"] is True and res["slugs"] == {} and calls == []
    assert ir.refresh_lists(run_id="r2")["locked"] is False


def test_lists_ok_and_the_summary_line(lists):
    state, _ = lists
    res = ir.refresh_lists(run_id="r1")
    assert ir.lists_ok(res)                              # sp600 waiting for its first fill is not a failure
    assert ir.summary_line(res) == ("Index lists: sp500 450 (+450), dow 30 (+30), nasdaq 1,100 (+1,100), "
                                    "sp600 awaiting first fill")
    state["sp500"] = _members("S", 10)
    state["dow"] = RuntimeError("boom")
    res = ir.refresh_lists(run_id="r2")
    assert not ir.lists_ok(res)
    assert "sp500 below floor (10 < 400, kept)" in ir.summary_line(res)
    assert "dow error" in ir.summary_line(res)


def test_reading_the_lists_writes_nothing(lists):
    """A Refresh reads the registry (annotate_cards, load_registry): it never fetches or appends a change row."""
    _, calls = lists
    ir.refresh_lists(run_id="r1")
    before, fetched = len(_changes()), list(calls)
    ir.load_registry()
    ir.annotate_cards([{"ticker": "S0001"}])
    ir.load_index("sp500")
    assert len(_changes()) == before
    assert calls == fetched


def test_the_module_cli_writes_only_through_the_guarded_step(lists, monkeypatch, capsys):
    """Review S1: ``python -m RevenueChaser.core.index_registry --refresh`` used to write through ``build_registry``,
    outside the lock and the run-id/provider label. It now runs the weekly step; without the flag it only reads."""
    _, calls = lists
    seen: list[list] = []
    real = ir.refresh_lists
    monkeypatch.setattr(ir, "refresh_lists", lambda run_id, first_fill=(): seen.append(list(first_fill))
                        or real(run_id=run_id, first_fill=first_fill))
    monkeypatch.setattr(ir, "build_registry", lambda *a, **k: pytest.fail("the unguarded writer ran"))
    assert ir._main(["--refresh"]) == 0
    assert seen == [[]] and calls == ["sp500", "dow", "nasdaq"]
    assert ir._main([]) == 0
    assert seen == [[]] and calls == ["sp500", "dow", "nasdaq"]          # no flag: read-only, nothing fetched
    assert "registry:" in capsys.readouterr().out


# ── SmallCap 600 from the last accepted S&P 1500 snapshot ──────────────────────────────────────────────────────
def _snapshot(fetched_at: str, rows: list[tuple[str, str]]) -> None:
    from RevenueChaser.core.identity import cache
    from RevenueChaser.core.identity.sources import SourceResult
    cache.save_results([SourceResult("sp1500", "ok", fetched_at=fetched_at, rows=[
        {"ticker": t, "sector": "Industrials", "sub_industry": "x", "index": idx, "name": t} for t, idx in rows])])


def test_sp600_comes_from_the_last_accepted_sp1500_snapshot(rawstore_tmp_db, monkeypatch):
    import httpx

    def no_network(*a, **k):
        raise AssertionError("no request: the identity job already fetched the S&P 1500 list")

    monkeypatch.setattr(httpx, "get", no_network)
    _snapshot("2026-10-01T10:00:00+00:00", [("OLD1", "sp600"), ("KEEP", "sp600"), ("AAA", "sp500")])
    _snapshot("2026-10-08T10:00:00+00:00", [("KEEP", "sp600"), ("NEW1", "sp600"), ("MID1", "sp400"),
                                            ("AAA", "sp500")])
    assert ir.fetch_sp600() == {"KEEP", "NEW1"}


def test_no_sp1500_snapshot_gives_an_empty_sp600_list(rawstore_tmp_db):
    assert ir.fetch_sp600() == set()
