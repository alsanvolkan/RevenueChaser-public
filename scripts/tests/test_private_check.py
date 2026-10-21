"""Private-material guard: tracked files carry no secret, personal path, personal email or private folder.

The shared repo has more than one developer, so anything committed is visible to all of them. Owner-only notes live
in ``.private/`` (gitignored). ``scripts/private_check.py`` is the one checker: the pre-commit hook
(``scripts/hooks/pre-commit``) runs it on staged files and ``test_tracked_files_hold_no_private_material`` runs it on
every tracked file, so a slip is caught in the full suite whoever pushed it.

Synthetic secrets, paths and addresses are joined at runtime, so this file itself holds none of them.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from RevenueChaser.scripts import private_check as pc

_REPO = Path(__file__).resolve().parents[2]


def _kinds(path: str, text: str) -> set[str]:
    return {f.kind for f in pc.check_text(path, text)}


# --- secrets -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind,value", [
    ("anthropic_key", "sk-ant-" + "api03-" + "A1b2" * 10),
    ("openai_style_key", "sk-" + "a1B2" * 10),
    ("xai_key", "xai-" + "Q9w8" * 12),
    ("github_token", "ghp_" + "Z9" * 18),
    ("aws_key", "AKIA" + "ABCDEFGHIJKLMNOP"),
    ("google_api_key", "AIza" + "S" * 35),
    ("slack_token", "xoxb-" + "1234567890-abcdef"),
    ("discord_token", "M" + "T" * 25 + "." + "Gxabcd" + "." + "k" * 30),
    ("discord_webhook", "https://discord.com/api/" + "webhooks/" + "1" * 18 + "/" + "tok_" * 8),
    ("private_key", "-----BEGIN " + "RSA PRIVATE KEY-----"),
])
def test_secret_patterns_are_found(kind, value):
    assert kind in _kinds("mod/x.py", f'x = "{value}"\n')


def test_a_known_key_name_with_a_literal_value_is_found():
    assert "key_assignment" in _kinds(".env.example", "UNUSUAL_WHALES_" + "API_KEY=" + "3f9c" * 6 + "\n")
    assert "key_assignment" in _kinds("s.ps1", '$env:DEEPSEEK_' + 'API_KEY = "' + "9a8b" * 8 + '"\n')


@pytest.mark.parametrize("line", [
    "DISCORD_TOKEN=\nRC_CLAUDE_OAUTH_FILE=\n",                       # empty values: the next line is not a value
    'UW_API_KEY = os.environ.get("UNUSUAL_WHALES_API_KEY", "")\n',      # read from the environment
    'DISCORD_TOKEN = "SENTINEL-TOKEN-1b7e"\n',                          # a test sentinel
    'echo "UNUSUAL_WHALES_API_KEY=your-key-here" > .env\n',             # a README placeholder
    "token = job_control.StopToken()\n",                                # code, not a value
    'assert header == "Bearer sentinel-claude-token"\n',
])
def test_empty_placeholder_and_code_values_pass(line):
    assert pc.check_text("mod/x.py", line) == []


# --- personal paths, addresses and account ids -------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "C:\\Users\\" + "jdoe\\AppData\\Local\\Temp",
    "C:/Users/" + "jdoe/code",
    "/Users/" + "jdoe/code",
    "/home/" + "jdoe/rc",
])
def test_a_home_path_with_a_real_user_name_is_found(text):
    assert "personal_path" in _kinds("docs/a.md", f"see {text}\n")


@pytest.mark.parametrize("text", [
    "C:\\Users\\" + "x\\AppData\\RC246_DRY\\.store",
    "C:\\Users\\" + "<user>\\AppData",
    "/home/" + "user/RevenueChaser",
    "%USERPROFILE%\\AppData",
    "C:/projects/Github/RevenueChaser",
])
def test_generic_paths_pass(text):
    assert pc.check_text("docs/a.md", f"see {text}\n") == []


def test_a_personal_mailbox_is_found():
    assert "personal_email" in _kinds("s.py", "UA = 'RC research jane.doe" + "@gmail.com'\n")
    assert "personal_email" in _kinds("s.py", "mail j.doe" + "@outlook.com today\n")


@pytest.mark.parametrize("text", [
    "dev" + "@unusualwhales.com",
    "jane" + "@example.com",
    "tests" + "@revenuechaser.test",
    "1+someone" + "@users.noreply.github.com",
    "loughranmcdonald" + "@gmail.com",                    # the public contact of the Loughran-McDonald dictionary
])
def test_project_and_public_addresses_pass(text):
    assert pc.check_text("s.py", f"x = '{text}'\n") == []


def test_a_brokerage_account_id_is_found():
    assert "account_id" in _kinds("n.md", "account " + "U" + "1234567" + " positions\n")
    assert "account_id" in _kinds("n.md", "paper " + "DU" + "7654321\n")


# --- paths that must never be tracked ----------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    ".env", "sub/.env", ".env.local", ".store/raw.duckdb", "x/raw.duckdb", "a.duckdb.wal", "s.sqlite3",
    "core/__pycache__/m.cpython-314.pyc", ".private/NOTES.md", ".cache/research/1/a.csv", "k.pem", "auth.json",
    "discord_media/a.png", "module_x/.cache/a.json",
])
def test_private_paths_are_refused(path):
    assert pc.check_path(path) is not None


@pytest.mark.parametrize("path", [
    ".env.example", ".hermes/plans_claude/README.md", "core/credentials.py", "docs/data_architecture.md",
    "core/tests/fixtures/claude_oauth_usage.json",
])
def test_shared_paths_pass(path):
    assert pc.check_path(path) is None


# --- reporting ---------------------------------------------------------------------------------------------------

def test_a_finding_names_file_line_and_kind_but_never_the_value():
    secret = "sk-ant-" + "api03-" + "Q7" * 20
    [f] = pc.check_text("a/b.py", "x = 1\nkey = '" + secret + "'\n")
    assert (f.path, f.line, f.kind) == ("a/b.py", 2, "anthropic_key")
    assert secret[10:] not in pc.format_finding(f)
    assert secret[10:] not in repr(f)


def test_binary_content_is_skipped():
    assert pc.check_text("img.png", "\x00PNG sk-ant-" + "api03-" + "Q7" * 20) == []


# --- the two entry points ----------------------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def scratch_repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    _git(tmp_path, "init", "-q")
    return tmp_path


def test_staged_mode_checks_the_index_not_the_working_tree(scratch_repo):
    f = scratch_repo / "notes.md"
    f.write_text("contact jane.doe" + "@gmail.com\n", encoding="utf-8")
    _git(scratch_repo, "add", "notes.md")
    f.write_text("clean now\n", encoding="utf-8")                  # the commit would still carry the address
    assert pc.main(["--staged"], repo=scratch_repo) == 1

    _git(scratch_repo, "add", "notes.md")
    assert pc.main(["--staged"], repo=scratch_repo) == 0


def test_staged_mode_refuses_a_private_path(scratch_repo):
    (scratch_repo / ".env").write_text("X=1\n", encoding="utf-8")
    _git(scratch_repo, "add", "-f", ".env")
    assert pc.main(["--staged"], repo=scratch_repo) == 1


def test_the_pre_commit_hook_blocks_a_commit_with_private_material(scratch_repo):
    if shutil.which("sh") is None:
        pytest.skip("no POSIX shell to run the hook")
    hook = _REPO / "scripts" / "hooks" / "pre-commit"
    (scratch_repo / "a.md").write_text("see /home/" + "jdoe/rc\n", encoding="utf-8")
    _git(scratch_repo, "add", "a.md")
    blocked = subprocess.run(["sh", str(hook)], cwd=scratch_repo, capture_output=True, text=True)
    assert blocked.returncode == 1, blocked.stdout + blocked.stderr
    assert "a.md:1: personal_path" in blocked.stdout + blocked.stderr

    (scratch_repo / "a.md").write_text("see the docs\n", encoding="utf-8")
    _git(scratch_repo, "add", "a.md")
    passed = subprocess.run(["sh", str(hook)], cwd=scratch_repo, capture_output=True, text=True)
    assert passed.returncode == 0, passed.stdout + passed.stderr


def test_tracked_files_hold_no_private_material():
    try:
        files = pc.tracked_files(_REPO)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    findings = pc.scan_files(_REPO, files)
    assert not findings, "private material in tracked files:\n" + "\n".join(map(pc.format_finding, findings))
