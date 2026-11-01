"""Which lock holds which other one off (#309 follow-up): ``job_lock.WRITES`` and ``blocking_holders``.

Before this, a start gate asked ``holders()`` — *any* lock in the store dir — so the market builder was refused
while the Relatedness pilot held its own lock, although the two share no file. The gate now asks which stores each
family writes. The rule is deliberately coarse (one entry per store, not per file) and deliberately pessimistic:
an undeclared name shares everything.
"""
from __future__ import annotations

from RevenueChaser.core import job_lock


def test_the_two_stores_do_not_overlap():
    assert job_lock.writes_of("rcdata_build") == (job_lock.MARKET,)
    assert job_lock.writes_of("relatedness") == (job_lock.STORE,)
    assert not set(job_lock.writes_of("rcdata_build")) & set(job_lock.writes_of("relatedness"))


def test_a_tail_family_is_matched_by_its_prefix():
    assert job_lock.writes_of("nightly_inputs_spx") == (job_lock.STORE,)
    assert job_lock.writes_of("board_fetch_comovement") == (job_lock.STORE,)


def test_an_undeclared_name_shares_every_store():
    assert job_lock.writes_of("a_new_writer") == (job_lock.UNKNOWN,)
    assert job_lock.shares_files("rcdata_build", "a_new_writer")
    assert job_lock.shares_files("a_new_writer", "relatedness")


def test_the_builder_and_an_old_store_job_do_not_share_a_file():
    assert not job_lock.shares_files("rcdata_build", "relatedness")
    assert not job_lock.shares_files("rcdata_build", "nightly_inputs_spx")


def test_two_old_store_jobs_still_share_one():
    assert job_lock.shares_files("relatedness", "edgar_sync")


def test_a_second_run_of_one_family_always_shares():
    assert job_lock.shares_files("rcdata_build", "rcdata_build")
    assert job_lock.shares_files("a_new_writer", "a_new_writer")


def test_blocking_holders_drops_a_lock_on_another_store():
    with job_lock.acquire("relatedness", wait=0) as got:
        assert got
        assert "relatedness" in [n for n, _ in job_lock.holders()]
        assert "relatedness" not in [n for n, _ in job_lock.blocking_holders("rcdata_build")]
        assert "relatedness" in [n for n, _ in job_lock.blocking_holders("edgar_sync")]


def test_blocking_holders_keeps_a_second_builder_and_an_undeclared_one():
    with job_lock.acquire("rcdata_build", wait=0) as got:
        assert got
        assert "rcdata_build" in [n for n, _ in job_lock.blocking_holders("rcdata_build")]
    with job_lock.acquire("a_new_writer", wait=0) as got:
        assert got
        assert "a_new_writer" in [n for n, _ in job_lock.blocking_holders("rcdata_build")]
