"""Docs that state a value the code owns must match the code (#134).

``docs/data_architecture.md`` said schema version 6 for a while after #114 made it 7.
"""
from __future__ import annotations

import re
from pathlib import Path

from RevenueChaser.core import config, rawstore

_REPO = Path(__file__).resolve().parents[2]


def test_data_architecture_names_the_current_schema_version():
    text = (_REPO / "docs" / "data_architecture.md").read_text(encoding="utf-8")
    m = re.search(r"Schema version `(\d+)`", text)
    assert m, "docs/data_architecture.md no longer states the schema version"
    assert m.group(1) == rawstore._SCHEMA_VERSION


# ``.env.example`` is the one place a new user learns which variables exist. It listed 3 of the 8
# provider variables (#169a); these tests compare it with the code that owns the names.
_ENV_EXTRAS = ("RC_SEC_USER_AGENT",
               "RAW_STORE_BACKUP_WARN_GB", "RAW_STORE_BACKUP_MIRROR_WARN_GB",   # #309 part 3
               "RC_ALLOWED_HOSTS", "RC_IBKR_LOGIN_URL",                         # #159 / the gateway shortcut
               "RC_IBKR_LOGIN_CHECK", "RC_IBKR_HOST", "RC_IBKR_PORT",           # #162
               "RC_SCHEDULER",                                                  # #158
               "RC_HTTP_CONTACT",                                               # #206, the builder's User-Agent
               "WRDS_USERNAME", "RC_UNIVERSE_SOURCE", "RC_WRDS_PYTHON")         # PR 5, the spine


def _env_example_lines() -> list[str]:
    return (_REPO / ".env.example").read_text(encoding="utf-8").splitlines()


def test_env_example_lists_every_provider_variable_and_the_known_extras():
    from RevenueChaser.core import credentials
    wanted = {p.env_var for p in credentials.PROVIDERS.values()} | set(_ENV_EXTRAS)
    listed = {m.group(1) for line in _env_example_lines() if (m := re.match(r"([A-Z][A-Z0-9_]*)=", line))}
    assert listed == wanted, (f"missing from .env.example: {sorted(wanted - listed)}; "
                              f"not a provider variable or known extra: {sorted(listed - wanted)}")


def test_env_example_has_names_only_and_a_one_line_comment_on_each():
    lines = _env_example_lines()
    for i, line in enumerate(lines):
        m = re.match(r"([A-Z][A-Z0-9_]*)=(.*)$", line)
        if not m:
            continue
        assert m.group(2).strip() == "", f"{m.group(1)} has a value in .env.example"
        assert i > 0 and lines[i - 1].startswith("# "), f"{m.group(1)} has no comment line above it"


def test_every_redirectable_base_url_in_the_code_is_in_the_data_retrieval_table():
    """A provider's ``RC_*_BASE`` seam that the doc does not list is a seam nobody finds (#169a P23)."""
    text = (_REPO / "docs" / "data_retrieval.md").read_text(encoding="utf-8")
    section = text.split("## Base URLs are redirectable", 1)[1].split("\n---", 1)[0].split("\n## ", 1)[0]
    documented = set(re.findall(r"RC_[A-Z0-9_]+?_BASE(?:_ROOT)?\b", section))
    skip = {"tests", "module_testing", ".git", ".hermes", "__pycache__", ".cache", ".store"}
    read_in_code: dict[str, str] = {}
    for path in _REPO.rglob("*.py"):
        rel = path.relative_to(_REPO)
        if skip & set(rel.parts) or rel.name == "conftest.py" or rel.name.startswith("test_"):
            continue
        if rel.parts[:2] == ("scripts", "research"):    # one-off research scripts, not app seams
            continue
        for name in re.findall(r"RC_[A-Z0-9_]+?_BASE(?:_ROOT)?\b", path.read_text(encoding="utf-8", errors="ignore")):
            read_in_code.setdefault(name, rel.as_posix())
    missing = {n: f for n, f in sorted(read_in_code.items()) if n not in documented}
    assert not missing, f"base-URL variables read in code but not in docs/data_retrieval.md: {missing}"


