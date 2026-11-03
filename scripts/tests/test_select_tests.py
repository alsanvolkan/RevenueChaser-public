"""scripts/select_tests.py: which tests a PR's changed files can reach, or FULL when that is not safe to say.

The selection follows imports (direct and transitive, top-level or inside a function, and module names
written as strings: the e2e dashboard subprocess and the import_module registries). Every case that cannot
be answered from that graph runs the full suite.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from RevenueChaser.scripts import select_tests as mod

_REPO = Path(__file__).resolve().parents[2]


def _runs(sel, test: str) -> bool:
    return sel.mode == "FULL" or test in sel.unit


def test_rcdata_ops_reaches_the_e2e_tests_that_broke_on_pr_381():
    """PR #381 changed rcdata/ops.py and these three e2e tests failed; none of them imports ops directly."""
    reached = mod.reached_tests(["rcdata/ops.py"])
    for name in ("test_settings_newdata.py", "test_spx_gamma_rcdata.py", "test_watchlist_pair.py"):
        assert f"module_testing/e2e/{name}" in reached, name
    assert mod.select(["rcdata/ops.py"]).mode == "FULL"  # the root conftest imports ops


def test_an_e2e_test_that_reaches_the_app_only_through_the_dashboard_fixture_is_selected():
    """test_board_oipd imports opd_math only; the board code runs in the dashboard the e2e conftest starts."""
    sel = mod.select(["module_oipd/report.py"])
    assert _runs(sel, "module_testing/e2e/test_board_oipd.py"), sel.reason
    assert "module_testing/e2e/test_board_oipd.py" in sel.e2e or sel.mode == "FULL"


def test_a_change_reaching_a_conftest_selects_every_test_below_it(tmp_path):
    root = _mini_repo(tmp_path, {
        "pkg/__init__.py": "", "pkg/fix.py": "", "pkg/other.py": "",
        "pkg/tests/conftest.py": "from RevenueChaser.pkg import fix\n",
        "pkg/tests/test_x.py": "", "pkg/tests/deep/test_y.py": "from RevenueChaser.pkg import other\n",
        "pkg2/tests/test_z.py": "",
    })
    sel = mod.select(["pkg/fix.py"], root=root)
    assert (sel.mode, sel.unit) == ("SELECTIVE", ["pkg/tests/deep/test_y.py", "pkg/tests/test_x.py"])


def test_a_change_reaching_the_root_conftest_runs_the_full_suite(tmp_path):
    root = _mini_repo(tmp_path, {
        "conftest.py": "def f():\n    from RevenueChaser.pkg import a\n",
        "pkg/__init__.py": "", "pkg/a.py": "", "pkg/tests/test_a.py": "from RevenueChaser.pkg import a\n",
    })
    assert mod.select(["pkg/a.py"], root=root).mode == "FULL"


def test_a_narrow_change_runs_fewer_tests_than_the_full_suite():
    sel = mod.select(["module_relatedness/naming.py"])
    assert sel.mode == "SELECTIVE", sel.reason
    assert "module_relatedness/tests/test_naming.py" in sel.unit
    assert len(sel.unit) < len(mod.all_tests(_REPO)) / 10


@pytest.mark.parametrize("doc, reader", [
    ("docs/data_retrieval.md", "core/tests/test_doc_constants.py"),
    ("docs/rules/00_overview.md", "module_earnings_radar/tests/test_confidence_wording_df06.py"),
    ("docs/rules/redesign.md", "module_earnings_radar/tests/test_cleanup_199.py"),
    ("docs/data_architecture.md", "core/tests/test_store_schema_refuse.py"),
])
def test_a_doc_a_test_reads_selects_that_test(doc, reader):
    assert _runs(mod.select([doc]), reader)


def test_a_markdown_file_nothing_reads_selects_nothing(tmp_path):
    root = _mini_repo(tmp_path, {"pkg/a.py": "", "pkg/tests/test_a.py": "", "docs/notes.md": "x"})
    sel = mod.select(["docs/notes.md"], root=root)
    assert (sel.mode, sel.unit, sel.e2e) == ("SELECTIVE", [], [])


@pytest.mark.parametrize("path", [
    "conftest.py", "module_testing/e2e/conftest.py", "requirements.txt", "pytest.ini",
    ".github/workflows/tests.yml", "scripts/select_tests.py",
    "rcdata/tests/fixtures/cftc_tff.json", "module_testing/e2e/harness/provider_fixtures/unusual_whales/option_trades.json",
])
def test_shared_config_and_fixtures_run_the_full_suite(path):
    assert mod.select([path]).mode == "FULL"


def test_a_deleted_module_runs_the_full_suite(tmp_path):
    """Its importers are not in the graph any more, so the tests that still import it cannot be found."""
    root = _mini_repo(tmp_path, {
        "pkg/__init__.py": "", "pkg/a.py": "", "pkg/tests/test_a.py": "from RevenueChaser.pkg import a\n",
        "pkg/tests/test_gone.py": "from RevenueChaser.pkg import gone\n",
    })
    assert mod.select(["pkg/gone.py", "pkg/a.py"], root=root).mode == "FULL"


