"""#110: the two forecast writer CLIs share one skeleton (parser, lock, refusal, report, exit codes), so a change to
it is made once. Each CLI's own behaviour stays pinned by ``test_forecast_outcomes.py`` and
``test_forecast_baselines.py``; this file pins that the two are the same skeleton with a different writer.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import job_lock
from RevenueChaser.scripts import forecast_baselines, forecast_cli, forecast_outcomes

CLIS = [(forecast_outcomes, "forecast_outcomes", "forecast outcomes"),
        (forecast_baselines, "forecast_baselines", "forecast baselines")]


@pytest.mark.parametrize("mod, lock, what", CLIS)
def test_each_cli_is_the_shared_skeleton_with_its_own_writer_and_lock(mod, lock, what):
    assert mod.BUSY_EXIT == forecast_cli.BUSY_EXIT == 3
    assert mod.CLI.lock == lock and mod.CLI.prog == lock and mod.CLI.what == what


@pytest.mark.parametrize("mod, lock, what", CLIS)
def test_the_refusal_names_the_lock_and_writes_a_busy_report(mod, lock, what, tmp_path, capsys, monkeypatch):
    class Busy:
        def __enter__(self):
            return False

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(job_lock, "acquire", lambda name, wait=0: Busy())
    monkeypatch.setattr(job_lock, "holders", lambda: [(lock, 4242)])
    out = tmp_path / "r.json"
    assert mod.main(["--nightly", "--report", str(out)]) == 3
    assert capsys.readouterr().err.strip() == (
        f"{what}: another run (pid 4242) holds the {lock} lock; nothing was written.")
    assert out.read_text(encoding="utf-8") == '{"status": "busy", "holder_pid": 4242}'