def test_every_manifest_table_has_a_row_in_the_per_table_column_table():
    """The per-table table in ``data_architecture.md`` lists every MANIFEST table (#164)."""
    text = (_REPO / "docs" / "data_architecture.md").read_text(encoding="utf-8")
    start = text.index("| Table | Typed (selectable) columns")
    section = text[start:].split("\n\n")[0]
    documented: set[str] = set()
    for line in section.splitlines()[2:]:
        documented |= set(re.findall(r"`([a-z_0-9]+)`", line.split("|")[1]))
    missing = sorted(set(rawstore.MANIFEST) - documented)
    assert not missing, f"tables without a row in the per-table column table: {missing}"


def test_the_result_runs_row_names_every_json_column():
    """A header JSON column added to the MANIFEST must be named in the ``result_runs`` row (PH-1, #23)."""
    text = (_REPO / "docs" / "data_architecture.md").read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| `result_runs` |"))
    json_columns = [c for c in rawstore._columns_of(rawstore.MANIFEST["result_runs"])
                    if rawstore._col_type("result_runs", c).split("(", 1)[0].rstrip("[]") == "JSON"]
    missing = [c for c in json_columns if f"`{c}`" not in row]
    assert not missing, f"result_runs JSON columns not named in docs/data_architecture.md: {missing}"


def test_the_stage_2_table_names_the_file_each_table_lives_in():
    """The DB column of the Stage 2 table is the file the MANIFEST routes each table to (#293: it said raw for
    ``index_levels_daily`` while #371 read it as the decision still open)."""
    text = (_REPO / "docs" / "data_architecture.md").read_text(encoding="utf-8")
    start = text.index("| Table | Content | DB | TTL |")
    section = text[start:].split("\n\n")[0]
    wrong = {}
    for line in section.splitlines()[2:]:
        cells = [c.strip() for c in line.split("|")[1:-1]]
        for name in re.findall(r"`([a-z_0-9]+)`", cells[0]):
            if name in rawstore.MANIFEST and rawstore._db_of(name) != cells[-2]:
                wrong[name] = (cells[-2], rawstore._db_of(name))
    assert not wrong, f"Stage 2 rows whose DB column is not the MANIFEST file (doc, code): {wrong}"


def test_data_architecture_names_the_backup_folders_and_switches_the_code_owns():
    """Backups section: the folder and setting names ``backup_store`` / ``config`` own are written in the doc (#275-#277)."""
    from RevenueChaser.scripts import backup_store as bs
    text = (_REPO / "docs" / "data_architecture.md").read_text(encoding="utf-8")
    section = text.split("## Backups, restore and compaction", 1)[1]
    assert hasattr(config, "RAW_STORE_BACKUP_WEEKLY") and hasattr(config, "RAW_STORE_BACKUP_MIRROR")
    for name in (bs.SPX_MIRROR_DIRNAME, bs.SPX_VERSIONS_DIRNAME, bs.WEEKLY_DIRNAME,
                 "RAW_STORE_BACKUP_DIR", "RAW_STORE_BACKUP_WEEKLY", "--no-weekly", "--keep-weekly",
                 "RAW_STORE_BACKUP_MIRROR", "--keep-mirror", bs.OFFMACHINE_SUBDIR,
                 "--keep-mirror-big", "--mirror-big-every-days", "--mirror-big-now",
                 bs.MANIFEST_NAME, bs.OFFMACHINE_MANIFEST_NAME, bs.SCAN_MARKER_NAME, "--scan-every-days",
                 "--scan-now", "RAW_STORE_BACKUP_WARN_GB", "RAW_STORE_BACKUP_MIRROR_WARN_GB"):
        assert name in section, f"the Backups section of docs/data_architecture.md does not name {name}"


