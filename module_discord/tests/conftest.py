from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "export_small.json"


@pytest.fixture
def export_data() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))
