"""``export_html``'s page must land in the test's own folder, not the process root.

`report.export_html` names its file from a second-resolution timestamp
(``earnings/revenue_chaser_%Y%m%d_%H%M%S.html``) and then the caller reads that path back. Nine test
files call it, `--dist loadfile` hands each file to a different xdist worker, and the data-root
redirect is one folder per process — so two workers that render within the same second write and read
the same file, and whichever wrote last decides what both of them read. That is the CI failure of
`test_the_dollar_volume_help_names_the_premium_it_reads` on 2026-10-30 (run 41034036608): it read a
page another worker had rendered without ``filter_stats``, so the gates line was simply not in it.

The same collision has been fixed twice before, for Asymmetric Growth and SEC 13F, by redirecting the
path in ``isolate_board_artifacts``. This is the third one.
"""
from __future__ import annotations

from pathlib import Path

from RevenueChaser.module_earnings_radar import report


def test_the_earnings_page_is_written_inside_this_tests_own_folder(isolate_board_artifacts):
    assert Path(report.OUTPUT_DIR) == Path(isolate_board_artifacts), (
        "report.OUTPUT_DIR is the process root, so two xdist workers rendering in the same second "
        "share one page file")


def test_two_renders_in_the_same_second_are_still_this_tests_own_page(isolate_board_artifacts):
    from RevenueChaser.module_earnings_radar.tests.test_labels_e3 import _card

    stats = {"total": 1, "passed": 1, "bypassed": 0, "filtered": 0, "by_reason": {}, "excluded": []}
    page = report.export_html([_card()], [], {}, filter_stats=stats)
    assert Path(isolate_board_artifacts) in Path(page).parents
    assert "$Vol≥$50K (call + put premium)" in page.read_text(encoding="utf-8")
