"""The right-hand telemetry side panel.

Two halves, deliberately separated:

* **parsing and sampling** are module-level pure functions (plus two small
  ctypes wrappers that return ``None`` instead of raising) so they can be
  tested without a display, without a backend and without ``psutil``;
* the widget only renders strings it is handed.

Nothing here ever runs on the Tk thread except the ``set_*`` calls: CPU/RAM are
sampled and ``GET /tools`` / ``GET /audit`` / ``GET /processes`` are fetched
with ``urllib`` on the app's worker thread (see ``app.app``).
"""

from __future__ import annotations

import ctypes
import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable

import customtkinter as ctk

from . import theme

#: Panel width at the wide end of its allowed range (280-320).
PANEL_WIDTH = 300

#: How often the worker thread is allowed to sample the OS, in seconds. Kept
#: above the pump tick so sampling can never become per-frame work.
SAMPLE_INTERVAL = 3.0

#: Text shown when a value is unavailable. Never guess a number.
UNKNOWN = "—"


# ---------------------------------------------------------------------------
# JSON over urllib (no new dependencies)
# ---------------------------------------------------------------------------

def _default_opener(url: str, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return response.read()


def decode_json(raw: Any) -> Any | None:
    """Decode a body into JSON, returning ``None`` for anything unusable.

    An endpoint that does not exist yet (``/audit``, ``/processes``) answers
    with HTML or a 404 page, and a truncated body raises — all of which must
    read as "no data", never as an exception on the worker thread.
    """
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw  # already-parsed objects pass through (fake openers in tests)


def fetch_json(url: str, timeout: float = 3.0, opener=None) -> Any | None:
    """GET ``url`` and parse JSON. Never raises; failure is ``None``.

    ``opener`` is injectable so the tolerance paths are testable without
    touching the network.
    """
    open_url = opener or _default_opener
    try:
        raw = open_url(url, timeout)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    except Exception:  # pragma: no cover - defensive: a worker must not die
        return None
    return decode_json(raw)


# ---------------------------------------------------------------------------
# response parsers — tolerant of shape, strict about honesty
# ---------------------------------------------------------------------------

def parse_tools(payload: Any) -> list[str]:
    """``GET /tools`` -> tool names.

    Accepts ``{"tools": [...]}`` or a bare list, and each entry may be a name,
    a dict with ``name``/``action``, or a mapping key. Anything unrecognised is
    skipped rather than stringified into the UI.
    """
    if payload is None:
        return []
    if isinstance(payload, dict):
        payload = payload.get("tools", payload.get("by_tier", []))
    if isinstance(payload, dict):
        # by_tier: {"safe": [...], "confirm": [...]}
        names: list[str] = []
        for _tier, group in payload.items():
            names.extend(parse_tools(group))
        return names
    if not isinstance(payload, (list, tuple)):
        return []
    out: list[str] = []
    for entry in payload:
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict):
            name = entry.get("name") or entry.get("action") or entry.get("id")
            name = str(name) if name else ""
        else:
            continue
        if name and name not in out:
            out.append(name)
    return out


def parse_model(payload: Any) -> str:
    """``GET /health`` -> the model the backend is actually using.

    Reads the head of ``llm.chain``; falls back to the routing table; returns
    ``""`` when nothing names a model (the header then shows ``MODEL: —``).
    """
    if not isinstance(payload, dict):
        return ""
    llm = payload.get("llm")
    if isinstance(llm, dict):
        chain = llm.get("chain")
        if isinstance(chain, list) and chain:
            head = chain[0]
            if isinstance(head, dict) and head.get("model"):
                return str(head["model"])
        routing = llm.get("routing")
        if isinstance(routing, dict):
            for tier in ("fast", "reasoning", "general", "tools"):
                entry = routing.get(tier)
                if isinstance(entry, dict):
                    models = entry.get("models")
                    if isinstance(models, list) and models:
                        first = models[0]
                        if isinstance(first, dict) and first.get("model"):
                            return str(first["model"])
                        if isinstance(first, str):
                            return first
    return ""


def parse_audit(payload: Any, limit: int = 8) -> list[str]:
    """``GET /audit`` -> the newest few actions as display strings.

    The endpoint is optional on the backend: absence, HTML, or an unexpected
    shape all read as an empty list, which the panel shows as a muted dash.
    """
    entries = _entries(payload, ("audit", "actions", "entries", "items", "log"))
    out: list[str] = []
    for entry in entries[:limit]:
        if isinstance(entry, str):
            text = entry
        elif isinstance(entry, dict):
            action = entry.get("action") or entry.get("tool") or entry.get("command") or ""
            detail = entry.get("summary") or entry.get("detail") or entry.get("result") or ""
            stamp = entry.get("time") or entry.get("at") or entry.get("timestamp") or ""
            text = " · ".join(str(part) for part in (stamp, action, detail) if part)
        else:
            continue
        if text.strip():
            out.append(text.strip())
    return out


