"""Relatedness board settings (#360): the profile writer, ``WRITER`` (provider) and ``WRITER_MODEL``.

Both are text settings (``board_settings.value_text``). The model must be one of the chosen provider's
(``writers.WRITERS``), and a provider whose credential is absent cannot be saved; Reset brings EVREN back.
"""
from __future__ import annotations

from RevenueChaser.core import board_settings

from . import writers

BOARD = "module_relatedness"


def _stored_writer() -> str:
    try:
        slug = board_settings._stored(BOARD).get("WRITER")
    except Exception:  # noqa: BLE001 - no store yet: the default
        slug = None
    return slug if slug in writers.WRITERS else writers.DEFAULT


def specs() -> dict[str, board_settings.Setting]:
    return {
        "WRITER": board_settings.Setting(
            "WRITER", "text", lambda: writers.DEFAULT, choices=lambda: tuple(writers.WRITERS),
            label="Profile writer"),
        # The default follows the saved provider, so a saved Grok without a model reads Grok's model.
        "WRITER_MODEL": board_settings.Setting(
            "WRITER_MODEL", "text", lambda: writers.get(_stored_writer()).models[0],
            choices=writers.all_models, label="Writer model"),
    }


def rules(v: dict) -> list[str]:
    out = []
    slug, model = v.get("WRITER"), v.get("WRITER_MODEL")
    w = writers.WRITERS.get(slug)
    if w is None:
        return out
    if model not in w.models:
        out.append(f"WRITER_MODEL must be one of {slug}'s models: {', '.join(w.models)}; got {model}")
    absent = writers.missing_text(slug)
    if absent:
        out.append(f"WRITER {slug} cannot be chosen: {absent}")
    return out


def writer() -> tuple[str, str]:
    """``(provider, model)`` the board runs with (the saved pair, else EVREN and its default model)."""
    vals = board_settings.values(BOARD)["values"]
    return vals["WRITER"], vals["WRITER_MODEL"]


def chosen_model() -> str | None:
    """The saved model, or ``None`` when none is saved (the chat module's own ``MODEL`` then stays, which honours
    ``RC_<PROVIDER>_MODEL``)."""
    return board_settings.values(BOARD)["values"]["WRITER_MODEL"] if (
        "WRITER_MODEL" in board_settings.values(BOARD)["overridden"]) else None
