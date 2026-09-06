"""Every third-party module the tests import must be declared, or the tests cannot run.

`tests/integration/conftest.py` imported `qrcode` -- inside a fixture, so collection
succeeded and only EXECUTION failed -- and it was declared nowhere. The whole integration
tree was therefore unrunnable in any environment built from this pyproject, and CI never
noticed because it runs only `tests/unit`, `tests/cli` and `tests/http`.

`--collect-only` does not catch this: a function-level import is not executed at
collection. That is why this walks the AST instead, and why it asserts on the DECLARATION
rather than on an import that happens to work on the machine running it.
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import re
import sys
import tomllib

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_FIRST_PARTY = {"clippyshot", "tests", "conftest"}


def _declared() -> set[str]:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    project = data["project"]
    groups = [project.get("dependencies", [])]
    groups += list(project.get("optional-dependencies", {}).values())
    names = set()
    for group in groups:
        for spec in group:
            name = re.split(r"[<>=!~\[; ]", spec)[0].strip().lower()
            if name:
                names.add(name)
                names.add(name.replace("-", "_"))
    return names


def _imported_modules() -> dict[str, str]:
    """Top-level module name -> the first file that imports it, from the whole test tree."""
    found: dict[str, str] = {}
    for path in sorted((_ROOT / "tests").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:  # pragma: no cover - a broken test file fails elsewhere
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:            # relative import: first-party by construction
                    continue
                mods = [(node.module or "").split(".")[0]]
            else:
                continue
            for mod in mods:
                if mod and mod not in found:
                    found[mod] = str(path.relative_to(_ROOT))
    return found


def test_every_third_party_test_import_is_declared_or_provided():
    declared = _declared()
    stdlib = set(sys.stdlib_module_names)
    offenders = []
    for mod, where in sorted(_imported_modules().items()):
        if mod in stdlib or mod in _FIRST_PARTY:
            continue
        if mod.lower() in declared:
            continue
        # Not named in pyproject, but present anyway -> a transitive dependency of
        # something that IS declared (fastapi arrives with blastbox[host]). That is
        # provided, if implicitly, so it does not break a fresh environment.
        if importlib.util.find_spec(mod) is not None:
            continue
        offenders.append(f"{mod} (imported by {where})")
    assert not offenders, (
        "these modules are imported by the tests but declared nowhere in pyproject, so "
        "a fresh environment cannot run them: " + "; ".join(offenders)
    )
