"""Public #17 (C2) — the rotation record lives in `module_sector_rotation/models.py`.

Two invariants:
  1. the old home, `RevenueChaser.core.models.rotation`, is gone (no re-export shim:
     a shim keeps the board -> core edge the move exists to remove);
  2. the move was pure — `ETFRotationSnapshot` keeps its field names, order and
     defaults, pinned below from the file as it stood on 3670e76.

Defaults are pinned by marker: `REQUIRED` (no default), `FACTORY:<name>`
(a `field(default_factory=...)` reference), or the literal default value.
"""
import dataclasses
import importlib

import pytest


def test_old_home_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("RevenueChaser.core.models.rotation")


def test_new_home_imports_the_record():
    from RevenueChaser.module_sector_rotation.models import ETFRotationSnapshot

    assert dataclasses.is_dataclass(ETFRotationSnapshot)


def _spec(cls):
    """[(name, marker)] — REQUIRED / FACTORY:<name> / literal default."""
    out = []
    for f in dataclasses.fields(cls):
        if f.default is not dataclasses.MISSING:
            out.append((f.name, f.default))
        elif f.default_factory is not dataclasses.MISSING:
            out.append((f.name, "FACTORY:" + f.default_factory.__name__))
        else:
            out.append((f.name, "REQUIRED"))
    return out


# ---------------------------------------------------------------- expected ---

ETF_ROTATION_SNAPSHOT = [
    ("as_of_date", "REQUIRED"),
    ("ticker", "REQUIRED"),
    ("group_name", "REQUIRED"),
    ("close_price", 0.0),
    ("rs_1w", None),
    ("rs_1m", None),
    ("rs_3m", None),
    ("rs_ratio", None),
    ("rs_momentum", None),
    ("rs_ratio_z", None),
    ("rs_momentum_z", None),
    ("rrg_quadrant", ""),
    ("flow_score", None),
    ("rotation_score", None),
    ("source", "UW"),
    ("retrieved_at", "FACTORY:now"),
]

EXPECTED = {
    "ETFRotationSnapshot": ETF_ROTATION_SNAPSHOT,
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_field_parity(name):
    import RevenueChaser.module_sector_rotation.models as models

    assert _spec(getattr(models, name)) == EXPECTED[name]
