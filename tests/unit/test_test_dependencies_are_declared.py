"""Every third-party module the tests import must be reachable from a DECLARED dependency.

`tests/integration/conftest.py` imported `qrcode` -- inside a fixture, so collection
succeeded and only EXECUTION failed -- and it was declared nowhere. The whole integration
tree was therefore unrunnable in any environment built from this pyproject, and CI never
noticed because it runs only `tests/unit`, `tests/cli` and `tests/http`.

Two things this deliberately does NOT do, because both are how the same defect gets
through again:

* It does not rely on `--collect-only`. A function-level import is not executed at
  collection, so collection of that conftest succeeded with `qrcode` absent.

* It does not accept "the module imports fine here" as evidence. Importability is a
  property of the machine running the test, not of the declaration: an undeclared package
  that is already installed -- left over in a venv, or preinstalled on a CI runner --
  imports perfectly while a fresh environment built from this pyproject still cannot run
  the tests. Allowance is therefore derived from the declared requirements' own dependency
  metadata: a module is acceptable only if some distribution that provides it is in the
  transitive closure of what pyproject declares.
"""
from __future__ import annotations

import ast
import importlib.metadata as md
import pathlib
import re
import sys
import tomllib

from packaging.requirements import Requirement

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_FIRST_PARTY = {"clippyshot", "tests", "conftest"}


def _canon(name: str) -> str:
    """PEP 503 canonical distribution name."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _declared_roots() -> list[Requirement]:
    """Every requirement pyproject declares: runtime plus every optional group."""
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]
    specs = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        specs.extend(group)
    return [Requirement(s) for s in specs]


def _closure(roots: list[Requirement]) -> set[str]:
    """Canonical names of every distribution reachable from the declared requirements.

    Walks each installed distribution's own `Requires-Dist` metadata, honouring the
    extras that were asked for -- so `blastbox[host]` pulls in what that extra requires
    and nothing more. A declared distribution that is not installed still counts as
    declared; we simply cannot walk through it.
    """
    seen: set[str] = set()
    queue = [(_canon(r.name), frozenset(r.extras)) for r in roots]
    visited: set[tuple[str, frozenset[str]]] = set()
    while queue:
        name, extras = queue.pop()
        if (name, extras) in visited:
            continue
        visited.add((name, extras))
        seen.add(name)
        try:
            requires = md.requires(name) or []
        except md.PackageNotFoundError:
            continue  # declared but not installed here -- still declared
        for spec in requires:
            try:
                req = Requirement(spec)
            except Exception:  # pragma: no cover - malformed metadata in the wild
                continue
            if req.marker is not None:
                # A dependency guarded by `extra == "x"` applies only when x was asked
                # for; an unguarded one must hold in this environment.
                if not any(
                    req.marker.evaluate({"extra": e}) for e in (extras or frozenset({""}))
                ):
                    continue
            queue.append((_canon(req.name), frozenset(req.extras)))
    return seen


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
    closure = _closure(_declared_roots())
    providers = md.packages_distributions()
    stdlib = set(sys.stdlib_module_names)
    offenders = []
    for mod, where in sorted(_imported_modules().items()):
        if mod in stdlib or mod in _FIRST_PARTY:
            continue
        if _canon(mod) in closure:
            continue                       # declared, or required by something declared
        dists = [d for d in providers.get(mod, []) if _canon(d) in closure]
        if dists:
            continue                       # the module's own distribution is in the closure
        installed = providers.get(mod)
        why = (
            f"installed as {sorted(installed)}, which nothing declared requires"
            if installed
            else "not provided by any installed distribution"
        )
        offenders.append(f"{mod} (imported by {where}; {why})")
    assert not offenders, (
        "these modules are imported by the tests but are not reachable from any dependency "
        "pyproject declares, so an environment built from this pyproject cannot run the "
        "tests: " + "; ".join(offenders)
    )
