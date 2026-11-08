"""scripts/red_on_base.py: a test this PR adds that already passes on the base proves nothing about the change.

The check runs the PR's new tests against the merge-base. Every new test id that passes there is listed and the
check fails. A new test that fails or cannot even be collected on the base is red-first: that is the point.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from RevenueChaser.scripts import red_on_base as mod


@pytest.mark.parametrize("path,want", [
    ("core/tests/test_x.py", True), ("test_top.py", True), ("module_a/tests/test_b_c.py", True),
    ("core/tests/conftest.py", False), ("core/x.py", False), ("core/tests/helpers_test.py", False),
    ("docs/test_notes.md", False), ("core/tests/test_x.pyc", False),
])
def test_only_test_modules_count(path, want):
    assert mod.is_test_file(path) is want


def test_new_ids_are_the_head_ids_the_base_file_did_not_have():
    head = ["a.py::test_old", "a.py::test_new", "a.py::test_p[1]", "a.py::test_p[2]"]
    base = ["a.py::test_old", "a.py::test_p[1]"]
    assert mod.new_ids(head, base) == ["a.py::test_new", "a.py::test_p[2]"]


def test_collected_ids_are_read_from_collect_only_output():
    out = "a.py::test_one\na.py::Cls::test_two[x-1]\n\n3 tests collected in 0.01s\n"
    assert mod.parse_collected(out) == ["a.py::test_one", "a.py::Cls::test_two[x-1]"]


def test_junit_gives_the_ids_that_passed():
    xml = textwrap.dedent("""\
        <testsuites><testsuite>
          <testcase classname="pkg.a" name="test_pass" file="pkg/a.py"/>
          <testcase classname="pkg.a" name="test_fail" file="pkg/a.py"><failure message="x"/></testcase>
          <testcase classname="pkg.a" name="test_err" file="pkg/a.py"><error message="x"/></testcase>
          <testcase classname="pkg.a" name="test_skip" file="pkg/a.py"><skipped message="x"/></testcase>
          <testcase classname="pkg.a.Cls" name="test_m[1]" file="pkg/a.py"/>
        </testsuite></testsuites>""")
    assert mod.passed_in_junit(xml) == {("pkg.a", "test_pass"), ("pkg.a.Cls", "test_m[1]")}


@pytest.mark.parametrize("node,key", [("pkg/a.py::test_x", ("pkg.a", "test_x")),
                                      ("pkg/a.py::Cls::test_m[1-b]", ("pkg.a.Cls", "test_m[1-b]")),
                                      ("test_top.py::test_y", ("test_top", "test_y"))])
def test_a_node_id_maps_to_the_key_junit_writes(node, key):
    assert mod._id_key(node) == key


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """A tiny git repo: main has calc.add; the PR fixes calc.sub and adds tests."""
    r = tmp_path / "proj"
    r.mkdir()
    (r / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (r / "calc.py").write_text("def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a + b\n", encoding="utf-8")
    (r / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
                                    encoding="utf-8")
    _git(r, "init", "-q", "-b", "main")
    _git(r, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(r, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")
    _git(r, "checkout", "-q", "-b", "pr")
    return r


def _commit(r, files):
    for name, text in files.items():
        (r / name).write_text(text, encoding="utf-8")
    _git(r, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(r, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "pr")


def _run(r, tmp_path):
    return mod.main(["main", "--repo", str(r), "--work", str(tmp_path / "work"), "--python", sys.executable])


def test_a_new_test_that_fails_on_the_base_is_red_first(repo, tmp_path, capsys):
    _commit(repo, {"calc.py": "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n",
                   "test_calc.py": "from calc import add, sub\n\ndef test_add():\n    assert add(1, 2) == 3\n\n"
                                   "def test_sub():\n    assert sub(3, 1) == 2\n"})
    assert _run(repo, tmp_path) == 0
    assert "test_calc.py::test_sub" in capsys.readouterr().out


def test_a_new_test_that_passes_on_the_base_fails_the_check(repo, tmp_path, capsys):
    _commit(repo, {"calc.py": "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n",
                   "test_calc.py": "from calc import add, sub\n\ndef test_add():\n    assert add(1, 2) == 3\n\n"
                                   "def test_sub():\n    assert sub(3, 1) == 2\n\n"
                                   "def test_add_again():\n    assert add(2, 2) == 4\n"})
    assert _run(repo, tmp_path) == 1
    out = capsys.readouterr().out
    assert "test_calc.py::test_add_again" in out and "prove nothing" in out
    assert "test_sub" not in out.split("prove nothing", 1)[1]


def test_a_new_test_file_that_cannot_import_on_the_base_is_red_first(repo, tmp_path):
    _commit(repo, {"mul.py": "def mul(a, b):\n    return a * b\n",
                   "test_mul.py": "from mul import mul\n\ndef test_mul():\n    assert mul(2, 3) == 6\n"})
    assert _run(repo, tmp_path) == 0


def test_no_new_tests_passes_and_says_so(repo, tmp_path, capsys):
    _commit(repo, {"calc.py": "def add(a, b):\n    return b + a\n\ndef sub(a, b):\n    return a + b\n"})
    assert _run(repo, tmp_path) == 0
    assert "no new tests" in capsys.readouterr().out


def test_the_base_checkout_is_left_untouched(repo, tmp_path):
    _commit(repo, {"test_mul.py": "def test_x():\n    assert True\n"})
    _run(repo, tmp_path)
    assert subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True,
                          text=True).stdout == ""
    assert subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True,
                          text=True).stdout.count("\n") == 1


# --- review of 1449059 (Opus, 8 Nov 2026) ---

def test_a_passing_test_beside_an_uncollectable_file_is_still_flagged(repo, tmp_path, capsys):
    """B1: one file that cannot import on the base must not turn every other new test red."""
    _commit(repo, {"mul.py": "def mul(a, b):\n    return a * b\n",
                   "test_mul.py": "from mul import mul\n\ndef test_mul():\n    assert mul(2, 3) == 6\n",
                   "test_plain.py": "def test_plain():\n    assert True\n"})
    assert _run(repo, tmp_path) == 1
    out = capsys.readouterr().out
    assert "test_plain.py::test_plain" in out.split("prove nothing", 1)[1]


def test_a_pytest_that_reports_nothing_is_a_failure_not_all_red(repo, tmp_path, monkeypatch, capsys):
    """B2: no junit (pytest crashed or got a usage error) is exit 2, never "every test red"."""
    _commit(repo, {"test_plain.py": "def test_plain():\n    assert True\n"})
    real = mod.subprocess.run

    def fake(cmd, *a, **kw):
        if "--junitxml" in " ".join(map(str, cmd)):
            return subprocess.CompletedProcess(cmd, 4, "", "usage error")
        return real(cmd, *a, **kw)

    monkeypatch.setattr(mod.subprocess, "run", fake)
    assert _run(repo, tmp_path) == 2
    assert "pytest" in capsys.readouterr().err


@pytest.mark.parametrize("node,key", [("a.py::test_p[a::b]", ("a", "test_p[a::b]")),
                                      ("a.py::Cls::test_q[pkg/a.py::Cls]", ("a.Cls", "test_q[pkg/a.py::Cls]"))])
def test_a_parameter_with_double_colons_keeps_its_name(node, key):
    """M1: junit keeps the bracketed parameters whole."""
    assert mod._id_key(node) == key


def test_a_passing_parametrized_id_with_double_colons_is_flagged(repo, tmp_path, capsys):
    _commit(repo, {"test_p.py": "import pytest\n\n@pytest.mark.parametrize('x', ['a::b'])\n"
                                "def test_p(x):\n    assert x\n"})
    assert _run(repo, tmp_path) == 1
    assert "test_p.py::test_p[a::b]" in capsys.readouterr().out.split("prove nothing", 1)[1]


def test_a_renamed_test_file_still_has_its_new_tests_checked(repo, tmp_path, capsys):
    """M2: a rename keeps the old ids as old; only the added test is new."""
    _git(repo, "mv", "test_calc.py", "test_calc2.py")
    _commit(repo, {"test_calc2.py": "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n\n"
                                    "def test_more():\n    assert add(0, 0) == 0\n"})
    assert _run(repo, tmp_path) == 1
    out = capsys.readouterr().out
    assert "test_calc2.py::test_more" in out and "test_calc2.py::test_add" not in out


def test_a_scratch_path_that_already_exists_is_refused(repo, tmp_path):
    """m3: never point the worktree (and its forced removal) at a folder that is already there."""
    _commit(repo, {"test_plain.py": "def test_plain():\n    assert True\n"})
    work = tmp_path / "work"
    (work / repo.name).mkdir(parents=True)
    (work / repo.name / "keep.txt").write_text("mine", encoding="utf-8")
    assert mod.main(["main", "--repo", str(repo), "--work", str(work), "--python", sys.executable]) == 2
    assert (work / repo.name / "keep.txt").read_text(encoding="utf-8") == "mine"


def test_a_test_file_with_a_space_in_its_path_is_checked(repo, tmp_path, capsys):
    """m4: paths are read NUL-separated."""
    (repo / "sub dir").mkdir()
    _commit(repo, {"sub dir/test_s.py": "def test_s():\n    assert True\n"})
    assert _run(repo, tmp_path) == 1
    assert "sub dir/test_s.py::test_s" in capsys.readouterr().out