def test_a_data_file_maps_to_the_tests_reaching_a_module_that_names_it(tmp_path):
    root = _mini_repo(tmp_path, {
        "pkg/__init__.py": "", "pkg/a.py": 'PATH = "data/table.csv"\n',
        "pkg/tests/test_a.py": "from RevenueChaser.pkg import a\n",
        "pkg/data/table.csv": "x\n", "pkg/data/orphan.csv": "x\n",
        "templates/base.html": "", "templates/page.html": '{% extends "base.html" %}',
        "pkg/b.py": 'PAGE = "page.html"\n', "pkg/tests/test_b.py": "from RevenueChaser.pkg import b\n",
    })
    assert mod.select(["pkg/data/table.csv"], root=root).unit == ["pkg/tests/test_a.py"]
    assert mod.select(["templates/base.html"], root=root).unit == ["pkg/tests/test_b.py"]
    assert mod.select(["pkg/data/orphan.csv"], root=root).mode == "FULL"


def _mini_repo(tmp_path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def test_code_no_test_reaches_runs_the_full_suite(tmp_path):
    root = _mini_repo(tmp_path, {
        "pkg/__init__.py": "", "pkg/a.py": "", "pkg/lonely.py": "",
        "pkg/tests/__init__.py": "", "pkg/tests/test_a.py": "from RevenueChaser.pkg import a\n",
    })
    assert mod.select(["pkg/a.py"], root=root).unit == ["pkg/tests/test_a.py"]
    assert mod.select(["pkg/lonely.py"], root=root).mode == "FULL"


def test_a_file_that_does_not_parse_runs_the_full_suite(tmp_path):
    root = _mini_repo(tmp_path, {
        "pkg/__init__.py": "", "pkg/a.py": "def (:\n",
        "pkg/tests/test_a.py": "from RevenueChaser.pkg import a\n",
    })
    assert mod.select(["pkg/a.py"], root=root).mode == "FULL"
    assert mod.select(["pkg/tests/test_a.py"], root=root).mode == "FULL"


def test_importing_a_submodule_runs_its_packages(tmp_path):
    root = _mini_repo(tmp_path, {
        "pkg/__init__.py": "X = 1\n", "pkg/sub/__init__.py": "", "pkg/sub/leaf.py": "",
        "pkg/tests/test_leaf.py": "from RevenueChaser.pkg.sub import leaf\n",
    })
    assert mod.select(["pkg/__init__.py"], root=root).unit == ["pkg/tests/test_leaf.py"]


def test_relative_lazy_and_string_imports_are_edges(tmp_path):
    root = _mini_repo(tmp_path, {
        "pkg/__init__.py": "", "pkg/a.py": "", "pkg/b.py": "", "pkg/c.py": "", "pkg/entry.py": "",
        "pkg/hop.py": "from . import a\n",
        "pkg/tests/__init__.py": "",
        "pkg/tests/test_rel.py": "from .. import hop\n",
        "pkg/tests/test_lazy.py": "def test():\n    from RevenueChaser.pkg import b\n",
        "pkg/tests/test_str.py": 'TARGET = "RevenueChaser.pkg.entry"\nREG = "pkg.c:run"\n',
    })
    assert mod.select(["pkg/a.py"], root=root).unit == ["pkg/tests/test_rel.py"]
    assert mod.select(["pkg/b.py"], root=root).unit == ["pkg/tests/test_lazy.py"]
    assert mod.select(["pkg/entry.py"], root=root).unit == ["pkg/tests/test_str.py"]
    assert mod.select(["pkg/c.py"], root=root).unit == ["pkg/tests/test_str.py"]


def test_every_dynamic_import_outside_tests_names_its_targets_as_string_literals():
    """The string-literal edges cover these three registries; a new import_module site must be checked the same way."""
    sites = set()
    for p in _REPO.rglob("*.py"):
        rel = p.relative_to(_REPO).as_posix()
        if {".git", ".store", ".cache"} & set(p.parts) or "/tests/" in f"/{rel}" or p.name == "conftest.py":
            continue
        if re.search(r"\bimport_module\(|__import__\(", p.read_text(encoding="utf-8")):
            sites.add(rel)
    assert sites == {"core/datasets.py", "core/nightly_inputs.py", "module_relatedness/writers.py"}


def test_the_workflow_names_each_run_full_or_selective():
    wf = (_REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert "name: unit (${{ needs.plan.outputs.mode }})" in wf
    assert "name: e2e (${{ needs.plan.outputs.mode }})" in wf
    assert "scripts/select_tests.py" in wf
    assert "full-ci" in wf


def test_the_merge_gate_check_is_green_only_for_a_full_run_that_passed():
    """Review of #383: without it every check of an unlabelled PR is green and reads like the gate."""
    wf = (_REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    gate = wf.split("\n  merge-gate:\n", 1)[1]
    assert "needs: [plan, unit, e2e]" in gate
    assert "if: always() && needs.plan.result == 'success'" in gate
    script = "\n".join(line[10:] for line in gate.split("run: |\n", 1)[1].splitlines() if line.startswith(" " * 10))
    for (mode, unit, e2e), code in {("FULL", "success", "success"): 0, ("SELECTIVE", "success", "success"): 1,
                                    ("FULL", "success", "failure"): 1, ("FULL", "cancelled", "success"): 1}.items():
        env = {**os.environ, "MODE": mode, "UNIT": unit, "E2E": e2e}
        assert subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True).returncode == code, mode
