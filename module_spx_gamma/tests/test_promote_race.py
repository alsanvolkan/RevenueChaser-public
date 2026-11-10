"""Promoting a staged raw day into the board file is one critical section (#194, CI run 40950317460).

The folder rename and its ``ENOTEMPTY`` race are gone: a day goes into ``boards/spx_gamma.duckdb`` in one
transaction (``store.put_raw_day``). What stays: two threads of one process staging the same day (a board Fetch's
backfill and the history pull) never interleave the "already stored?" check and the write, and never mix files.
"""
from __future__ import annotations

import threading
import time

from RevenueChaser.module_spx_gamma import snapshot, store
from .conftest import store_day


def _staged(root, name: str, tag: str, files: int = 2):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text('{"tag": "%s"}' % tag, encoding="utf-8")
    for i in range(files):
        (d / f"f{i}.json").write_text('{"tag": "%s"}' % tag, encoding="utf-8")
    return d


def test_two_threads_promoting_the_same_day_never_mix_their_files(tmp_path):
    staging = [_staged(tmp_path, f".partial-{i}", str(i)) for i in (1, 2)]
    errors: list[BaseException] = []

    def run(src):
        try:
            snapshot._promote(src, "2026-10-29")
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(s,)) for s in staging]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert errors == [], errors
    files = store.raw_files("2026-10-29")
    assert sorted(files) == ["f0.json", "f1.json"]
    assert len(set(files.values())) == 1, "one thread's files only"
    assert store.raw_manifest("2026-10-29")["tag"] == next(iter(set(files.values())))[9:10]


def test_only_one_thread_is_inside_the_promotion_at_a_time(tmp_path, monkeypatch):
    staging = [_staged(tmp_path, f".partial-{i}", str(i)) for i in (1, 2)]
    inside, peak, seat = 0, 0, threading.Lock()
    real_put = store.put_raw_day

    def put(*a, **k):
        nonlocal inside, peak
        with seat:
            inside += 1
            peak = max(peak, inside)
        try:
            time.sleep(0.05)
            return real_put(*a, **k)
        finally:
            with seat:
                inside -= 1
    monkeypatch.setattr(store, "put_raw_day", put)

    threads = [threading.Thread(target=snapshot._promote, args=(s, "2026-10-29")) for s in staging]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert peak == 1, f"{peak} threads were promoting at once"


def test_a_complete_day_stored_meanwhile_is_kept(tmp_path):
    """The review's m5: a history pull's staged copy never replaces a complete day stored meanwhile."""
    store_day("2026-10-28", {"tag": "fetch"}, {"f0.json": '{"tag": "fetch"}'})
    staged = _staged(tmp_path, ".partial-1", "pull")
    assert snapshot._promote(staged, "2026-10-28", keep_stored=True) is False
    assert store.raw_manifest("2026-10-28")["tag"] == "fetch"
    assert store.raw_files("2026-10-28") == {"f0.json": '{"tag": "fetch"}'}


def test_a_fetch_replaces_the_stored_day_whole(tmp_path):
    store_day("2026-10-28", {"tag": "old"}, {"old.json": "{}", "f0.json": "{}"})
    snapshot._promote(_staged(tmp_path, ".partial-1", "new", files=1), "2026-10-28")
    assert store.raw_files("2026-10-28") == {"f0.json": '{"tag": "new"}'}
