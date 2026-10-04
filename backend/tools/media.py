"""Media control.

The product spec asks for ``play_media`` / ``pause_media`` "where supported".
There is no portable way to control arbitrary players from Python, so this is
Windows-only and shells out to the media keys that every keyboard exposes —
which is the one mechanism that works regardless of which app is playing.

Because the keys go to whatever has focus, this is ``confirm``: the user should
know the assistant is about to send a keystroke to the foreground window.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from typing import Any, Dict, List

from tools.base import BaseTool, ToolResult, ToolValidationError, _prop
from tools.sandbox import encode_powershell_command

logger = logging.getLogger("Zaytoon.Tools.Media")

#: Named intents -> media key. Sent as a virtual-key press, so whichever player
#: currently owns the media session receives it.
_KEYS = {
    "play": 0xB3,        # VK_MEDIA_PLAY_PAUSE
    "pause": 0xB3,       # same key; players toggle
    "play_pause": 0xB3,
    "stop": 0xB2,        # VK_MEDIA_STOP
    "next": 0xB0,        # VK_MEDIA_NEXT_TRACK
    "previous": 0xB1,    # VK_MEDIA_PREV_TRACK
    "volume_up": 0xAF,   # VK_VOLUME_UP
    "volume_down": 0xAE,  # VK_VOLUME_DOWN
    "mute": 0xAD,        # VK_VOLUME_MUTE
}

#: Presses are 25ms apart: long enough for the shell to register each one.
_INTERVAL_MS = 25


class MediaControl(BaseTool):
    permission = "confirm"

    def name(self) -> str:
        return "play_media"

    def aliases(self) -> List[str]:
        return [
            "pause_media", "media_control", "next_track", "previous_track",
            "stop_media", "volume_up", "volume_down", "mute",
        ]

    def description(self) -> str:
        return (
            "Control media playback: play, pause, play_pause, stop, next, "
            "previous, volume_up, volume_down, mute. Sends the system media "
            "key, so it affects whichever player is active. Requires "
            "confirmation. Windows only."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": _prop(
                    "string", f"One of: {', '.join(sorted(_KEYS))}.", default="play_pause"
                ),
                "times": _prop(
                    "integer", "Repeat count, for volume steps.",
                    default=1, minimum=1, maximum=20,
                ),
            },
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        action = (params.get("action") or "play_pause").strip().lower()
        if action not in _KEYS:
            raise ToolValidationError(
                f"Unknown media action {action!r}. Available: {', '.join(sorted(_KEYS))}"
            )
        times = params.get("times", 1)
        if not isinstance(times, int) or isinstance(times, bool) or not 1 <= times <= 20:
            raise ToolValidationError("'times' must be an integer between 1 and 20")

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"action": params.get("action", "play_pause")}

    async def execute(self, action: str = "play_pause", times: int = 1, **_: Any) -> ToolResult:
        name = (action or "play_pause").strip().lower()
        key = _KEYS.get(name)
        if key is None:
            return ToolResult.fail(
                f"Unknown media action '{name}'. Available: {', '.join(sorted(_KEYS))}"
            )
        if os.name != "nt":
            return ToolResult.fail(
                "Media control is only implemented on Windows. "
                "On Linux, use your desktop's media keys or a player-specific tool."
            )

        count = max(1, min(int(times or 1), 20))
        # No interpolation of caller data into the script: the key code and
        # count are ints derived from a fixed table.
        script = (
            "Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;"
            "public class K{[DllImport(\"user32.dll\")] public static extern void "
            "keybd_event(byte b, byte s, uint f, UIntPtr e);}' ;"
            f"for($i=0;$i -lt {count};$i++){{"
            f"K::keybd_event(0x{key:02X},0,0,[UIntPtr]::Zero);"
            f"Start-Sleep -Milliseconds {_INTERVAL_MS};"
            f"K::keybd_event(0x{key:02X},0,2,[UIntPtr]::Zero);"
            f"Start-Sleep -Milliseconds {_INTERVAL_MS};}}"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-NonInteractive",
                "-EncodedCommand", encode_powershell_command(script),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
        except FileNotFoundError:
            return ToolResult.fail("powershell is not available on this system.")
        except (asyncio.TimeoutError, OSError) as e:
            return ToolResult.fail(f"Media control failed: {e}")

        if proc.returncode != 0:
            detail = err.decode("utf-8", errors="replace").strip()[:300]
            return ToolResult.fail(f"Media control failed: {detail}")
        suffix = f" x{count}" if count > 1 else ""
        return ToolResult.ok(f"Sent media {name}{suffix}", {"action": name, "times": count})


def build_tools() -> List[BaseTool]:
    return [MediaControl()]
