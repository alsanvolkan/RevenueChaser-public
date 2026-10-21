"""UW Interaction engine — run one catalog tool and normalize its rows.

Two responsibilities, kept pure so unit tests never touch the network:

1. **Validate + coerce** the user's form values against the tool's param
   schema (kind, required, ``in_path``) BEFORE any API call. A malformed
   request is rejected here, never half-sent.
2. **Call** the endpoint via ``UWClient.raw_endpoint`` and **normalize** the
   body into ``rows`` (list of dicts) + ``columns`` for the generic table.

``rows`` semantics per catalog entry:
- ``data``   — body is ``{"data": [...]}`` (the common UW envelope).
- ``list``   — body is a bare JSON array.
- ``object`` — body is a single object (e.g. GEX levels); wrapped in one row.

A call that returns nothing is an *explicit empty state* with a reason, never a
silent ``[]`` — callers can say so on the board.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

from RevenueChaser.core.providers.unusual_whales import UWClient

from .catalog import get_tool

logger = logging.getLogger(__name__)


class ToolError(ValueError):
    """A catalog tool could not be run: unknown key, bad param, or bad body shape."""


@dataclass
class ToolResult:
    """Normalized outcome of one tool run."""

    tool_key: str
    label: str
    family: str
    path: str
    help: str
    params_used: dict
    rows: list = field(default_factory=list)
    columns: list = field(default_factory=list)
    row_count: int = 0
    error: Optional[str] = None        # user-facing problem (empty state)
    http_status: Optional[int] = None  # 401/403 credential rejection, when seen


def _coerce(value, kind: str) -> object:
    """Coerce a raw form string to the param's declared kind (or None when blank)."""
    if value is None:
        return None
    s = str(value).strip()
    if s == "":
        return None
    if kind == "int":
        try:
            return int(float(s))
        except ValueError:
            raise ToolError(f"'{s}' is not a whole number")
    if kind == "float":
        try:
            return float(s)
        except ValueError:
            raise ToolError(f"'{s}' is not a number")
    if kind == "bool":
        low = s.lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        raise ToolError(f"'{s}' is not a yes/no value")
    return s  # text / select


def _columns_from(rows: list[dict]) -> list[str]:
    """Deterministic column order: union of keys, first-seen order wins."""
    cols: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for k in row.keys():
            if k not in seen:
                seen.add(k)
                cols.append(k)
    return cols


def _extract(body, spec: str) -> tuple[list[dict], Optional[str]]:
    """Pull rows out of the raw body per the catalog ``rows`` semantics.

    Returns ``(rows, error)`` — error explains an unusable/unexpected shape so
    the board can say *why* instead of just showing nothing.
    """
    if spec == "list":
        if isinstance(body, list):
            return [r for r in body if isinstance(r, dict)], None
        return [], f"expected a JSON array, got {type(body).__name__}"
    if spec == "object":
        if isinstance(body, dict):
            data = body.get("data", body)
            if isinstance(data, dict):
                if not data:
                    # ``UWClient._get`` returns {} on a failed call: that is no
                    # rows, not one blank row.
                    return [], "empty response (the call failed or returned nothing)"
                return [data], None
            if isinstance(data, list):
                return [r for r in data if isinstance(r, dict)], None
        return [], f"expected a JSON object, got {type(body).__name__}"
    # spec == "data" (default envelope)
    if isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)], None
        if isinstance(data, dict):
            return [data], None
        if data is None and body:
            # No rows in the envelope is a legitimate empty answer for most
            # feeds — but if the payload is a bare dict of fields (no `data`
            # key at all) treat it as a single object so it is never lost.
            if "data" not in body:
                return [body], None
        return [], None
    if isinstance(body, list):
        return [r for r in body if isinstance(r, dict)], None
    return [], f"no rows found (body: {type(body).__name__})"


def run_tool(uw: UWClient, tool_key: str, raw_params: dict) -> ToolResult:
    """Validate params, call the endpoint, normalize to rows.

    Raises ``ToolError`` for unknown tools / bad params / unusable bodies — the
    caller turns that into a user-facing empty state, never a silent success.
    """
    tool = get_tool(tool_key)
    if tool is None:
        raise ToolError(f"Unknown tool '{tool_key}'. Pick one from the catalog.")

    schema = tool.get("params", [])
    path_tmpl: str = tool["path"]

    # Coerce + validate.
    coerced: dict[str, object] = {}
    for p in schema:
        name = p["name"]
        raw = raw_params.get(name)
        if raw is None or str(raw).strip() == "":
            # DF49: only an ABSENT param takes its default; a field the user cleared (explicit blank) is
            # sent without the filter. A required param keeps its default either way.
            cleared = raw is not None and not p.get("required")
            if (not cleared and p.get("default") is not None
                    and str(p.get("default", "")).strip() != ""):
                coerced[name] = _coerce(p["default"], p["kind"])
                continue
            if p.get("required"):
                raise ToolError(f"'{name}' is required for {tool['label']}.")
            continue
        coerced[name] = _coerce(raw, p["kind"])
        if p["kind"] == "select" and coerced[name] not in p.get("options", []):
            raise ToolError(f"'{coerced[name]}' is not an option for '{name}'.")

    # Fill in_path placeholders; drop them from the query string. A path value is
    # one encoded segment: "/", "?" and "#" cannot reach another endpoint, and a
    # bare "." / ".." (which the HTTP client normalises away) is refused (M12).
    query: dict = {}
    path = path_tmpl
    for name, val in coerced.items():
        ph = f"{{{name}}}"
        if ph in path:
            seg = quote(str(val), safe="")
            if seg in (".", ".."):
                raise ToolError(f"'{val}' is not a valid {name}.")
            path = path.replace(ph, seg)
        else:
            if isinstance(val, bool):
                query[name] = "true" if val else "false"
            else:
                query[name] = val

    body = uw.raw_endpoint(path, query or None)

    rows, err = _extract(body, tool.get("rows", "data"))

    # A credential rejection (401/403) recorded by the client explains an empty
    # body better than "no rows" ever could.
    http = getattr(uw, "auth_error", None)
    status = (http or {}).get("status") if isinstance(http, dict) else None

    return ToolResult(
        tool_key=tool_key,
        label=tool["label"],
        family=tool["family"],
        path=path,
        help=tool.get("help", ""),
        params_used={k: v for k, v in coerced.items() if v is not None},
        rows=rows,
        columns=_columns_from(rows),
        row_count=len(rows),
        error=err if not rows else None,
        http_status=status,
    )
