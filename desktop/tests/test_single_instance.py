"""The shell owns global resources, so a second launch must not fight it.

A leftover instance holding ``Ctrl+Space`` made the next launch die with a
``BaseException`` raised inside the ``keyboard`` package while it built its
key-name tables -- not a clean error. These tests pin both halves of the fix:
the hook failure is survivable, and a second launch is refused politely.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import hotkeys as hotkeys_mod  # noqa: E402
from app.hotkeys import HotkeyManager  # noqa: E402
from app.instance import SingleInstance, ALREADY_RUNNING_MESSAGE  # noqa: E402


@pytest.fixture(autouse=True)
def _a_keyboard_to_patch(monkeypatch):
    """Stand in for the `keyboard` package where it cannot be imported.

    `app.hotkeys` sets `keyboard = None` when the package is unusable — correct
    production behaviour, since the library needs a low-level OS hook (root on
    Linux) — but it left these four tests calling `setattr(None, "add_hotkey", …)`.
    They therefore passed only on a host that had the package installed: Windows,
    the one leg whose output nobody could read. With a stand-in, the guarantee
    "a failed bind degrades and reports" is checked on every platform.
    """
    if hotkeys_mod.keyboard is None:
        fake = types.SimpleNamespace(
            add_hotkey=lambda *_a, **_k: True,
            unhook_all_hotkeys=lambda: None,
        )
        monkeypatch.setattr(hotkeys_mod, "keyboard", fake)


class TestHookFailureIsSurvivable:
    """AGENTS.md promises a hotkey failure degrades rather than crashing."""

    def test_any_exception_from_the_hook_is_caught(self, monkeypatch):
        class Boom(Exception):
            pass

        def explode(*_a, **_k):
            raise Boom("name table build failed")

        monkeypatch.setattr(hotkeys_mod.keyboard, "add_hotkey", explode)
        monkeypatch.setattr(hotkeys_mod.keyboard, "unhook_all_hotkeys", lambda: None, raising=False)

        m = HotkeyManager()
        assert m.register("ctrl+space", lambda: None) is False
        assert m.error, "a failed bind must set an explanatory error"

    def test_base_exceptions_are_caught_too(self, monkeypatch):
        """The real-world failure was not a plain Exception.

        `except Exception` let it through, which is exactly the regression
        these tests exist to prevent. `KeyboardInterrupt` and `SystemExit`
        are still honoured, because those genuinely should stop the process.
        """

        class Weird(BaseException):
            pass

        monkeypatch.setattr(
            hotkeys_mod.keyboard, "add_hotkey",
            lambda *_a, **_k: (_ for _ in ()).throw(Weird("hook died")),
        )
        monkeypatch.setattr(hotkeys_mod.keyboard, "unhook_all_hotkeys", lambda: None, raising=False)

        m = HotkeyManager()
        assert m.register("alt+z", lambda: None) is False

    def test_ctrl_c_still_stops_the_process(self, monkeypatch):
        def interrupt(*_a, **_k):
            raise KeyboardInterrupt

        monkeypatch.setattr(hotkeys_mod.keyboard, "add_hotkey", interrupt)
        m = HotkeyManager()
        with pytest.raises(KeyboardInterrupt):
            m.register("ctrl+space", lambda: None)

    def test_a_failed_bind_is_not_recorded_as_registered(self, monkeypatch):
        monkeypatch.setattr(
            hotkeys_mod.keyboard, "add_hotkey",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("busy")),
        )
        monkeypatch.setattr(hotkeys_mod.keyboard, "unhook_all_hotkeys", lambda: None, raising=False)
        m = HotkeyManager()
        m.register("ctrl+space", lambda: None)
        assert m.bound() == [], "a hotkey that failed to bind must not appear bound"


class TestSingleInstance:
    def test_first_claim_succeeds(self):
        guard = SingleInstance("Local\\Zaytoon.Test.A")
        try:
            assert guard.acquire() is True
            assert guard.acquired is True
        finally:
            guard.release()

    def test_second_claim_is_refused(self):
        first = SingleInstance("Local\\Zaytoon.Test.B")
        second = SingleInstance("Local\\Zaytoon.Test.B")
        try:
            assert first.acquire() is True
            assert second.acquire() is False, (
                "a second shell must not start; it would contend for the hotkey"
            )
        finally:
            first.release()

    def test_releasing_allows_a_later_claim(self):
        name = "Local\\Zaytoon.Test.C"
        first = SingleInstance(name)
        assert first.acquire() is True
        first.release()
        second = SingleInstance(name)
        try:
            assert second.acquire() is True, "the lock must not outlive the process"
        finally:
            second.release()

    def test_release_is_idempotent(self):
        guard = SingleInstance("Local\\Zaytoon.Test.D")
        guard.acquire()
        guard.release()
        guard.release()  # must not raise

    def test_message_points_at_the_hotkey_and_the_tray(self):
        assert "Ctrl+Space" in ALREADY_RUNNING_MESSAGE
        assert "tray" in ALREADY_RUNNING_MESSAGE.lower()