"""The needs layer was ``rcdata.api`` until #329. Nothing may import the old name, and its HTTP routes stay gone."""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
OLD_IMPORT = re.compile(r"rcdata\.api\b|from\s+(?:RevenueChaser\.)?rcdata\s+import\s+[^\n]*\bapi\b|from\s+\.api\b")
SKIP = {"needs"}


def _python_files():
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if any(part.startswith(".") or part in ("__pycache__", "node_modules") for part in rel.parts):
            continue
        yield path, rel


def test_no_module_imports_the_old_rcdata_api():
    here = pathlib.Path(__file__).resolve()
    offenders = []
    for path, rel in _python_files():
        if path.resolve() == here:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            code = line.split("#", 1)[0]
            if OLD_IMPORT.search(code) and not code.lstrip().startswith(('"', "'")):
                offenders.append(f"{rel}:{n}: {line.strip()}")
    assert not offenders, "the package is rcdata.needs now (#329):\n" + "\n".join(offenders)


def test_the_old_package_directory_is_gone():
    assert not (ROOT / "rcdata" / "api").exists()
    assert (ROOT / "rcdata" / "needs" / "serve.py").exists()


def test_the_old_api_data_routes_are_gone():
    from RevenueChaser.app import dashboard
    rules = {r.rule for r in dashboard.app.url_map.iter_rules()}
    assert "/needs" in rules and "/needs/<path:need>" in rules
    assert not {r for r in rules if r == "/api/data" or r.startswith("/api/data/")}, "old routes came back"
    assert "/api/data_asof/<mod>" in rules, "the unrelated provenance route stays"
