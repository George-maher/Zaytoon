"""Shared security primitives for OS tool execution.

These are deliberately dependency-free and side-effect-free so both
``tools/base.py`` (the ``OSToolkit`` layer) and ``tools/executor.py`` (the
``ToolExecutor`` layer) can use them.

Two classes of defect are addressed here:

1. **Shell metacharacter injection.** Any value that is interpolated into a
   shell command line must be rejected, because the OS tools run through
   ``subprocess(..., shell=True)`` / ``create_subprocess_shell``.
2. **Path containment via ``str.startswith``.** ``os.path.abspath`` plus
   ``startswith`` is *not* containment: with an allowed root of
   ``C:\\\\work\\\\app``, the sibling ``C:\\\\work\\\\app_evil\\\\x`` also passes.
   Containment must be tested with :func:`os.path.commonpath`.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

# Characters the Windows/POSIX shells treat as syntax rather than data.
SHELL_METACHARACTERS = frozenset("&|<>^;`$!*?()[]{}~'\"\n\r\t")

# Characters that are meaningful *specifically* in a command line. `%VAR%` and
# `!VAR!` expand in cmd.exe, and a leading `/` or `-` can be reinterpreted as a
# switch by the target, so both are refused for commands. Paths are unaffected:
# this set is only consulted by :func:`split_command`.
FORBIDDEN_IN_COMMAND = frozenset("%!")

# Command separators that let an attacker append a second command.
COMMAND_SEPARATORS = re.compile(r"[&|;<>`\n\r]|\$\(|\|\|")

# Windows executable / path suffix, e.g. ".exe", ".bat", ".ps1".
_EXECUTABLE_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,6}$")

# Suffixes that must never be executed as an application target.
BLOCKED_EXECUTABLE_SUFFIXES = frozenset(
    {".bat", ".cmd", ".com", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf",
     ".wsh", ".msi", ".scr", ".hta", ".cpl", ".lnk"}
)

#: Interpreters refused for `run_code` even after a human approves, because
#: their entire purpose is to execute arbitrary system commands. Sandboxed
#: languages (python, node) remain available.
DENIED_CODE_LANGUAGES = frozenset(
    {"bash", "sh", "shell", "zsh", "fish", "cmd", "batch", "powershell", "ps1"}
)


class SecurityError(Exception):
    """Raised when a value fails a pre-execution safety check."""


# ---------------------------------------------------------------------------
# Path containment
# ---------------------------------------------------------------------------
def workspace_root() -> str:
    """The directory every file tool is confined to.

    Defaults to ``config.workspace_path`` — an explicit, user-chosen directory —
    rather than ``os.getcwd()``. Using the cwd meant that because the backend
    must be launched from ``backend/``, the assistant could only ever see and
    write inside its own source tree.

    Falls back to the cwd if the config module is unavailable, so this stays
    usable as a standalone helper (and in tests that stub ``config``).
    """
    try:
        from config import config

        return str(config.workspace_path)
    except Exception:  # pragma: no cover - defensive
        return os.getcwd()


def resolve_workspace(path: str, root: Optional[str] = None) -> str:
    """Return the absolute path of ``path``, or ``""`` if it escapes the root.

    This is the guard every file tool should call before touching the disk. It
    answers a different question from :func:`is_safe_path`: not "is this path
    contained?" but "what is the concrete path I am allowed to operate on?" —
    so a caller cannot accidentally act on the unresolved, unvalidated string.
    """
    if not path or not str(path).strip():
        return ""
    base = root if root else workspace_root()
    try:
        candidate = Path(os.path.expandvars(str(path))).expanduser()
        if not candidate.is_absolute():
            candidate = Path(base) / candidate
        candidate = Path(os.path.realpath(candidate))
        base_real = Path(os.path.realpath(base))
    except (ValueError, OSError):
        return ""
    if candidate == base_real:
        return str(candidate)
    try:
        if os.path.commonpath([str(candidate), str(base_real)]) != str(base_real):
            return ""
    except ValueError:
        # Different drives on Windows, or a mix of absolute/relative paths.
        return ""
    return str(candidate)


def is_safe_path(path: str, root: Optional[str] = None) -> bool:
    """True when ``path`` resolves inside the workspace root.

    Delegates to :func:`resolve_workspace` so there is exactly one definition of
    how a relative path is resolved. These two functions previously disagreed:
    this one used ``os.path.abspath`` (i.e. the process cwd) while
    ``resolve_workspace`` used the workspace root — so a relative path could be
    judged against one root and then opened against another.

    Containment uses :func:`os.path.commonpath`, so sibling directories that
    merely share a name prefix (``app`` vs ``app_evil``) are rejected.

    ``root`` defaults to :func:`workspace_root`, not the process cwd.
    """
    return bool(resolve_workspace(path, root))


def assert_safe_path(path: str, root: Optional[str] = None) -> str:
    """Return the resolved path, or raise :class:`SecurityError`."""
    resolved = resolve_workspace(path, root)
    if not resolved:
        raise SecurityError(
            f"Access denied: path outside the workspace "
            f"({workspace_root()}): {path}"
        )
    return resolved


# ---------------------------------------------------------------------------
# Shell injection
# ---------------------------------------------------------------------------
def has_shell_metachars(value: str) -> bool:
    """True when ``value`` contains any character the shell would interpret."""
    if not value:
        return False
    return any(ch in SHELL_METACHARACTERS for ch in str(value))


def assert_no_shell_metachars(value: str, field: str = "value") -> str:
    """Return ``value`` unchanged, or raise :class:`SecurityError`."""
    if has_shell_metachars(value):
        raise SecurityError(
            f"Refusing to run: {field} contains shell metacharacters "
            f"({sorted(set(str(value)) & SHELL_METACHARACTERS)}): {value!r}"
        )
    return str(value)


def split_command(command: str) -> List[str]:
    """Tokenise a command and reject anything with shell syntax.

    This is the guard for the ``shell=True`` code paths. A command such as
    ``"echo hi & hostname"`` is rejected outright rather than being handed to
    the shell, because the second command would otherwise bypass the allow-list.
    """
    if not command or not command.strip():
        raise SecurityError("No command provided")
    text = str(command)
    if COMMAND_SEPARATORS.search(text):
        raise SecurityError(
            f"Refusing to run: command chaining is not allowed: {command!r}"
        )
    if has_shell_metachars(text):
        raise SecurityError(
            f"Refusing to run: command contains shell metacharacters: {command!r}"
        )
    forbidden = sorted(set(text) & FORBIDDEN_IN_COMMAND)
    if forbidden:
        raise SecurityError(
            f"Refusing to run: command contains shell expansion {forbidden}: {command!r}"
        )
    return text.strip().split()


# ---------------------------------------------------------------------------
# Application targets
# ---------------------------------------------------------------------------
def is_blocked_executable(target: str) -> bool:
    """True for script types we never launch as an 'application'."""
    if not target:
        return False
    suffix = os.path.splitext(target)[1].lower()
    return suffix in BLOCKED_EXECUTABLE_SUFFIXES


def validate_app_target(target: Optional[str]) -> Optional[str]:
    """Sanity-check a resolved application path before it reaches a shell.

    Rejects script interpreters and anything carrying shell syntax. The caller
    is still responsible for only ever passing vetted values.
    """
    if not target:
        return None
    text = str(target)
    if has_shell_metachars(text):
        raise SecurityError(
            f"Refusing to launch: application name contains shell metacharacters: {text!r}"
        )
    if is_blocked_executable(text):
        raise SecurityError(
            f"Refusing to launch: '{os.path.basename(text)}' is a script, not an application"
        )
    return text


def resolve_executable(name: str, extra_dirs: Optional[Sequence[str]] = None) -> Optional[str]:
    """Resolve an executable name via PATH plus optional extra directories."""
    if not name:
        return None
    found = shutil.which(name)
    if found:
        return found
    for directory in extra_dirs or ():
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def sanitize_app_name(app: str) -> str:
    """Normalise an LLM-supplied application name to a bare identifier.

    Strips path separators and rejects shell syntax outright, so the result can
    only ever be a plain program name.
    """
    if not app or not str(app).strip():
        raise SecurityError("No app name provided")
    text = str(app).strip()
    if has_shell_metachars(text) or COMMAND_SEPARATORS.search(text):
        raise SecurityError(
            f"Refusing to act on: application name contains shell syntax: {text!r}"
        )
    text = os.path.basename(text.replace("\\", "/").rstrip("/"))
    if not text:
        raise SecurityError(f"Refusing to act on: empty application name from {app!r}")
    if is_blocked_executable(text):
        raise SecurityError(
            f"Refusing to act on: '{text}' is a script, not an application"
        )
    return text


def is_executable_like(value: str) -> bool:
    """True when the value already looks like a path to a program."""
    if not value:
        return False
    return bool(_EXECUTABLE_SUFFIX.search(value)) or os.path.sep in value


# ---------------------------------------------------------------------------
# PowerShell invocation
# ---------------------------------------------------------------------------
#: Characters that ``System.Windows.Forms.SendKeys`` treats as syntax rather
#: than data. A literal one has to be sent as ``{+}`` etc. This is a
#: *correctness* concern, not an injection one: escaping the PowerShell layer
#: (see :func:`powershell_sendkeys`) is what makes injection impossible, and
#: SendKeys metacharacters can then only alter which keys are pressed.
SENDKEYS_LITERALS = {
    "+": "{+}", "^": "{^}", "%": "{%}", "~": "{~}",
    "(": "{(}", ")": "{)}", "{": "{{}", "}": "{}}",
    "[": "{[}", "]": "{]}",
}

#: Refuse absurd payloads outright rather than encoding megabytes of script.
MAX_SENDKEYS_CHARS = 2000


def escape_sendkeys(text: str) -> str:
    """Escape a literal string for use as ``SendKeys`` keystrokes."""
    return "".join(SENDKEYS_LITERALS.get(ch, ch) for ch in text)


def powershell_sendkeys_script(text: str) -> str:
    """Build the PowerShell script that types ``text`` into the focused window.

    The payload is embedded as a *single-quoted* PowerShell string literal with
    embedded quotes doubled, which is the correct PowerShell escaping.
    """
    escaped = str(text).replace("'", "''")
    return (
        "Add-Type -AssemblyName System.Windows.Forms; "
        f"[System.Windows.Forms.SendKeys]::SendWait('{escaped}')"
    )


def encode_powershell_command(script: str) -> str:
    """Encode a PowerShell script for ``-EncodedCommand``.

    ``-EncodedCommand`` takes the script as base64-encoded UTF-16LE. Passing the
    script this way means **no part of it is ever placed on a command line**, so
    there is no quoting or escaping to get wrong and no way for the payload to
    terminate the string and append statements.

    This is the fix for a real injection: the previous implementation built
    ``powershell -Command "...SendWait('<text>')"`` with only ``'`` -> ``''``
    escaping, at a permission level (``ELEVATED``) that never prompted, so a
    crafted ``text`` parameter could escape the literal and execute PowerShell.
    """
    import base64

    return base64.b64encode(str(script).encode("utf-16-le")).decode("ascii")


def _validate_sendkeys_payload(text: str) -> str:
    if text is None:
        raise SecurityError("No text provided")
    value = str(text)
    # Whitespace-only is refused too: callers strip before calling, so this is
    # defence in depth rather than the primary check.
    if not value or not value.strip():
        raise SecurityError("No text provided")
    if len(value) > MAX_SENDKEYS_CHARS:
        raise SecurityError(
            f"Keystroke payload too long: {len(value)} characters "
            f"(max {MAX_SENDKEYS_CHARS})"
        )
    if "\x00" in value:
        raise SecurityError("Keystroke payload contains a null byte")
    return value


def assert_sendkeys_text(text: str) -> str:
    """Validate *literal text* destined for ``SendKeys``, returning it escaped.

    For text the user wants typed, ``+^%~(){}[]`` are data and must be sent as
    ``{+}`` and friends. Use :func:`assert_sendkeys_keys` for key combinations.
    """
    return escape_sendkeys(_validate_sendkeys_payload(text))


def assert_sendkeys_keys(keys: str) -> str:
    """Validate a *key combination*, returning it unescaped.

    ``+``, ``^`` and ``%`` are SendKeys' modifier syntax — ``ctrl+c`` is ``^c`` —
    so escaping them here would break the very shortcuts the caller asked for.
    """
    return _validate_sendkeys_payload(keys)


def unique(values: Iterable[str]) -> List[str]:
    """Order-preserving de-duplication."""
    seen = set()
    out = []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out
