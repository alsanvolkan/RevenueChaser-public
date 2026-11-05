"""S4 (#38): the SPX Gamma tree moves out of the checkout by a verified copy, never a delete."""
from __future__ import annotations

from RevenueChaser.module_spx_gamma.config import resolve_data_root
from RevenueChaser.module_spx_gamma.data_root_move import migrate


def _tree(root):
    (root / "raw" / "2026-11-04").mkdir(parents=True)
    (root / "raw" / "2026-11-04" / "gex_levels.json").write_text('{"a": 1}')
    (root / "brier.json").write_text("{}")


def test_override_wins(tmp_path):
    assert resolve_data_root(str(tmp_path / "x"), tmp_path / "store", tmp_path / "legacy") == tmp_path / "x"


def test_legacy_tree_is_read_until_the_store_root_exists(tmp_path):
    legacy, store = tmp_path / "legacy", tmp_path / "store" / "spx_gamma"
    _tree(legacy)
    assert resolve_data_root(None, store, legacy) == legacy
    _tree(store)                                     # the migrated tree, renamed into place with its files
    assert resolve_data_root(None, store, legacy) == store


def test_a_fresh_install_uses_the_store_root(tmp_path):
    store = tmp_path / "store" / "spx_gamma"
    assert resolve_data_root(None, store, tmp_path / "legacy") == store


def test_migrate_dry_run_then_verified_copy_keeps_the_legacy_tree(tmp_path):
    legacy, target = tmp_path / "legacy", tmp_path / "store" / "spx_gamma"
    _tree(legacy)
    dry = migrate(legacy, target)
    assert dry["status"] == "dry_run" and dry["files"] == 2 and not target.exists()
    done = migrate(legacy, target, apply=True)
    assert done["status"] == "copied" and done["verified"] == 2
    assert (target / "raw" / "2026-11-04" / "gex_levels.json").read_text() == '{"a": 1}'
    assert (legacy / "brier.json").exists()          # never deleted
    assert not (tmp_path / "store" / "spx_gamma.partial").exists()
    assert migrate(legacy, target, apply=True)["status"] == "target_exists"


def test_nothing_to_copy(tmp_path):
    assert migrate(tmp_path / "none", tmp_path / "t", apply=True)["status"] == "nothing_to_copy"


def test_an_empty_store_root_does_not_hide_the_legacy_tree(tmp_path):
    """Fable #5: a bare mkdir of the new root before --migrate-data-root must not switch the board to an empty tree."""
    legacy, store = tmp_path / "legacy", tmp_path / "store" / "spx_gamma"
    _tree(legacy)
    store.mkdir(parents=True)
    assert resolve_data_root(None, store, legacy) == legacy


def test_migrate_copies_into_an_empty_store_root(tmp_path):
    """Fable delta #2: an empty <store>/spx_gamma (no files) is no target: resolve reads legacy, so migrate copies."""
    legacy, target = tmp_path / "legacy", tmp_path / "store" / "spx_gamma"
    _tree(legacy)
    (target / "raw").mkdir(parents=True)
    assert migrate(legacy, target)["status"] == "dry_run"
    assert migrate(legacy, target, apply=True)["status"] == "copied"
    assert (target / "brier.json").exists() and resolve_data_root(None, target, legacy) == target


def test_a_target_holding_a_file_is_still_refused(tmp_path):
    legacy, target = tmp_path / "legacy", tmp_path / "store" / "spx_gamma"
    _tree(legacy)
    target.mkdir(parents=True)
    (target / "stray.json").write_text("{}")
    assert migrate(legacy, target, apply=True)["status"] == "target_exists"
    assert (target / "stray.json").read_text() == "{}"
