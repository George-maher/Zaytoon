"""Live CPU / memory / disk figures for the status panel.

No third-party dependency: ``psutil`` would be a new install for four
numbers. Windows reads the kernel counters (``GetSystemTimes``,
``GlobalMemoryStatusEx``), Linux reads ``/proc``, and anything else reports
``unavailable`` **with the reason** — an explicit state, never a fabricated
zero that reads as "idle".

Two honesty rules are structural here:

* **CPU is a delta.** The OS counters are cumulative, so a single reading is
  not a percentage. The first call stores the sample and reports
  ``status: "sampling"``; the next call has something to divide. A panel that
  showed ``0%`` instead would be lying at exactly the moment the machine was
  busiest.
* **Network is explicit.** Counters without a collector would mean a new
  dependency, so the state is ``unavailable`` with a reason rather than a
  silent omission the UI would fill with a dash and call a metric.
"""

from __future__ import annotations

import ctypes
import platform
import sys
import shutil
import time
from typing import Any, Dict, Optional, Tuple

#: ``(monotonic_ts, total_ticks, busy_ticks)`` of the previous CPU reading.
_PREV: Optional[Tuple[float, int, int]] = None


def _filetime_value(filetime: "ctypes.Structure") -> int:
    return (int(filetime.dwHighDateTime) << 32) | int(filetime.dwLowDateTime)


def _cpu_ticks() -> Optional[Tuple[int, int]]:
    """``(total, busy)`` cumulative counters, or ``None`` if unsupported."""
    system = platform.system()
    if system == "Windows":
        class FILETIME(ctypes.Structure):
            _fields_ = [
                ("dwLowDateTime", ctypes.c_uint32),
                ("dwHighDateTime", ctypes.c_uint32),
            ]

        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        ok = ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        )
        if not ok:
            return None
        # Kernel time *includes* idle time on Windows.
        total = _filetime_value(kernel) + _filetime_value(user)
        busy = total - _filetime_value(idle)
        return total, max(busy, 0)
    if system == "Linux":
        try:
            with open("/proc/stat", "r", encoding="ascii") as handle:
                parts = handle.readline().split()[1:]
            numbers = [int(p) for p in parts]
            total = sum(numbers)
            # fields: user nice system idle iowait irq softirq steal …
            idle = numbers[3] + (numbers[4] if len(numbers) > 4 else 0)
            return total, max(total - idle, 0)
        except (OSError, ValueError, IndexError):
            return None
    if system == "Darwin":
        reading = _darwin_cpu_ticks()
        if reading is not None:
            return reading
    return None


def parse_cp_time(text: str) -> Optional[Tuple[int, int]]:
    """`kern.cp_time` → (total, busy) ticks. Pure, so it is testable anywhere.

    Fields are user, nice, system, idle, irq — the same order Linux reports in
    /proc/stat, minus the extras older kernels add.
    """
    try:
        numbers = [int(part) for part in text.split()]
    except ValueError:
        return None
    if len(numbers) < 4:
        return None
    total = sum(numbers)
    idle = numbers[3]
    return total, max(total - idle, 0)


def _darwin_cpu_ticks() -> Optional[Tuple[int, int]]:
    """macOS has no /proc; `sysctl kern.cp_time` is the same counter."""
    text = _run(["sysctl", "-n", "kern.cp_time"])
    return parse_cp_time(text) if text else None


def parse_vm_stat(
    text: str, page_size: int, total_bytes: int
) -> Optional[Dict[str, Any]]:
    """`vm_stat` page counts → the same shape the Linux branch returns.

    Two traps here. One: the compressor label is "Pages occupied by compressor",
    so matching a key called "compressed" silently drops tens of gigabytes on a
    MacBook — which is precisely the machine this branch exists for. Two: the
    keys must match the other platforms (`used_bytes`/`total_bytes`/`percent`),
    or every consumer of `/system` has to know which OS it is running on.

    A missing label counts as zero, but a file with none of the three labels we
    need yields ``None``, so an unparseable ``vm_stat`` degrades to
    "unavailable" instead of a confident 0%.
    """
    if page_size <= 0 or total_bytes <= 0:
        return None
    pages: Dict[str, int] = {}
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        fields = rest.split()
        if not fields:
            continue
        # `vm_stat` terminates each count with a period ("Pages free:  1234.").
        token = fields[0].rstrip(".")
        if token.isdigit():
            pages[" ".join(key.split()).lower()] = int(token)
    wanted = ("pages active", "pages wired down", "pages occupied by compressor")
    if not any(label in pages for label in wanted):
        return None
    used_pages = sum(pages.get(label, 0) for label in wanted)
    used_pages -= pages.get("pages purgeable", 0)
    used_pages = max(used_pages, 0)
    used = min(used_pages * page_size, total_bytes)
    return {
        "percent": round(100.0 * used / total_bytes, 1),
        "used_bytes": used,
        "total_bytes": total_bytes,
    }


