"""Relatedness board settings (#360): the profile writer, ``WRITER`` (provider) and ``WRITER_MODEL``.

Both are text settings (``board_settings.value_text``). The model must be one of the chosen provider's
(``writers.WRITERS``), and a provider whose credential is absent cannot be saved; Reset brings EVREN back.
"""
from __future__ import annotations

from RevenueChaser.core import board_settings

from . import writers

BOARD = "module_relatedness"


def specs() -> dict[str, board_settings.Setting]:
    return {
        "WRITER": board_settings.Setting(
            "WRITER", "text", lambda: writers.DEFAULT, choices=lambda: tuple(writers.WRITERS),
            label="Profile writer"),
        # A fixed default (review nit4): a save's drop-on-default never depends on the provider stored before it.
        # ``writer()`` reads a provider's own default model when the stored pair does not fit (review S2).
        "WRITER_MODEL": board_settings.Setting(
            "WRITER_MODEL", "text", lambda: writers.get(writers.DEFAULT).models[0],
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


def _pair() -> tuple[str, str, bool]:
    """``(provider, model, model_chosen)``. A stored pair that does not fit (a one-key reset, a model saved for
    another provider; PR 362 review S2) reads the provider's own default model, never an invalid pair."""
    vals = board_settings.values(BOARD)
    slug = vals["values"]["WRITER"]
    if slug not in writers.WRITERS:
        slug = writers.DEFAULT
    model = vals["values"]["WRITER_MODEL"]
    models = writers.get(slug).models
    if model in models:
        return slug, model, "WRITER_MODEL" in vals["overridden"] or model != models[0]
    return slug, models[0], False


def writer() -> tuple[str, str]:
    """``(provider, model)`` the board runs with (the saved pair, else EVREN and its default model)."""
    slug, model, _ = _pair()
    return slug, model


def chosen_model() -> str | None:
    """The chosen model, or ``None`` when the provider's default applies (the chat module's own ``MODEL`` then
    stays, which honours ``RC_<PROVIDER>_MODEL``)."""
    _, model, chosen = _pair()
    return model if chosen else None
