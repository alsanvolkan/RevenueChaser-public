"""#111: every test gets its own board artifact paths, both copies of each pair, the way it gets its own store.

The data-root redirect points at one temp folder per process, so before this a board payload one test saved was
what the next test's Refresh read, and a Refresh test could pass on that leftover. The two tests below run in file
order in one process: the second fails if the first one's file is still where the second one looks.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

PAIRS = [("FUNDAMENTALS", "module_fundamentals"), ("SWING", "module_swing_potential"),
         ("COMOVEMENT", "module_comovement"), ("VALUE_CHAIN", "module_value_chain"),
         ("RELATEDNESS", "module_relatedness"), ("OIPD", "module_oipd")]
SEEN: set = set()


def _copies(prefix, module, kind):
    runners = importlib.import_module("RevenueChaser.app.runners")
    cfg = importlib.import_module(f"RevenueChaser.{module}.config")
    return getattr(runners, f"{prefix}_{kind}"), getattr(cfg, f"{prefix}_{kind}")


@pytest.mark.parametrize("prefix, module", PAIRS)
@pytest.mark.parametrize("kind", ["LATEST", "JSON"])
def test_both_copies_of_each_artifact_path_point_into_this_tests_folder(prefix, module, kind,
                                                                         isolate_board_artifacts):
    from RevenueChaser.core import config
    in_runners, in_config = _copies(prefix, module, kind)
    assert in_runners == in_config, "a test that patches one copy must not leave the other shared"
    assert isolate_board_artifacts in in_runners.parents
    assert config.OUTPUT_DIR in in_runners.parents, "paths stay under the cache root (relative_to and the layout guard)"
    assert isolate_board_artifacts not in SEEN, "every test gets a folder of its own"
    SEEN.add(isolate_board_artifacts)


def test_a_fetch_leaves_a_payload():
    for prefix, module in PAIRS:
        for path in _copies(prefix, module, "JSON"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")


def test_the_next_test_does_not_see_it():
    for prefix, module in PAIRS:
        for path in _copies(prefix, module, "JSON"):
            assert not path.exists(), f"{path} is an earlier test's leftover"


def test_the_sec_13f_page_is_written_and_read_in_this_tests_folder(isolate_board_artifacts):
    """#257: seven app test files render ``sec_13f_latest.html``; in the shared process root two xdist workers wrote,
    read and unlinked one file (``FileNotFoundError``, 28 Oct 2026). The runner reads where the report writes."""
    runners = importlib.import_module("RevenueChaser.app.runners")
    report = importlib.import_module("RevenueChaser.module_sec_13f.report")
    assert runners.SEC_13F_LATEST.is_relative_to(isolate_board_artifacts)
    assert Path(report.OUTPUT_DIR) / "sec_13f_latest.html" == runners.SEC_13F_LATEST