def test_data_architecture_states_the_scan_interval_and_the_size_thresholds_the_code_owns():
    """#309: the integrity scan's default interval and the two size warnings' defaults, as the code has them."""
    from RevenueChaser.scripts import backup_store as bs
    text = (_REPO / "docs" / "data_architecture.md").read_text(encoding="utf-8")
    section = text.split("## Backups, restore and compaction", 1)[1]
    for name, value in (("--scan-every-days", bs.SCAN_EVERY_DAYS),
                        ("RAW_STORE_BACKUP_WARN_GB", f"{config.BACKUP_WARN_GB_DEFAULT:g}"),
                        ("RAW_STORE_BACKUP_MIRROR_WARN_GB", f"{config.BACKUP_MIRROR_WARN_GB_DEFAULT:g}")):
        assert re.search(rf"`{re.escape(name)}`\s+\(default {value}\b", section), f"the doc does not state {name}'s default"


def test_data_architecture_states_the_off_machine_defaults_the_code_owns():
    """#349: the Backups section says how often the big kinds go to the second folder and how many copies stay."""
    from RevenueChaser.scripts import backup_store as bs
    text = (_REPO / "docs" / "data_architecture.md").read_text(encoding="utf-8")
    section = text.split("## Backups, restore and compaction", 1)[1]
    assert f"every {bs.MIRROR_BIG_EVERY_DAYS} days" in section
    assert f"--keep-mirror-big` (default {bs.KEEP_MIRROR_BIG})" in section
    assert f"--keep-mirror` (default {bs.KEEP_MIRROR})" in section


def test_the_spx_tree_row_no_longer_calls_it_external():
    """B3: the storage table said "external ... outside the repo"; the tree is ``module_spx_gamma/data`` in the repo (#276)."""
    text = (_REPO / "docs" / "data_architecture.md").read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if "SPX_GAMMA_DATA_ROOT" in line and line.startswith("|"))
    assert "module_spx_gamma/data" in row and "outside the repo" not in row and "external" not in row


def test_data_architecture_names_no_scheduled_job_or_history_the_code_does_not_have():
    """#288 (review B14): three sentences described a forward-test collector, two 22:30 tasks and calibration history
    that no code installs or keeps. ``register_tasks.ps1`` installs only the backup; the registry has five tasks."""
    from RevenueChaser.core import automations
    text = (_REPO / "docs" / "data_architecture.md").read_text(encoding="utf-8")
    for phrase in ("forward-test collector", "forward-test history", "calibration stats", "22:30", "Outcomes + Rotation"):
        assert phrase not in text, f"docs/data_architecture.md still says {phrase!r}"
    # An hourly row (RC-Build, PR 2) is stated by its minute: "hourly at :07".
    times = {e.name: f"hourly at :{e.time[-2:]}" if e.hourly else e.time for e in automations.REGISTRY}
    assert times == {"RC-Store-Backup": "04:00", "RC-Relatedness-OffPeak": "12:30",
                     "RC-ValueChain-Sync": "13:00", "RC-Daily-Bars": "22:35",
                     "RC-Screeners-Daily": "23:00", "RC-Forecast-Outcomes": "23:45",
                     "RC-Forecast-Baselines": "23:50",
                     "RC-Index-Lists": "10:00", "RC-Earnings-Inputs": "01:30",
                     "RC-Open-Issues-Check": "09:30", "RC-IBKR-Login-Check": "09:15",
                     "RC-Build": "hourly at :07", "RC-Build-Backup": "04:30"}
    for stamp in times.values():
        assert stamp in text, f"the registered time {stamp} is not in docs/data_architecture.md"
    ps1 = (_REPO / "register_tasks.ps1").read_text(encoding="utf-8")
    assert "RC-Store-Backup" in ps1 and "RC-Relatedness-OffPeak" not in ps1 and "RC-ValueChain-Sync" not in ps1


