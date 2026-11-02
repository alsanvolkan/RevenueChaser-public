"""Text settings in ``board_settings.value_text`` (#360, avo 2 Nov 2026: "evet ekle")."""
from __future__ import annotations

import pytest

from RevenueChaser.core import board_settings, rawstore
from RevenueChaser.core.board_settings import Setting

BOARD = "test_text_board"


@pytest.fixture
def text_board(rawstore_tmp_db, monkeypatch):
    specs = {"PICK": Setting("PICK", "text", lambda: "alpha", choices=lambda: ("alpha", "beta", "gamma")),
             "N": Setting("N", "int", lambda: 5, lo=1, hi=9)}
    monkeypatch.setitem(board_settings.SPEC, BOARD, (lambda: specs, lambda v: []))
    return specs


def test_a_text_value_lands_in_value_text(text_board):
    out = board_settings.save(BOARD, {"PICK": "beta"})
    assert out["values"]["PICK"] == "beta" and out["overridden"] == ["PICK"]
    row = rawstore.query("SELECT value, value_text FROM board_settings WHERE board = ? AND key = 'PICK'", [BOARD])[0]
    assert row["value_text"] == "beta" and row["value"] is None
    assert board_settings.get(BOARD, "PICK") == "beta"


def test_a_number_still_lands_in_value(text_board):
    board_settings.save(BOARD, {"N": 7})
    row = rawstore.query("SELECT value, value_text FROM board_settings WHERE board = ? AND key = 'N'", [BOARD])[0]
    assert row["value"] == 7.0 and row["value_text"] is None
    assert board_settings.get(BOARD, "N") == 7


def test_a_text_value_outside_its_choices_is_refused(text_board):
    with pytest.raises(board_settings.SettingsError) as exc:
        board_settings.save(BOARD, {"PICK": "delta"})
    assert "PICK must be one of alpha, beta, gamma" in exc.value.messages[0]
    with pytest.raises(board_settings.SettingsError):
        board_settings.save(BOARD, {"PICK": 3})               # a number is not a text choice


def test_saving_the_default_text_drops_the_override(text_board):
    board_settings.save(BOARD, {"PICK": "gamma"})
    out = board_settings.save(BOARD, {"PICK": "alpha"})
    assert out["overridden"] == [] and out["values"]["PICK"] == "alpha"


def test_reset_brings_the_text_default_back(text_board):
    board_settings.save(BOARD, {"PICK": "gamma"})
    assert board_settings.reset(BOARD)["values"]["PICK"] == "alpha"


def test_describe_names_the_text_kind(text_board):
    row = {r["key"]: r for r in board_settings.describe(BOARD)}["PICK"]
    assert row["kind"] == "text" and row["choices"] == ["alpha", "beta", "gamma"] and row["value"] == "alpha"
