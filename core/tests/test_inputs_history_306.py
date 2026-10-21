"""Watchlist and board settings keep every change (#306, D36, rule A; history redesign C).

The store records each write to ``watchlist`` and ``board_settings`` in ``<table>_changes`` (schema 11). These tests
pin what a user action leaves there: a toggle writes its own ticker only, in one transaction, and a Reset appends its
'absent' rows in one change. Temp stores only.
"""
from __future__ import annotations

import threading

import pytest

from RevenueChaser.core import board_settings as bs
from RevenueChaser.core import rawstore
from RevenueChaser.module_comovement import config

B = "module_comovement"


def _changes(name: str, where: str = "TRUE", params: list | None = None) -> list[dict]:
    return rawstore.query(f"SELECT * FROM {name}_changes WHERE {where} ORDER BY run_at", params or [])


# ── watchlist: one ticker, one transaction ───────────────────────────────────────────────────────────────────────
def test_toggle_adds_then_removes_and_says_which(rawstore_tmp_db):
    assert rawstore.toggle_watchlist("AAA") is True
    assert rawstore.load_watchlist() == {"AAA"}
    assert rawstore.toggle_watchlist("AAA") is False
    assert rawstore.load_watchlist() == set()


def test_toggle_off_then_on_in_one_session_keeps_both_rows(rawstore_tmp_db):
    for _ in range(3):
        rawstore.toggle_watchlist("AAA")
    rows = _changes("watchlist", "ticker = ?", ["AAA"])
    assert [r["status"] for r in rows] == ["present", "absent", "present"]
    assert {r["source"] for r in rows} == {"manual"}
    assert len({r["run_at"] for r in rows}) == 3


def test_toggle_leaves_the_other_tickers_alone(rawstore_tmp_db):
    rawstore.write_rows("watchlist", [{"ticker": "BBB"}, {"ticker": "CCC"}])
    before = _changes("watchlist")
    rawstore.toggle_watchlist("AAA")
    rawstore.toggle_watchlist("BBB")
    assert rawstore.load_watchlist() == {"AAA", "CCC"}
    new = _changes("watchlist")[len(before):]
    assert [(r["ticker"], r["status"]) for r in new] == [("AAA", "present"), ("BBB", "absent")]


def test_toggle_never_rewrites_the_whole_list(rawstore_tmp_db, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a toggle rewrote the whole watchlist")
    monkeypatch.setattr(rawstore, "replace_all", boom)
    monkeypatch.setattr(rawstore, "clear", boom)
    rawstore.toggle_watchlist("AAA")
    rawstore.toggle_watchlist("AAA")


def test_concurrent_toggles_of_different_tickers_all_land(rawstore_tmp_db):
    tickers = [f"T{i}" for i in range(8)]
    errors: list[Exception] = []
    gate = threading.Barrier(len(tickers))

    def flip(t):
        try:
            gate.wait(10)
            rawstore.toggle_watchlist(t)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=flip, args=(t,)) for t in tickers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors[:2]
    assert rawstore.load_watchlist() == set(tickers)
    latest = rawstore.changes_as_of("watchlist", float("inf"))
    assert {r["ticker"] for r in latest} == set(tickers)


# ── board settings: Reset appends 'absent', the board reads its defaults ─────────────────────────────────────────
def test_reset_appends_absent_rows_and_the_board_reads_its_defaults(rawstore_tmp_db):
    bs.save(B, {"WINSOR_K": 6.5, "MIN_OBS": 100})
    bs.reset(B)
    rows = _changes("board_settings", "board = ?", [B])
    assert [(r["key"], r["status"]) for r in rows if r["status"] == "absent"] in (
        [("MIN_OBS", "absent"), ("WINSOR_K", "absent")], [("WINSOR_K", "absent"), ("MIN_OBS", "absent")])
    assert sorted((r["key"], r["value"]) for r in rows if r["status"] == "present") == [
        ("MIN_OBS", 100.0), ("WINSOR_K", 6.5)]
    assert rawstore.count("board_settings") == 0
    assert bs.values(B)["overridden"] == []
    assert bs.get(B, "WINSOR_K") == config.WINSOR_K


def test_a_reset_is_one_change(rawstore_tmp_db):
    """One user action, one record: the Reset's 'absent' rows share their run_id and run_at."""
    bs.save(B, {"WINSOR_K": 6.5, "MIN_OBS": 100, "BENCH_STALE_SESSIONS": 4})
    bs.reset(B)
    absent = [r for r in _changes("board_settings") if r["status"] == "absent"]
    assert len(absent) == 3
    assert len({(r["run_id"], r["run_at"]) for r in absent}) == 1


