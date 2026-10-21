"""One rule result with three values: True (passed), False (failed), None (unknown), shared by the AG, Swing and
Fundamentals screeners (#151, decision 8-A).

A rule is unknown when an input it needs is missing. An unknown rule is neither a pass nor a fail: it is left out
of the "k passed" count, the board says how many rules were known ("k/X of n known"), and a gate that needs every
rule (Quality 3/3, Minervini 8/8, Weinstein 5/5, AG's three signals) passes only when every rule is known and
passed. Pure module: no store, no network.
"""
from __future__ import annotations

from typing import Callable


def known(*values) -> bool:
    return all(v is not None for v in values)


def rule(inputs: tuple, test: Callable[..., object]) -> bool | None:
    """``test(*inputs)`` as a bool, or None when any input is None (the test is not called)."""
    return bool(test(*inputs)) if known(*inputs) else None


def either(*results: bool | None) -> bool | None:
    """True when any result is True; False when all are known and False; else None (it could still pass)."""
    if any(r is True for r in results):
        return True
    return False if all(r is False for r in results) else None


def tally(rules: list[dict]) -> tuple[int, int, int]:
    """``(passed, known, total)`` of rule dicts that carry ``passed``."""
    passed = sum(1 for r in rules if r["passed"] is True)
    return passed, sum(1 for r in rules if r["passed"] is not None), len(rules)


def score_text(passed: int, n_known: int, total: int) -> str:
    """``"2/3"`` when every rule is known, else ``"2/3 of 2 known"``."""
    base = f"{passed}/{total}"
    return base if n_known == total else f"{base} of {n_known} known"
