"""PL7-b (#26): refuse data writes when a store file's schema stamp is newer than this code, or missing.

Off by default, so a mismatch cannot stop the dashboard from starting. Turn it on with
``RC_STORE_SCHEMA_REFUSE=1``. ``SCHEMA_REFUSE_WRITES`` is the default when that variable is unset.
Escape: ``RC_STORE_SCHEMA_OK=1`` (a deliberate migration). Readers warn; they do not raise.
An older stamp is not a refusal — the ensure path upgrades it (PL7-a).

A caller's own ``connect()`` handle is outside this guard, same as the history-SQL guard.
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


def refuse_schema_write(db: str) -> None:
    """Raise ``StoreSchemaMismatch`` when this file must not be written. No-op when the switch is off.

    A file that does not exist yet is not a refusal: creating it is how a fresh store starts, and
    the ensure path stamps it. A file that exists with no stamp, or with a stamp newer than this
    code, is refused — the message names the file, both versions and the restore command.
    """
    if not schema_refuse_writes():
        return
    from RevenueChaser.core import rawstore
    path = rawstore._db_path(db)
    if not path.exists():
        return
    stored = rawstore.schema_stamp(db)
    code = rawstore.schema_version()
    if stored is not None and rawstore._stamp_rank(stored) <= rawstore._stamp_rank(code):
        return
    restore = f"python -m RevenueChaser.scripts.restore_store --latest {db}"
    file = path.name
    if stored is None:
        raise rawstore.StoreSchemaMismatch(
            f"{file} has no schema stamp but this code is {code}; refusing writes. "
            f"Restore a matching snapshot: {restore}. "
            f"Set {ESCAPE_ENV}=1 only for a deliberate migration.")
    raise rawstore.StoreSchemaMismatch(
        f"{file} schema stamp is {stored} but this code is {code}; refusing writes "
        f"so this checkout cannot change a newer store. "
        f"Restore a matching snapshot: {restore}. "
        f"Set {ESCAPE_ENV}=1 only for a deliberate migration.")
