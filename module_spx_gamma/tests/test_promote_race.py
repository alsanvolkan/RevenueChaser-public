"""Promoting a staged raw day is one critical section, not a check followed by a rename.

CI run 40950317460 failed ``test_two_threads_staging_the_same_day_never_mix_their_files`` with
``OSError(39, 'Directory not empty')`` on a diff that does not touch this module, so the race is in the code and
not in the diff. The promotion used to read

    if final.exists(): shutil.rmtree(final)
    raw_dir.replace(final)

and on POSIX ``rename`` onto a directory that is not empty raises ``ENOTEMPTY``. Two threads staging the same day
interleave there: one removes the old day and the other promotes into the gap before the first renames. The test
below wedges them there on purpose, so the failure is deterministic rather than one CI run in some number.
"""
from __future__ import annotations

import errno
import threading

import pytest

from RevenueChaser.module_spx_gamma import snapshot


def _day(root, name: str, files: int = 2):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    for i in range(files):
        (d / f"f{i}.json").write_text("{}", encoding="utf-8")
    return d


def test_the_promotion_is_one_critical_section(tmp_path, monkeypatch):
    """Both threads promote a day of their own onto the same destination; neither may raise."""
    final = _day(tmp_path, "2026-10-29")
    staging = [_day(tmp_path, f".partial-{i}") for i in (1, 2)]
    reached = threading.Barrier(2, timeout=0.5)
    real_rmtree = snapshot.shutil.rmtree

    def rmtree(path, *a, **kw):
        real_rmtree(path, *a, **kw)
        # Inside the old section both threads would now have removed the destination and be about to rename onto
        # it. Hold them here together, which is exactly the interleaving CI hit.
        try:
            reached.wait()
        except threading.BrokenBarrierError:
            pass
    monkeypatch.setattr(snapshot.shutil, "rmtree", rmtree)

    errors: list[BaseException] = []

    def run(src):
        try:
            snapshot._promote(src, final)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(s,)) for s in staging]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert errors == [], errors
    assert final.exists() and sorted(p.name for p in final.glob("*.json")) == ["f0.json", "f1.json"]
    assert not list(tmp_path.glob(".partial-*")), "the staging folder that won is renamed away"


def test_a_destination_that_appears_between_the_removal_and_the_rename_is_still_promoted(tmp_path, monkeypatch):
    """The retry, without threads: the first rename raises ENOTEMPTY and the promotion still finishes."""
    final = _day(tmp_path, "2026-10-29")
    staging = _day(tmp_path, ".partial-1")
    calls: list[int] = []
    real_replace = type(staging).replace

    def replace(self, dest):
        calls.append(1)
        if len(calls) == 1:
            _day(tmp_path, "2026-10-29")          # someone put the day back
            raise OSError(errno.ENOTEMPTY, "Directory not empty")
        return real_replace(self, dest)
    monkeypatch.setattr(type(staging), "replace", replace)

    snapshot._promote(staging, final)
    assert len(calls) == 2, "one retry, not a loop"
    assert final.exists() and not staging.exists()


def test_an_error_that_is_not_the_race_is_not_retried(tmp_path, monkeypatch):
    final = _day(tmp_path, "2026-10-29")
    staging = _day(tmp_path, ".partial-1")
    calls: list[int] = []

    def replace(self, dest):
        calls.append(1)
        raise OSError(errno.EACCES, "Permission denied")
    monkeypatch.setattr(type(staging), "replace", replace)

    with pytest.raises(OSError) as exc:
        snapshot._promote(staging, final)
    assert exc.value.errno == errno.EACCES
    assert len(calls) == 1


def test_only_one_thread_is_inside_the_promotion_at_a_time(tmp_path, monkeypatch):
    """The retry alone would converge, but not before a thread removes a day the other has just promoted - the
    data loss #194 was about. So the section is held, and this counts how many threads are inside it."""
    final = _day(tmp_path, "2026-10-29")
    staging = [_day(tmp_path, f".partial-{i}") for i in (1, 2)]
    inside, peak, seat = 0, 0, threading.Lock()
    real_replace = type(final).replace

    def replace(self, dest):
        nonlocal inside, peak
        with seat:
            inside += 1
            peak = max(peak, inside)
        try:
            import time
            time.sleep(0.05)
            return real_replace(self, dest)
        finally:
            with seat:
                inside -= 1
    monkeypatch.setattr(type(final), "replace", replace)

    threads = [threading.Thread(target=snapshot._promote, args=(s, final)) for s in staging]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert peak == 1, f"{peak} threads were promoting at once"
