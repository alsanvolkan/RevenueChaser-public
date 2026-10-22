"""#10: the ``--derive-only`` entry point — its documented command form and what it says when it runs.

S-E: run by path (``python scripts/migrate_sec13f_v12.py``) the script puts its own directory on ``sys.path``, so
the absolute ``from RevenueChaser.core import rawstore`` at the top dies with ``ModuleNotFoundError`` before any
check runs; only the ``python -m RevenueChaser.scripts.migrate_sec13f_v12`` form finds the package. The assertion
reads ``migrate.__doc__`` itself, not a copy of it, so the next edit cannot reintroduce the path form.

S-F: ``--derive-only`` is the branch the migration re-runs in a fresh process, so it has to speak for itself: a
refusal prints its reason and exits ``EXIT_REFUSED`` instead of a traceback, a success names the number of tickers
it rebuilt, an empty derive says so, and a child that refused is not handed the retry line that must refuse the same
way. ``recompute_totals`` itself is not exercised (that is the store's job); it is faked here.
"""
from __future__ import annotations

import types

from RevenueChaser.scripts import migrate_sec13f_v12 as migrate


def _command_lines(doc: str) -> list[str]:
    """The docstring lines that open a shell command for this script."""
    return [line.strip() for line in doc.splitlines()
            if "migrate_sec13f_v12" in line and line.strip().startswith("python ")]


def test_docstring_documents_only_the_module_form() -> None:
    commands = _command_lines(migrate.__doc__ or "")
    assert commands, "the docstring documents no runnable command; the assertion below would be vacuous"
    for line in commands:
        assert line.startswith("python -m RevenueChaser.scripts.migrate_sec13f_v12"), (
            f"documented command cannot import the package: {line!r}")


def _fake_totals(monkeypatch, result=None, raises: BaseException | None = None) -> None:
    def fake():
        if raises is not None:
            raise raises
        return result

    monkeypatch.setattr(migrate.engine, "recompute_totals", fake)


def test_derive_only_refusal_prints_the_reason_and_exits_refused(monkeypatch, capsys) -> None:
    _fake_totals(monkeypatch, raises=migrate.engine.DeriveRefused("orphans"))
    code = migrate.main(["--derive-only"])          # must not raise
    out = capsys.readouterr().out
    assert code == migrate.EXIT_REFUSED
    assert "refused:" in out and "orphans" in out
    assert "Traceback" not in out


def test_derive_only_success_names_the_ticker_count(monkeypatch, capsys) -> None:
    _fake_totals(monkeypatch, result={"AAA": {"2026-06-30": {"value": 1}},
                                     "BBB": {"2026-06-30": {"value": 2}}})
    code = migrate.main(["--derive-only"])
    out = capsys.readouterr().out
    assert code == migrate.EXIT_OK
    assert out.strip(), "a successful --derive-only run must print its result"
    assert "2" in out


def test_derive_only_empty_store_says_nothing_was_derived(monkeypatch, capsys) -> None:
    _fake_totals(monkeypatch, result={})
    code = migrate.main(["--derive-only"])
    out = capsys.readouterr().out
    assert code == migrate.EXIT_OK
    assert "nothing to derive" in out
    assert "0 ticker" not in out


def _fake_child(monkeypatch, returncode: int) -> None:
    monkeypatch.setattr(migrate.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=returncode))


def test_a_refusing_child_is_not_told_to_run_again(monkeypatch) -> None:
    _fake_child(monkeypatch, migrate.EXIT_REFUSED)
    try:
        migrate.derive_in_a_fresh_process(out=lambda *_: None)
    except migrate.DeriveSubprocessFailed as exc:
        message = str(exc)
    else:                                            # pragma: no cover - the failure is the point
        raise AssertionError("a refusing child must raise DeriveSubprocessFailed")
    assert "refused" in message
    assert migrate.derive_command() not in message
    assert "again with" not in message


def test_a_crashed_child_still_gets_the_pasteable_retry(monkeypatch) -> None:
    _fake_child(monkeypatch, 3)
    try:
        migrate.derive_in_a_fresh_process(out=lambda *_: None)
    except migrate.DeriveSubprocessFailed as exc:
        message = str(exc)
    else:                                            # pragma: no cover - the failure is the point
        raise AssertionError("a crashed child must raise DeriveSubprocessFailed")
    assert migrate.derive_command() in message