def test_data_health_drill_line_names_the_500_store_read_failed_case():
    """#358: the Settings ticker drill answers 500 "store read failed" (DF98), not only 404."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "docs" / "data_health.md").read_text(encoding="utf-8")
    line = next(l for l in text.splitlines() if "Per-ticker drill `GET /api/settings/ticker/<t>`" in l)
    start = text.index(line)
    end = text.find("\n- ", start + 1)
    block = text[start:end]
    assert "500" in block and "store read failed" in block


# Every Markdown file a test reads by path. tests.yml drops ``**.md``, so each one has to be added back to the
# path filter of both triggers, or a docs-only PR that edits it skips CI and cannot go red against its test.
# Keep this list in step with the tests (grep them for ``.md``); the test below also fails on a ``.md`` fixture
# that lives under a ``tests`` directory and is missing here.
_MARKDOWN_READ_BY_TESTS = (
    "docs/data_architecture.md",        # test_doc_constants.py, test_store_schema_refuse.py:36, test_rawstore_history.py:595
    "docs/data_health.md",             # test_doc_constants.py:179, test_automation_chain.py:125
    "docs/data_retrieval.md",          # test_doc_constants.py:56
    "docs/rules/00_overview.md",       # module_earnings_radar/tests/test_confidence_wording_df06.py:62
    "docs/rules/redesign.md",          # module_earnings_radar/tests/test_cleanup_199.py:13-15
    "module_spx_gamma/tests/golden/position_map.base.md",   # module_spx_gamma/tests/test_autoescape_175.py:106
)


def _trigger_path_lists() -> dict[str, list[str]]:
    wf = (_REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    lists: dict[str, list[str]] = {}
    for trigger in ("pull_request", "push"):
        block = re.search(rf"^  {trigger}:\n(?:    .*\n)+", wf, re.M).group(0)
        lists[trigger] = re.findall(r'^      - "([^"]+)"$', block, re.M)
    return lists


def test_ci_paths_list_every_markdown_file_a_unit_test_reads():
    """#358 B1: tests.yml drops **.md, so a doc a test reads must be added back to both path lists, after the drop."""
    for trigger, paths in _trigger_path_lists().items():
        assert "!**.md" in paths, trigger
        after_drop = paths[paths.index("!**.md") + 1:]
        missing = [d for d in _MARKDOWN_READ_BY_TESTS if d not in after_drop]
        assert not missing, f"tests.yml {trigger}.paths does not add back (after !**.md): {missing}"


def test_every_markdown_the_tests_read_exists_and_every_test_fixture_markdown_is_listed():
    for doc in _MARKDOWN_READ_BY_TESTS:
        assert (_REPO / doc).is_file(), f"{doc} is listed as read by a test but is not in the tree"
    fixtures = {p.relative_to(_REPO).as_posix() for p in _REPO.rglob("*.md")
                if "tests" in p.relative_to(_REPO).parts[:-1] and not {".git", ".cache", ".store"} & set(p.parts)}
    assert fixtures <= set(_MARKDOWN_READ_BY_TESTS), sorted(fixtures - set(_MARKDOWN_READ_BY_TESTS))


def _market_store_section() -> str:
    text = (_REPO / "docs" / "data_architecture.md").read_text(encoding="utf-8")
    return text.split("## The market store (`market/`)", 1)[1].split("\n## ", 1)[0]


def test_the_market_store_section_states_the_versions_and_kinds_the_code_owns():
    """#206: the ops schema version, each serving kind and its serving schema version, as rcdata has them."""
    from RevenueChaser.rcdata import ops, serving
    section = _market_store_section()
    assert f"ops schema version `{ops.OPS_SCHEMA_VERSION}`" in section
    for kind, version in serving.SERVING_SCHEMA_VERSION.items():
        assert f"`{kind}` (serving schema version `{version}`)" in section, kind
    kinds = re.search(r"Serving kinds: (.+)\.", section)
    assert kinds and set(re.findall(r"`([a-z_]+)`", kinds.group(1))) == set(serving.KINDS)


