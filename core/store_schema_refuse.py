"""PL7-b (#26): refuse data writes when a store file's schema stamp is ahead of this code (newer, or unreadable).

Off by default, so a mismatch cannot stop the dashboard from starting. Turn it on with
``RC_STORE_SCHEMA_REFUSE=1``. ``SCHEMA_REFUSE_WRITES`` is the default when that variable is unset.
Escape: ``RC_STORE_SCHEMA_OK=1`` (a deliberate migration). Readers warn; they do not raise.
An older stamp is not a refusal — the ensure path upgrades it (PL7-a).

The same check runs before the ensure (``rawstore._refuse_ensure_if_ahead``), so an old checkout never
renames, merges or ALTERs a newer file first. A caller's own ``connect()`` handle is outside this guard, same as the history-SQL guard.
"""
from __future__ import annotations

import os

# Default off. The env var is the switch an operator sets; this constant is what the code names.
SCHEMA_REFUSE_WRITES = False
REFUSE_ENV = "RC_STORE_SCHEMA_REFUSE"
ESCAPE_ENV = "RC_STORE_SCHEMA_OK"


def schema_refuse_writes() -> bool:
    """True only when the switch is on and the escape is not set."""
    if os.getenv(ESCAPE_ENV, "0") == "1":
        return False
    raw = os.getenv(REFUSE_ENV)
    if raw is None:
        return SCHEMA_REFUSE_WRITES
    return raw == "1"


def message(db: str, stored: str, code: str) -> str:
    """The refusal text for a file whose stamp is ahead of this code: file name, both versions, the restore."""
    from RevenueChaser.core import rawstore
    file = rawstore._db_path(db).name
    what = (f"schema stamp {stored!r} is not a version number" if rawstore._stamp_unreadable(stored)
            else f"schema stamp is {stored}")
    return (f"{file} {what} but this code is {code}; refusing writes so this checkout cannot change a "
            f"newer store. Restore a matching snapshot: "
            f"python -m RevenueChaser.scripts.restore_store --latest {db}. "
            f"Set {ESCAPE_ENV}=1 only for a deliberate migration.")


def refuse_schema_write(db: str) -> None:
    """Raise ``StoreSchemaMismatch`` when this file's stamp is ahead of this code. No-op when the switch is off.

    Ahead = newer than ``_SCHEMA_VERSION``, or not a version number (it cannot be compared, so it is not
    trusted). A file with no stamp is NOT refused: the ensure stamps it current, and a refusal there would
    only fire when the ensure failed for another reason (a lock), with a misleading message.
    """
    if not schema_refuse_writes():
        return
    from RevenueChaser.core import rawstore
    stored = rawstore._cached_stamp(db)
    code = rawstore.schema_version()
    if stored is None or rawstore._stamp_rank(stored) <= rawstore._stamp_rank(code):
        return
    raise rawstore.StoreSchemaMismatch(message(db, stored, code))