def _run(argv: List[str]) -> str:
    """Run one fixed-argv system query. ``""`` means "treat as unavailable".

    No shell, no user input in the argv, and a short timeout: a health probe must
    not be able to hang the caller. A missing binary is the normal case on
    another OS, so it is silence here and an honest "unavailable" upstream —
    never an exception out of `/health`.
    """
    import subprocess

    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, capture_output=True, text=True, timeout=3.0, shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _cpu() -> Dict[str, Any]:
    global _PREV
    reading = _cpu_ticks()
    if reading is None:
        return {
            "percent": None,
            "status": "unavailable",
            "reason": f"no CPU counters for {platform.system()}",
        }
    now_total, now_busy = reading
    ts = time.monotonic()
    previous = _PREV
    _PREV = (ts, now_total, now_busy)
    if previous is None:
        # First reading: store the sample, report honestly that there is
        # nothing to divide yet.
        return {"percent": None, "status": "sampling"}
    prev_ts, prev_total, prev_busy = previous
    delta_total = now_total - prev_total
    delta_busy = now_busy - prev_busy
    if ts <= prev_ts or delta_total <= 0:
        # No interval to divide by — honest, not zero.
        return {"percent": None, "status": "sampling"}
    percent = 100.0 * max(0, min(delta_busy, delta_total)) / delta_total
    return {"percent": round(percent, 1), "status": "ok"}


def _memory() -> Dict[str, Any]:
    system = platform.system()
    if system == "Windows":
        class MEMORYSTATUSEX(ctypes.Structure):
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

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return {"status": "unavailable", "reason": "GlobalMemoryStatusEx failed"}
        total = int(status.ullTotalPhys)
        avail = int(status.ullAvailPhys)
        return {
            "percent": int(status.dwMemoryLoad),
            "used_bytes": max(total - avail, 0),
            "total_bytes": total,
            "status": "ok",
        }
    if system == "Darwin":
        text = _run(["vm_stat"])
        total_text = _run(["sysctl", "-n", "hw.memsize"])
        page_text = _run(["sysctl", "-n", "vm.page_size"])
        if text and total_text.isdigit():
            try:
                page = int(page_text)
            except ValueError:
                page = 4096
            measured = parse_vm_stat(text, page, int(total_text))
            if measured is not None:
                measured["free_bytes"] = max(measured["total_bytes"] - measured["used_bytes"], 0)
                return {**measured, "status": "ok"}
        return {
            "status": "unavailable",
            "reason": "vm_stat/hw.memsize unavailable",
        }
    if system == "Linux":
        try:
            values: Dict[str, int] = {}
            with open("/proc/meminfo", "r", encoding="ascii") as handle:
                for line in handle:
                    key, _, rest = line.partition(":")
                    fields = rest.split()
                    if fields and fields[0].isdigit():
                        values[key] = int(fields[0]) * 1024  # kB → bytes
            total = values.get("MemTotal", 0)
            avail = values.get("MemAvailable", 0)
            if total <= 0:
                raise ValueError("MemTotal missing")
            return {
                "percent": round(100.0 * (total - avail) / total, 1),
                "used_bytes": max(total - avail, 0),
                "total_bytes": total,
                "status": "ok",
            }
        except (OSError, ValueError):
            return {"status": "unavailable", "reason": "/proc/meminfo unreadable"}
    return {"status": "unavailable", "reason": f"no memory counters for {system}"}


def _disk(root: str) -> Dict[str, Any]:
    try:
        usage = shutil.disk_usage(root)
    except OSError as exc:
        return {"status": "unavailable", "reason": type(exc).__name__}
    percent = round(100.0 * usage.used / usage.total, 1) if usage.total else None
    return {
        "percent": percent,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "path": root,
        "status": "ok",
    }


def sample(root: str) -> Dict[str, Any]:
    """A snapshot of what the machine is doing, for ``GET /system``.

    ``root`` is the workspace — the disk figures answer "is the drive I am
    working on filling up?", which is the only disk question this product
    can act on.
    """
    return {
        "cpu": _cpu(),
        "memory": _memory(),
        "disk": _disk(root),
        "network": {
            "status": "unavailable",
            "reason": "network counters are not collected",
        },
    }


__all__ = ["sample"]