def test_the_market_store_section_names_every_ops_table_root_and_cap():
    from RevenueChaser.rcdata import backup, caps, ops
    section = _market_store_section()
    for table in ops.TABLES:
        assert f"`{table}`" in section, table
    for name in ("RC_MARKET_DIR", "RC_MARKET_RAW_DIR", "RC_MARKET_RAW_COPY_DIR", "RAW_STORE_BACKUP_DIR",
                 backup.MANIFEST_NAME):
        assert name in section, name
    for provider, cap in caps.DAY_CAPS.items():
        assert f"{provider} {cap:,}" in section, provider
    assert f"newest {backup.KEEP}" in section and f"{backup.RAW_COPY_DAYS} days" in section


def test_data_architecture_names_the_builder_units_and_their_times():
    """PR 2: the builder service's unit names, schedule and limits are the registry's (core/automations.py)."""
    from RevenueChaser.core import automations
    text = (_REPO / "docs" / "data_architecture.md").read_text(encoding="utf-8")
    for name in ("RC-Build", "RC-Build-Backup"):
        e = automations.entry(name)
        assert f"unit `{e.unit}`" in text, f"docs/data_architecture.md does not name the unit {e.unit}"
        assert f"{e.settings.limit_minutes} min limit" in text
    assert automations.entry("RC-Build").unit == automations.BUILDER_UNIT == "life-rc-builder"
    assert automations.entry("RC-Build-Backup").unit == automations.BUILDER_BACKUP_UNIT == "life-rc-builder-backup"


def test_data_health_states_the_new_section_limits_the_code_owns():
    """PR 2: the cycle lengths, the backup warning, the backfill confirm threshold and the handle rule."""
    from RevenueChaser.module_settings import newdata, report
    text = (_REPO / "docs" / "data_health.md").read_text(encoding="utf-8")
    section = text[text.index('### 2.0 The "New data system" section'):text.index("### 2.1 The summary tiles")]
    assert f"for {newdata.CYCLE_DAYS['weekly']} days" in section
    assert f"monthly one for {newdata.CYCLE_DAYS['monthly']}" in section
    assert f"past {newdata.BACKUP_WARN_DAYS} days" in section
    assert f"over {report.ND_BACKFILL_CONFIRM_OVER:,}" in section
    assert f"over {newdata.RETIREMENT_DAYS} days" in section
    assert f"SPX expiry-strike {newdata.COVERAGE_TARGET['uw/spot_expiry_strike']:.0%}" in section
    assert "`rcdata.serving.open_read(kind)`" in section and "No connection is cached" in section
    assert f"`{newdata.RETENTION_ENV}`" in section and f"`{newdata.RETENTION_ROW}`" in section


def test_data_architecture_states_the_spx_gamma_store_reads_the_code_owns():
    """PR 3: the market-store section says how SPX Gamma reads it; the lags and the requester are the code's."""
    from RevenueChaser.app import runners
    from RevenueChaser.module_spx_gamma import store_reads
    text = (_REPO / "docs" / "data_architecture.md").read_text(encoding="utf-8")
    section = text[text.index("**The first board on it: SPX Gamma"):].split("\n\n")[0].replace("\n", " ")
    for kind in ("vix", "cot"):
        assert f"inside {store_reads.MAX_LAG[kind]} sessions" in section, kind
    assert f"Tuesday + {store_reads.RELEASE_DAYS} days" in section
    assert f"`requested_by` `{runners.SPX_REQUESTED_BY}`" in section
    for name in runners.SPX_BUILDER_DATASETS:
        assert f"`{name}`" in section