def parse_processes(payload: Any, limit: int = 8) -> list[str]:
    """``GET /processes`` -> display strings, or ``[]`` when unimplemented."""
    entries = _entries(payload, ("processes", "procs", "items"))
    out: list[str] = []
    for entry in entries[:limit]:
        if isinstance(entry, str):
            out.append(entry)
        elif isinstance(entry, dict):
            name = entry.get("name") or entry.get("process") or entry.get("cmd") or ""
            pid = entry.get("pid")
            cpu = entry.get("cpu") or entry.get("cpu_percent")
            bits = [str(name)] if name else []
            if pid is not None:
                bits.append(f"pid {pid}")
            if cpu is not None:
                bits.append(f"{cpu}%")
            if bits:
                out.append(" · ".join(bits))
    return out


def _entries(payload: Any, keys: tuple[str, ...]) -> list[Any]:
    if payload is None:
        return []
    if isinstance(payload, dict):
        for key in keys:
            if key in payload and isinstance(payload[key], (list, tuple)):
                return list(payload[key])
        return []
    if isinstance(payload, (list, tuple)):
        return list(payload)
    return []


def summarize_names(names: list[str], limit: int = 10) -> str:
    """``a, b, c`` + ``+7 more`` — the side panel never overflows."""
    if not names:
        return UNKNOWN
    head = ", ".join(names[:limit])
    extra = len(names) - limit
    return f"{head}  +{extra} more" if extra > 0 else head


# ---------------------------------------------------------------------------
# system sampling (ctypes, no psutil)
# ---------------------------------------------------------------------------

class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


def read_times() -> tuple[int, int, int] | None:
    """``GetSystemTimes`` -> ``(idle, kernel, user)`` in 100ns units.

    ``kernel`` includes ``idle``, which is why the maths in
    :func:`cpu_percent` subtracts it twice. Returns ``None`` on any failure —
    the caller then shows ``—`` instead of a fabricated percentage.
    """
    try:
        idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
        return (_ft2int(idle), _ft2int(kernel), _ft2int(user))
    except Exception:  # pragma: no cover - non-Windows or an exotic build
        return None


def _ft2int(value: _FILETIME) -> int:
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


def read_memory() -> int | None:
    """``GlobalMemoryStatusEx`` -> memory load percentage, or ``None``."""
    try:
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return int(status.dwMemoryLoad)
    except Exception:  # pragma: no cover - non-Windows
        return None


def cpu_percent(before, after) -> float | None:
    """Busy % between two :func:`read_times` samples.

    ``(total - idle) / total`` over the delta, clamped to 0-100. Returns
    ``None`` when either sample is missing or the delta is empty (first sample,
    a clock hiccup, or a platform without the API).
    """
    if not before or not after:
        return None
    deltas = [a - b for a, b in zip(after, before)]
    if len(deltas) != 3 or any(d < 0 for d in deltas):
        return None
    idle, kernel, user = deltas
    total = kernel + user  # kernel already contains idle
    if total <= 0:
        return None
    busy = (total - idle) / total
    return round(max(0.0, min(1.0, busy)) * 100.0, 1)


def ram_percent(load) -> float | None:
    """Normalise the memory load the API returned, or ``None``."""
    if load is None:
        return None
    try:
        return round(max(0.0, min(100.0, float(load))), 1)
    except (TypeError, ValueError):
        return None


def format_percent(value: float | None) -> str:
    return UNKNOWN if value is None else f"{value:.1f}%"


