"""The frozen executable must be able to import what the source tree can.

`scripts/build_exe.py` runs PyInstaller with an explicit `--hidden-import` list
plus `--paths desktop`. Anything reachable from `main.py` that is not in that list
is a `ModuleNotFoundError` the first time someone launches `Zaytoon.exe` — and
*every* test in this repo imports from source, so none of them can see it. This
file closes that hole by recomputing the import closure the same way PyInstaller
would and comparing it to the list in the build script.

It is a static check on purpose: it must run on a Linux runner that cannot freeze a
Windows GUI executable, and it must not depend on PyInstaller being installed.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

DESKTOP_DIR = Path(__file__).resolve().parents[1]
APP_DIR = DESKTOP_DIR / "app"
BUILD_SCRIPT = DESKTOP_DIR / "scripts" / "build_exe.py"

sys.path.insert(0, str(DESKTOP_DIR))


def _imports_of(tree: ast.AST) -> set[str]:
    """Every `app.*` / relative module name an AST mentions."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "app" or alias.name.startswith("app."):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: `from . import host`, `from .core import x`
                if node.module is not None:
                    found.add(f"app.{node.module}")
                else:
                    # `from . import host, paths` names the submodules themselves —
                    # exactly how app/bridge.py pulls in app.paths, so missing this
                    # would hide the very case this test exists to catch.
                    for alias in node.names:
                        found.add(f"app.{alias.name}")
            elif node.module == "app" or (node.module or "").startswith("app."):
                found.add(node.module)
    return found


def _closure(entry: Path) -> dict[str, Path]:
    """`app.*` modules reachable from `entry` by following imports."""
    pending: list[tuple[str, Path]] = []
    for name in _imports_of(ast.parse(entry.read_text(encoding="utf-8"))):
        pending.append((name, entry))
    seen: dict[str, Path] = {}
    while pending:
        name, origin = pending.pop()
        if name in seen or not name.startswith("app"):
            continue
        module_path = APP_DIR / f"{name.split('app.')[-1].replace('.', '/')}.py"
        if name == "app":
            module_path = APP_DIR / "__init__.py"
        if not module_path.is_file():
            # A subpackage (`app.core` is a module, `app.theme` a module) that does
            # not exist is itself a finding worth failing on, but it cannot be
            # followed — record it so the reader sees which file pulled it in.
            seen[name] = origin
            continue
        seen[name] = module_path
        for child in _imports_of(ast.parse(module_path.read_text(encoding="utf-8"))):
            pending.append((child, module_path))
    return seen


def _hidden_imports() -> list[str]:
    tree = ast.parse(BUILD_SCRIPT.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "HIDDEN_IMPORTS"
            for target in node.targets
        ):
            return list(ast.literal_eval(node.value))
    pytest.fail("build_exe.py no longer defines a literal HIDDEN_IMPORTS list")


def test_module_file_exists_for_every_hidden_import():
    """A stale entry is as broken as a missing one."""
    for name in _hidden_imports():
        if name == "webview":
            continue  # the third-party package, not ours
        relative = name.split("app.")[-1].replace(".", "/")
        assert (APP_DIR / f"{relative}.py").is_file() or (
            APP_DIR / relative / "__init__.py"
        ).is_file(), f"HIDDEN_IMPORTS lists {name}, which does not exist"


def test_hidden_imports_cover_what_main_reaches():
    closure = _closure(DESKTOP_DIR / "main.py")
    assert closure, "found no app.* imports from main.py — the walker is broken"
    declared = set(_hidden_imports())
    missing = sorted(
        name for name, path in closure.items() if name != "app" and name not in declared
    )
    assert not missing, (
        "the frozen --onefile build will raise ModuleNotFoundError for "
        f"{missing}: add them to HIDDEN_IMPORTS in scripts/build_exe.py. "
        + "; ".join(f"{m} <- {closure[m].relative_to(DESKTOP_DIR)}" for m in missing)
    )


def test_app_package_does_not_reexport_everything():
    """If `app/__init__.py` ever imports the whole package, hidden imports stop mattering —
    and the frozen build starts pulling in Tk. This test says so instead of letting
    the build silently change shape."""
    init = APP_DIR / "__init__.py"
    reachable = _imports_of(ast.parse(init.read_text(encoding="utf-8")))
    assert not any(name.startswith("app.") for name in reachable), (
        "app/__init__.py imports app submodules; the closure this suite checks is "
        "no longer the closure PyInstaller sees — update this test deliberately"
    )