def test_the_spine_section_states_what_the_code_owns():
    """PR 5: the spine's datasets, tables, views, tier rule version, issue kinds and the flag, as the code has them."""
    from RevenueChaser.core import universe
    from RevenueChaser.rcdata.build import DATASETS
    from RevenueChaser.rcdata.build.spine import KIND, security, tier, views
    text = (_REPO / "docs" / "data_architecture.md").read_text(encoding="utf-8")
    section = text[text.index("### The identity spine (`spine.duckdb`, PR 5)"):text.index("## Migration")]
    for name, ds in DATASETS.items():
        if ds.kind == KIND:
            assert f"| `{name}` |" in section, name
    for table in security.TABLES:
        assert f"| `{table}` |" in section, table
    for view in views.VIEWS:
        assert f"`{view}`" in section, view
    for kind in security.ISSUE_KINDS:
        assert f"`{kind}`" in section, kind
    assert f"**Tier rule v{tier.VERSION}**" in section
    assert f"`{universe.SOURCE_ENV}={universe.SPINE}`" in section
    assert f"`{universe.SOURCE_ENV}={universe.STORE}`" in section            # PR 6: the old store, the rollback


def test_the_ownership_section_states_what_the_code_owns():
    """PR 16: the ownership store's datasets, served tables, views and build issue kinds, as the code has them."""
    from RevenueChaser.rcdata.build import DATASETS
    from RevenueChaser.rcdata.build.ownership import KIND, serve
    text = (_REPO / "docs" / "data_architecture.md").read_text(encoding="utf-8")
    section = text[text.index("### The ownership store (`ownership.duckdb`, PR 16)"):text.index("## Migration")]
    for name, ds in DATASETS.items():
        if ds.kind == KIND:
            assert f"| `{name}` |" in section, name
            for table in ds.tables:
                assert f"| `{table}` |" in section, table
    for view in serve.VIEWS:
        assert f"`{view}`" in section, view
    for kind in serve.ISSUE_KINDS:
        assert f"`{kind}`" in section, kind


def test_the_options_section_states_what_the_code_owns():
    """G8: the options data's datasets, tables, window, page sizes, pacing, call arithmetic and retention rule."""
    from RevenueChaser.rcdata import caps, retention
    from RevenueChaser.rcdata.build import DATASETS, options
    from RevenueChaser.rcdata.build.options import ibkr_iv, tables, uw, uw_screener
    text = (_REPO / "docs" / "data_architecture.md").read_text(encoding="utf-8")
    section = text[text.index("### The options data (`options.duckdb`, G8)"):text.index("## Migration")]
    flat = " ".join(section.split())
    for name, ds in DATASETS.items():
        if ds.kind == options.KIND:
            assert f"`{name}`" in section, name
    for table in tables.TABLES:
        assert f"| `{table}` |" in section, table
    assert f"the last {tables.EOD_SESSIONS} sessions" in flat
    assert f"after {uw.RUN_BUDGET_S // 60} minutes" in flat and f"next {uw_screener.DAYS_AHEAD} days" in flat
    assert f"`option-contracts` ({uw.PAGE_LIMIT} a page)" in flat
    assert f"{uw_screener.SCREENER_BATCH} names a call" in flat and f"{uw_screener.CALENDAR_LIMIT} a page" in flat
    assert f"{ibkr_iv.PER_MINUTE} requests a minute" in flat
    assert (f"A list name is {options.LIST_CALLS} calls plus {options.EXPIRY_CALLS} per expiry" in flat
            and f"an earnings-set name {options.EARNINGS_CALLS}" in flat)
    for n, words in ((0, "with SPX alone"), (100, "with 100 list names"), (500, "with 500")):
        assert f"{options.uw_calls_a_session(n):,} calls {words}" in flat or \
            f"{options.uw_calls_a_session(n):,} {words}" in flat, n
    assert f"UW's day cap of {caps.DAY_CAPS['uw']:,}" in flat
    assert f"`{retention.ENV}`" in section and f"`{retention.ROW}`" in section