class SystemSampler:
    """CPU/RAM sampling with a floor on how often it may run.

    Owned by the worker thread. ``times_reader``/``memory_reader`` are
    injectable, which is how the tests drive it without touching the OS.
    """

    def __init__(
        self,
        *,
        times_reader: Callable[[], Any] = read_times,
        memory_reader: Callable[[], Any] = read_memory,
        min_interval: float = SAMPLE_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._times_reader = times_reader
        self._memory_reader = memory_reader
        self._min_interval = float(min_interval)
        self._clock = clock
        self._previous = None
        self._last_sample = 0.0

    def due(self, now: float | None = None) -> bool:
        """True when enough time has passed for another sample."""
        now = self._clock() if now is None else now
        return (now - self._last_sample) >= self._min_interval

    def sample(self, now: float | None = None) -> dict:
        """Take one sample. Always returns a dict; values may be ``None``."""
        now = self._clock() if now is None else now
        current = self._times_reader()
        percent = cpu_percent(self._previous, current)
        self._previous = current
        self._last_sample = now
        return {"cpu": percent, "ram": ram_percent(self._memory_reader())}


# ---------------------------------------------------------------------------
# the widget
# ---------------------------------------------------------------------------

def _card(parent, title: str) -> tuple[ctk.CTkFrame, ctk.CTkFrame]:
    """A raised card with a letter-spaced uppercase section label."""
    card = ctk.CTkFrame(
        parent, fg_color=theme.BG2,
        corner_radius=theme.RADIUS_CARD,
        border_width=1, border_color=theme.HAIRLINE,
    )
    card.pack(fill="x", padx=theme.SPACE_2, pady=(0, theme.SPACE_2))
    ctk.CTkLabel(
        card, text=theme.letterspace(title), anchor="w",
        font=(theme.FONT_MONO, theme.FONT_SECTION, "bold"),
        text_color=theme.TEXT_FAINT,
    ).pack(fill="x", padx=theme.SPACE_3, pady=(theme.SPACE_2, 0))
    body = ctk.CTkFrame(card, fg_color="transparent", height=1)
    body.pack(fill="x", padx=theme.SPACE_3, pady=(2, theme.SPACE_2))
    return card, body


def _line(parent, text: str, *, color: str = theme.TEXT_BASE, mono: bool = True):
    label = ctk.CTkLabel(
        parent, text=text, anchor="w", justify="left",
        font=(theme.FONT_MONO if mono else theme.FONT_SANS, theme.FONT_BODY_SM),
        text_color=color,
    )
    label.pack(fill="x")
    return label


class SidePanel(ctk.CTkFrame):
    """Model / task / tools / system / recent actions / processes."""

    def __init__(self, master, **kwargs) -> None:
        super().__init__(
            master, fg_color=theme.BG1, width=PANEL_WIDTH,
            corner_radius=theme.RADIUS_PANEL,
            border_width=1, border_color=theme.HAIRLINE,
            **kwargs,
        )
        self.grid_propagate(False)

        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        scroll.pack(fill="both", expand=True)
        self._body = scroll

        _card_body = _card(scroll, "ACTIVE MODEL")
        self._model = _line(_card_body[1], UNKNOWN, color=theme.VIOLET)

        _task_card = _card(scroll, "CURRENT TASK")
        self._task = _line(_task_card[1], UNKNOWN)

        _sys_card = _card(scroll, "SYSTEM")
        sys_body = _sys_card[1]
        self._cpu = _line(sys_body, f"CPU   {UNKNOWN}")
        self._ram = _line(sys_body, f"RAM   {UNKNOWN}")

        _tools_card = _card(scroll, "TOOLS")
        self._tools = _line(_tools_card[1], UNKNOWN, color=theme.TEXT_MUTE)

        _actions_card = _card(scroll, "RECENT ACTIONS")
        self._actions: list[ctk.CTkLabel] = []
        self._actions_body = _actions_card[1]
        self._actions_empty = _line(self._actions_body, UNKNOWN, color=theme.TEXT_FAINT)

        _proc_card = _card(scroll, "PROCESSES")
        self._proc_card = _proc_card[0]
        self._processes: list[ctk.CTkLabel] = []
        self._proc_body = _proc_card[1]
        self._proc_empty = _line(self._proc_body, UNKNOWN, color=theme.TEXT_FAINT)
        # The endpoint may not exist yet: hide the card until it says otherwise.
        self._proc_card.pack_forget()

    # -- updates ----------------------------------------------------------
    def set_model(self, name: str) -> None:
        self._model.configure(text=name or UNKNOWN)

    def set_task(self, intent: str, status: str) -> None:
        parts = [str(p) for p in (intent, status) if p]
        self._task.configure(text="  ·  ".join(parts) if parts else UNKNOWN)

    def set_tools(self, names: list[str]) -> None:
        self._tools.configure(text=summarize_names(names))

    def set_system(self, cpu: str = UNKNOWN, ram: str = UNKNOWN) -> None:
        self._cpu.configure(text=f"CPU   {cpu}")
        self._ram.configure(text=f"RAM   {ram}")

    def set_actions(self, items: list[str]) -> None:
        self._fill(self._actions_body, self._actions, self._actions_empty, items)

    def set_processes(self, items: list[str]) -> None:
        if items:
            self._proc_card.pack(fill="x", padx=theme.SPACE_2, pady=(0, theme.SPACE_2))
        self._fill(self._proc_body, self._processes, self._proc_empty, items)

    def _fill(self, body, pool: list, empty_label, items: list[str]) -> None:
        """Recycle labels rather than rebuilding them every update."""
        if not items:
            for label in pool:
                label.pack_forget()
            if not empty_label.winfo_ismapped():
                empty_label.pack(fill="x")
            return
        empty_label.pack_forget()
        for index, text in enumerate(items):
            if index >= len(pool):
                pool.append(_line(body, "", color=theme.TEXT_MUTE))
            label = pool[index]
            label.configure(text=text)
            if not label.winfo_ismapped():
                label.pack(fill="x")
        for label in pool[len(items):]:
            label.pack_forget()