def test_a_reset_of_some_keys_leaves_the_others(rawstore_tmp_db):
    bs.save(B, {"WINSOR_K": 6.5, "MIN_OBS": 100})
    bs.reset(B, ["WINSOR_K", "NOT_A_SETTING"])
    assert bs.values(B)["overridden"] == ["MIN_OBS"]
    absent = [r["key"] for r in _changes("board_settings") if r["status"] == "absent"]
    assert absent == ["WINSOR_K"]


def test_a_reset_with_nothing_stored_appends_nothing(rawstore_tmp_db):
    bs.reset(B)
    assert _changes("board_settings") == []


def test_a_reset_touches_only_its_board(rawstore_tmp_db):
    rawstore.write_rows("board_settings", [{"board": "other_board", "key": "WINSOR_K", "value": 9.0}])
    bs.save(B, {"WINSOR_K": 6.5})
    bs.reset(B)
    assert rawstore.query("SELECT board, key FROM board_settings") == [{"board": "other_board", "key": "WINSOR_K"}]


def test_saving_the_default_appends_absent(rawstore_tmp_db):
    bs.save(B, {"WINSOR_K": 6.5})
    bs.save(B, {"WINSOR_K": config.WINSOR_K})
    assert [r["status"] for r in _changes("board_settings")] == ["present", "absent"]


# ── review round (B1, B2, S1) ────────────────────────────────────────────────────────────────────────────────────
def _race(fn, args):
    gate = threading.Barrier(len(args))
    results, errors = [], []

    def run(a):
        try:
            gate.wait(10)
            results.append(fn(*a))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(a,)) for a in args]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return results, errors


def _live_equals_history(name: str) -> bool:
    key = rawstore.MANIFEST[name]["key"]
    cols = key + rawstore.CHANGES[name]["track"]
    live = sorted(tuple(r[c] for c in cols) for r in rawstore.query(f"SELECT {', '.join(cols)} FROM {name}"))
    hist = sorted(tuple(r[c] for c in cols) for r in rawstore.changes_as_of(name, float("inf")))
    return live == hist


@pytest.mark.parametrize("n", [2, 7, 8])
def test_same_ticker_concurrent_toggles(rawstore_tmp_db, n):
    """Each click flips the state the previous one left: a read outside the transaction would flip one state twice."""
    results, errors = _race(rawstore.toggle_watchlist, [("AAA",)] * n)
    assert not errors, errors[:2]
    assert sorted(results) == sorted([True] * ((n + 1) // 2) + [False] * (n // 2)), results
    assert rawstore.load_watchlist() == ({"AAA"} if n % 2 else set())
    assert [r["status"] for r in _changes("watchlist", "ticker = ?", ["AAA"])] == (
        ["present", "absent"] * (n // 2) + (["present"] if n % 2 else []))
    assert _live_equals_history("watchlist")


def test_a_mixed_save_is_all_or_nothing(rawstore_tmp_db, monkeypatch):
    """One Save click that sets one key and returns another to its default writes both or neither."""
    bs.save(B, {"MIN_OBS": 100})
    real = rawstore._Tx.execute

    def failing(self, sql, params=None):
        if sql.lstrip().upper().startswith("DELETE"):
            raise RuntimeError("injected failure in the drop")
        return real(self, sql, params)

    monkeypatch.setattr(rawstore._Tx, "execute", failing)
    with pytest.raises(RuntimeError, match="injected"):
        bs.save(B, {"WINSOR_K": 7.0, "MIN_OBS": config.MIN_OBS})
    monkeypatch.setattr(rawstore._Tx, "execute", real)
    assert {r["key"]: r["value"] for r in rawstore.query("SELECT key, value FROM board_settings")} == {"MIN_OBS": 100.0}
    assert [(r["key"], r["status"]) for r in _changes("board_settings")] == [("MIN_OBS", "present")]


@pytest.mark.parametrize("keys", [None, ["WINSOR_K"]], ids=["full_reset", "one_key_reset"])
def test_a_reset_racing_a_save_keeps_history_equal_to_the_board(rawstore_tmp_db, keys):
    """DuckDB commits a DELETE and an upsert of the same row side by side; the writes must not interleave."""
    breaks = []
    for i in range(20):
        bs.save(B, {"WINSOR_K": 6.5})
        _, errors = _race(lambda f: f(), [(lambda: bs.reset(B, keys),), (lambda: bs.save(B, {"WINSOR_K": 7.0}),)])
        assert not errors, errors[:2]
        if not _live_equals_history("board_settings"):
            breaks.append(i)
            rawstore.sync_changes("test-heal")
        bs.reset(B)
    assert breaks == []
