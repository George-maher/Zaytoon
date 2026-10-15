"""Every attribute the shell's test double needs, checked without a display.

Why this file exists
--------------------
`tests/test_app_wiring.py` builds a `ZaytoonApp` with its constructor skipped
and its collaborators replaced by recorders. That module imports
`customtkinter`, which needs a real `tkinter` and a display, so on a machine
without them pytest collects the whole file and moves on. The double can
therefore be *wrong* — missing an attribute the real constructor always sets —
with every local signal green.

That is not hypothetical. A second copy of the builder inside
`TestTurnScopedAudio` omitted `client`, `events`, `mode` and `shutting_down`, CI
(running on a host with Tk) failed with `'ZaytoonApp' object has no attribute
'client'`, and the local suite had already reported 370 passed / 2 skipped for
the same commit.

So this module checks the double **statically**: it reads `app/app.py` and
`tests/test_app_wiring.py` as source, and asserts that the attributes
`make_shell()` provides cover the state the shell class actually uses. It needs
no Tk, no display and no import of `app.app`, so it runs on every host —
including the one where the double is otherwise invisible.

What it cannot see: whether the values are *right*, only that the names exist.
Attribute presence is the failure mode that hides behind a collect skip;
wrongness is caught by the module that does run in CI.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
DESKTOP_DIR = TESTS_DIR.parent
APP_SOURCE = DESKTOP_DIR / "app" / "app.py"
WIRING_SOURCE = TESTS_DIR / "test_app_wiring.py"

#: Attributes the double may omit, each with the reason it may.
#:
#: These are not conveniences: every one of them either needs a display, needs a
#: subprocess, or is only meaningful once the window exists. Anything *not* in
#: this table has to be assigned by `make_shell()`, which is the whole point —
#: an omission without a reason is the defect.
UI_ONLY = {
    "root": "the Tk window itself; creating one needs a display",
    "backend": "the backend supervisor; it spawns a child process",
    "config": "window geometry and state persistence",
    "hotkeys": "global keyboard hooks registered with the OS",
    "_panel_positioned": "true only once the side panel has been placed",
}


def _self_attributes(node: ast.AST) -> set:
    """`self.x` names in `node`, split into state reads and method calls."""
    reads: set = set()
    calls: set = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Attribute):
            continue
        if not (isinstance(sub.value, ast.Name) and sub.value.id == "self"):
            continue
        if isinstance(sub.ctx, ast.Load) and not isinstance(sub.ctx, ast.Store):
            reads.add(sub.attr)
    # A `self.name(...)` is a method, not state; the parent class supplies some
    # (`quit`), and treating those as state would make this check meaningless.
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
            func = sub.func
            if isinstance(func.value, ast.Name) and func.value.id == "self":
                calls.add(func.attr)
    return reads - calls


def _assigned_attributes(body, receiver: str) -> set:
    out: set = set()
    for statement in body:
        for sub in ast.walk(statement):
            if isinstance(sub, ast.Assign):
                for target in sub.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == receiver
                    ):
                        out.add(target.attr)
            elif isinstance(sub, ast.AnnAssign):
                target = sub.target
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == receiver
                ):
                    out.add(target.attr)
    return out


def _load():
    app_tree = ast.parse(APP_SOURCE.read_text(encoding="utf-8"))
    shell_class = next(
        node
        for node in app_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ZaytoonApp"
    )
    init = next(
        node
        for node in shell_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    constructor_state = _assigned_attributes(init.body, "self")

    # Methods defined on the class are not state, whether they are called
    # (`self._set_status(...)`) or handed over as a callback
    # (`self.after(50, self._pump)`) — the second form is why name-based
    # detection alone is not enough.
    methods = {
        node.name
        for node in shell_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    used: set = set()
    for node in shell_class.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name != "__init__":
            used |= _self_attributes(node)
    used -= methods

    wiring_tree = ast.parse(WIRING_SOURCE.read_text(encoding="utf-8"))
    builder = next(
        node
        for node in wiring_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "make_shell"
    )
    return constructor_state, used, _assigned_attributes(builder.body, "shell")


class TestTheShellDoubleMatchesTheShell:
    def test_the_sources_are_where_this_test_thinks_they_are(self):
        assert APP_SOURCE.is_file(), f"missing {APP_SOURCE}"
        assert WIRING_SOURCE.is_file(), f"missing {WIRING_SOURCE}"

    def test_the_builder_provides_every_state_attribute_the_constructor_sets(self):
        constructor_state, _used, double = _load()

        missing = sorted(constructor_state - double - set(UI_ONLY))
        assert not missing, (
            "make_shell() does not set "
            f"{', '.join(missing)} — the real __init__ always does, so a shell "
            "built by the double raises AttributeError the moment a handler "
            "touches it. Add it to make_shell(), or to UI_ONLY with the reason "
            "it may be omitted."
        )

    def test_no_method_uses_state_the_builder_does_not_provide(self):
        """The stricter direction: a *new* attribute added to a handler.

        Adding `self.something` to an event branch and forgetting the double is
        the same defect in a different disguise, and it is the one a reviewer
        would not notice in a diff.
        """
        constructor_state, used, double = _load()

        unsupplied = sorted(used - constructor_state - double - set(UI_ONLY))
        assert not unsupplied, (
            f"the shell uses {', '.join(unsupplied)} but neither __init__ nor "
            "make_shell() provides it"
        )

    def test_every_exemption_carries_a_reason(self):
        # An allowlist that can be extended without a reason is a list that will
        # grow until it hides the defect it exists to narrow.
        assert len(UI_ONLY) <= 6
        for name, reason in UI_ONLY.items():
            assert isinstance(reason, str) and len(reason) > 20, (
                f"{name} needs a reason, not a placeholder"
            )

    def test_the_check_notices_a_removed_attribute(self):
        """Prove the check can fail, by removing an attribute on disk.

        A guard that cannot fail is worse than no guard: it certifies the thing
        it never examined. This rewrites the builder into a temp file with
        `client` dropped and asserts the same analysis rejects it.
        """
        source = WIRING_SOURCE.read_text(encoding="utf-8")
        without = source.replace("    shell.client = FakeClient()\n", "", 1)
        assert without != source, "the line this test removes no longer exists"

        tree = ast.parse(without)
        builder = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "make_shell"
        )
        double = _assigned_attributes(builder.body, "shell")
        constructor_state, _used, _ = _load()

        # `client` is read by handlers and not UI-only, so its absence has to be
        # visible in exactly the set the real test subtracts.
        assert "client" in constructor_state
        assert "client" not in double
        assert "client" not in UI_ONLY
        assert not constructor_state <= (double | set(UI_ONLY))


def test_this_module_does_not_import_the_shell():
    """The property that lets this file run where `test_app_wiring.py` cannot.

    If someone adds `from app.app import ZaytoonApp` here for convenience, this
    module becomes collect-skipped too — and it would be skipped on exactly the
    hosts that need it.

    Asked of a fresh interpreter rather than of this process's `sys.modules`:
    on a host that has Tk, another module in the same session imports `app.app`
    during collection, so the shared table would be populated for a reason that
    has nothing to do with this file, and the test would fail there while
    passing here. The question is only ever "does *importing this module* pull
    the shell in", and a clean interpreter is the only place that question has a
    clean answer. It is also a second, independent check of the same property:
    the module imports and runs with no Tk, no display and nothing installed.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            assert "app.app" not in stripped, (
                "importing app.app drags in customtkinter and disables this module"
            )

    probe = (
        "import importlib, sys\n"
        "importlib.import_module('tests.test_shell_doubles')\n"
        "dragged = sorted(m for m in sys.modules if m == 'app' or m.startswith('app.'))\n"
        "assert not dragged, dragged\n"
        "print('clean')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(DESKTOP_DIR),
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(DESKTOP_DIR)},
    )
    assert done.returncode == 0, f"importing this module dragged in {done.stdout}{done.stderr}"
    assert "clean" in done.stdout
