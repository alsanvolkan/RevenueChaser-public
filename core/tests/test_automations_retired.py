"""PR 5 step 4: the old data timer whose data the spine now builds (RC-Index-Lists, the index lists: the spine's
``index_membership``) shows "retired" on the Automations panel and stays disabled: the panel offers no Install,
Enable or Run for it, and the API refuses them. Disable and Remove stay possible."""
from __future__ import annotations

import pytest

from RevenueChaser.core import automations
from RevenueChaser.core.automations import FakeScheduler, TaskStatus
from RevenueChaser.module_settings import report

RETIRED = "RC-Index-Lists"


def test_the_retired_rows_name_what_replaced_them():
    assert [e.name for e in automations.REGISTRY if e.retired_by] == [RETIRED]
    assert "funds/index_holdings" in automations.entry(RETIRED).retired_by
    assert automations.static_row(automations.entry(RETIRED))["retired_by"] == automations.entry(RETIRED).retired_by
    assert automations.static_row(automations.entry("RC-Build"))["retired_by"] is None


@pytest.mark.parametrize("status, state", [(None, "retired"), (TaskStatus(state="disabled"), "retired"),
                                           (TaskStatus(state="ready"), "retired_enabled"),
                                           (TaskStatus(state="running"), "retired_enabled")])
def test_a_retired_row_reads_retired_and_says_so_if_it_is_still_enabled(status, state):
    sched = FakeScheduler(tasks={RETIRED: status} if status else {})
    assert automations.live_row(automations.entry(RETIRED), sched)["state"] == state


@pytest.mark.parametrize("action", ["install", "enable", "run"])
def test_install_enable_and_run_are_refused_for_a_retired_row(action):
    sched = FakeScheduler(tasks={RETIRED: TaskStatus(state="disabled")})
    with pytest.raises(automations.RetiredTask) as exc:
        automations.apply(RETIRED, action, sched)
    assert exc.value.http_status == 409 and sched.mutations == []


@pytest.mark.parametrize("action", ["disable", "remove"])
def test_disable_and_remove_stay_possible(action):
    sched = FakeScheduler(tasks={RETIRED: TaskStatus(state="ready")})
    automations.apply(RETIRED, action, sched)
    assert sched.mutations == [(action, RETIRED)]


def test_the_panel_knows_the_two_retired_states_and_offers_only_disable():
    js = report.AUTOMATIONS_JS
    assert "retired:['Retired'" in js and "retired_enabled:['Retired, still enabled'" in js
    assert "retired_enabled:[['disable','Disable']]" in js and "retired:[" not in js.split("AUTO_BUTTONS")[1][:300]
