"""Device discovery coordinator.

Orchestrates the discovery of OS information, installed applications,
running processes, development environment, projects, and local AI capabilities.

This module is platform-aware: Windows-specific adapters are provided,
and the architecture is designed to be extensible to other platforms.
"""

from __future__ import annotations

import asyncio
import csv
import getpass
import io
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config import config
from tools.sandbox import is_safe_path, workspace_root

logger = logging.getLogger("Zaytoon.Discovery")


def _default_data_dir() -> str:
    """Where discovery's JSON/SQLite state belongs.

    ``core.paths.data_dir()`` so the portable layout (``ZAYTOON_HOME/Data``)
    and the frozen bundle's per-user directory are both honoured. The previous
    answer was ``__file__``-relative — ``<backend>/data`` — which is read-only
    under Program Files and wiped by a repair. Local import: ``core.paths``
    imports nothing from this package, but keeping it out of module scope
    means a broken core package degrades to the old path instead of failing
    the import of the whole discovery layer.
    """
    try:
        from core import paths as _paths

        return str(_paths.data_dir())
    except Exception:  # pragma: no cover - defensive
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


# ---------------------------------------------------------------------------
# Discovery result types
# ---------------------------------------------------------------------------

@dataclass
class HardwareInfo:
    """Discovered hardware information."""
    cpu_model: str = ""
    logical_cores: int = 0
    total_ram_bytes: int = 0
    available_ram_bytes: int = 0
    gpu_info: str = ""
    os_family: str = ""
    os_version: str = ""
    os_architecture: str = ""


@dataclass
class ApplicationInfo:
    """A discovered application, with the artifact a launcher needs.

    ``target`` is the whole point of the record. The previous version recorded
    only a display name and a source, so the inventory could say "Chrome is
    installed" and had nothing to launch — every discovered application was
    informational and the resolver still had to guess. An empty ``target`` means
    "seen, but no launch artifact could be verified"; that is a fact worth
    recording and must not be turned into a guess.

    ``verified`` is *positive evidence, checked at discovery time*: the target
    exists on disk right now (and, for a shortcut, is inside a Start Menu root).
    It is re-checked at launch, because an application can be uninstalled
    between a scan and a request.
    """

    name: str
    display_name: str
    launch_location: str = ""  # start_menu | registry | path | configured | bundle
    version: str = ""
    confidence: float = 1.0  # 0.0 = uncertain, 1.0 = high confidence
    discovery_source: str = ""  # e.g. "path", "start_menu", "app_paths"
    verified: bool = False
    #: Executable, Start Menu shortcut, `.app` bundle, or registered shell URI.
    target: str = ""
    #: The `.lnk` this record came from, when it came from one.
    shortcut_path: str = ""
    #: Installation directory when Windows advertises one.
    install_location: str = ""
    #: ISO timestamp of the scan that produced this record.
    last_seen: str = ""



@dataclass
class ProcessInfo:
    """Discovered running process information."""
    pid: int
    name: str
    executable: str
    cmdline: str
    window_title: str = ""
    is_system_process: bool = False


@dataclass
class DevelopmentTool:
    """Discovered development tool."""
    name: str
    version: str = ""
    executable_path: str = ""
    available: bool = False
    available_via_path: bool = False
    confidence: float = 1.0


@dataclass
class ProjectInfo:
    """Discovered project information."""
    root: str
    name: str = ""
    display_name: str = ""
    languages: List[str] = field(default_factory=list)
    manifests: Dict[str, str] = field(default_factory=dict)  # manifest_type -> path
    git_status: Optional[Dict[str, Any]] = None
    available_runtimes: List[str] = field(default_factory=list)


@dataclass
class LocalAIModel:
    """Discovered local AI model (e.g., Ollama)."""
    name: str
    endpoint: str
    version: str = ""
    size_bytes: int = 0
    capabilities: List[str] = field(default_factory=list)


@dataclass
class DeviceInventory:
    """Complete device inventory with discovery metadata."""
    timestamp: datetime
    hardware: HardwareInfo
    applications: List[ApplicationInfo] = field(default_factory=list)
    running_processes: List[ProcessInfo] = field(default_factory=list)
    development_tools: Dict[str, DevelopmentTool] = field(default_factory=dict)
    projects: List[ProjectInfo] = field(default_factory=list)
    local_ai: LocalAIModel = field(
        default_factory=lambda: LocalAIModel(name="", endpoint="")
    )
    discovery_scopes: List[str] = field(default_factory=list)
    inventory_version: str = "1.0"
    errors: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Operating system adapter
# ---------------------------------------------------------------------------

class OperatingSystemProvider:
    """Abstract base for OS-specific discovery adapters."""

    def discover_hardware(self) -> HardwareInfo:
        raise NotImplementedError

    def discover_installed_applications(self) -> List[ApplicationInfo]:
        raise NotImplementedError

    def discover_running_processes(self) -> List[ProcessInfo]:
        raise NotImplementedError

    def discover_development_environment(self) -> Dict[str, DevelopmentTool]:
        raise NotImplementedError

    def discover_projects(self) -> List[ProjectInfo]:
        raise NotImplementedError

    def discover_local_ai(self) -> LocalAIModel:
        raise NotImplementedError


class WindowsProvider(OperatingSystemProvider):
    """Windows-specific discovery adapter."""

    def _get_cpu_info(self) -> Tuple[str, int]:
        """Get CPU model and logical core count."""
        try:
            import psutil
            cpu_model = psutil.cpu_info().get("model", "") or psutil.cpu_freq().get(
                "model", ""
            ) or platform.processor() or ""
            logical_cores = psutil.cpu_count(logical=True) or 0
            return cpu_model, logical_cores
        except ImportError:
            pass

        # Fallback to platform
        cpu_model = platform.processor() or ""
        logical_cores = os.cpu_count() or 0
        return cpu_model, logical_cores

    def _get_ram_info(self) -> Tuple[int, int]:
        """Get total and available RAM in bytes."""
        try:
            import psutil
            mem = psutil.virtual_memory()
            return mem.total, mem.available
        except ImportError:
            pass

        # Fallback: read from /proc/meminfo on Linux or GlobalMemoryStatusEx on Windows
        # For now, just use os.sysconf if available
        try:
            page_size = os.sysconf("SC_PAGE_SIZE")
            phys_pages = os.sysconf("SC_PHYS_PAGES")
            avail_pages = os.sysconf("SC_AVPHYS_PAGES")
            total = page_size * phys_pages
            available = page_size * avail_pages
            return total, available
        except (AttributeError, ValueError):
            pass

        return 0, 0

    def _get_os_info(self) -> Tuple[str, str, str]:
        """Get OS family, version, and architecture."""
        system = platform.system()
        release = platform.release()
        machine = platform.machine()

        if system == "Windows":
            family = "windows"
        elif system == "Darwin":
            family = "macos"
        else:
            family = "linux"

        return family, release, machine

    def discover_hardware(self) -> HardwareInfo:
        """Discover Windows hardware information."""
        cpu_model, logical_cores = self._get_cpu_info()
        total_ram, available_ram = self._get_ram_info()
        family, os_version, arch = self._get_os_info()

        # Try to get GPU info
        gpu_info = ""
        try:
            import wmi
            w = wmi.WMI(namespace="root\cimv2")
            for gpu in w.Win32_VideoController():
                name = gpu.Name or ""
                if name:
                    gpu_info = name
                    break
        except Exception:
            pass

        return HardwareInfo(
            cpu_model=cpu_model,
            logical_cores=logical_cores,
            total_ram_bytes=total_ram,
            available_ram_bytes=available_ram,
            gpu_info=gpu_info,
            os_family=family,
            os_version=os_version,
            os_architecture=arch,
        )

    def discover_installed_applications(self) -> List[ApplicationInfo]:
        """Discover launchable applications from every source Windows offers.

        Five independent sources, each of which can fail on its own without
        taking the others down: Start Menu shortcuts (user + machine), the
        ``App Paths`` registry hive, the uninstall/registration entries, the
        ``PATH``, and explicitly configured portable directories
        (``ZAYTOON_APP_DIRS``). The old implementation walked the Start Menu and
        PATH, recorded **no launch target at all**, and inverted the PATH filter
        so it admitted everything *except* executables — which is why an
        inventory could list 994 "applications" and still be unable to open one.
        """
        applications: List[ApplicationInfo] = []
        stamp = datetime.now().isoformat(timespec="seconds")

        loaders = (
            ("start_menu", self._discover_start_menu_apps),
            ("app_paths", self._discover_registered_app_paths),
            ("registry", self._discover_registered_applications),
            ("path", self._discover_path_applications),
            ("configured", self._discover_configured_app_dirs),
        )
        for label, loader in loaders:
            try:
                applications.extend(loader(stamp))
            except Exception as e:  # one source failing must not hide the rest
                logger.warning("%s application discovery failed: %s", label, e)

        if not applications:
            logger.info(
                "Application discovery found nothing; supported sources were "
                "Start Menu, App Paths, installed-app registrations, PATH and "
                "ZAYTOON_APP_DIRS"
            )
        return self._dedupe_applications(applications)

    # -- source 1: Start Menu shortcuts (user and machine) -----------------
    def _discover_start_menu_apps(self, stamp: str) -> List[ApplicationInfo]:
        found: List[ApplicationInfo] = []
        for stem, path in self._build_shortcut_index(self._get_start_menu_roots()):
            display = _clean_display_name(stem)
            if not display or _is_noise_shortcut(display):
                continue
            found.append(
                ApplicationInfo(
                    name=_app_key(display),
                    display_name=display,
                    launch_location="start_menu",
                    confidence=0.95,
                    discovery_source="start_menu",
                    verified=os.path.isfile(path),
                    target=path,
                    shortcut_path=path,
                    last_seen=stamp,
                )
            )
        return found

    # -- source 2: HKCU/HKLM App Paths --------------------------------------
    def _discover_registered_app_paths(self, stamp: str) -> List[ApplicationInfo]:
        found: List[ApplicationInfo] = []
        for hive, view, base in _app_path_registry_locations():
            try:
                import winreg

                with winreg.OpenKey(hive, base, 0, winreg.KEY_READ | view) as key:
                    count = winreg.QueryInfoKey(key)[0]
                    for index in range(count):
                        try:
                            sub_name = winreg.EnumKey(key, index)
                        except OSError:
                            continue
                        try:
                            with winreg.OpenKey(key, sub_name) as sub:
                                raw, _ = winreg.QueryValueEx(sub, "")
                        except OSError:
                            continue
                        exe = _clean_registry_path(raw)
                        if not exe or not exe.lower().endswith(".exe"):
                            continue
                        display = os.path.splitext(sub_name)[0]
                        found.append(
                            ApplicationInfo(
                                name=_app_key(display),
                                display_name=display,
                                launch_location="registry",
                                confidence=0.95,
                                discovery_source="app_paths",
                                verified=os.path.isfile(exe),
                                target=exe,
                                install_location=os.path.dirname(exe),
                                last_seen=stamp,
                            )
                        )
            except (ImportError, OSError):
                # A missing hive/view is normal (WOW6432Node on 64-bit, HKCU on
                # a fresh profile) and is not a failure worth reporting.
                continue
        return found

    # -- source 3: application registration / uninstall entries ------------
    def _discover_registered_applications(self, stamp: str) -> List[ApplicationInfo]:
        found: List[ApplicationInfo] = []
        for hive, view, base in _uninstall_registry_locations():
            try:
                import winreg

                with winreg.OpenKey(hive, base, 0, winreg.KEY_READ | view) as key:
                    count = winreg.QueryInfoKey(key)[0]
                    for index in range(count):
                        try:
                            with winreg.OpenKey(key, winreg.EnumKey(key, index)) as sub:
                                values = _read_registry_values(sub)
                        except OSError:
                            continue
                        display = (values.get("DisplayName") or "").strip()
                        if not display or _is_noise_registration(display):
                            continue
                        icon = _clean_registry_path(values.get("DisplayIcon") or "")
                        install = _clean_registry_path(
                            values.get("InstallLocation") or ""
                        )
                        target = ""
                        if icon and icon.lower().endswith(".exe"):
                            target = icon
                        elif install:
                            # The directory is real information, but it is not a
                            # launch target and we refuse to guess an executable
                            # inside it.
                            target = ""
                        found.append(
                            ApplicationInfo(
                                name=_app_key(display),
                                display_name=display,
                                launch_location="registry",
                                version=(values.get("DisplayVersion") or "").strip(),
                                confidence=0.8 if target else 0.4,
                                discovery_source="registration",
                                verified=bool(target and os.path.isfile(target)),
                                target=target,
                                install_location=install if os.path.isdir(install) else "",
                                last_seen=stamp,
                            )
                        )
            except (ImportError, OSError):
                continue
        return found

    # -- source 4: PATH -----------------------------------------------------
    def _discover_path_applications(self, stamp: str) -> List[ApplicationInfo]:
        """Executables reachable through ``PATH``.

        The Windows directory is excluded. Every service host and system
        utility lives in ``System32``, so including it turned "installed
        applications" into a list of a thousand binaries the user never
        installed and could not recognise — and made the inventory slower for
        no benefit. Utilities that *are* part of the product's own table
        (``cmd``, ``powershell``) are still resolvable by name at launch; this
        list is a catalogue, not the launcher's only source.

        File versions are not read here. Probing 800 executables cost roughly
        twenty seconds on a cold process (each probe attempted an unavailable
        import), which is the difference between a listing that answers and one
        that looks hung.
        """
        found: List[ApplicationInfo] = []
        seen: set[str] = set()
        for entry in os.environ.get("PATH", "").split(os.pathsep):
            entry = entry.strip().strip('"')
            if not entry or not os.path.isdir(entry) or _is_windows_dir(entry):
                continue
            key = os.path.normcase(os.path.realpath(entry))
            if key in seen:
                continue
            seen.add(key)
            try:
                names = os.listdir(entry)
            except (PermissionError, OSError):
                continue
            for name in names:
                if not self._is_likely_application(name):
                    continue
                full = os.path.join(entry, name)
                if not os.path.isfile(full):
                    continue
                display = os.path.splitext(name)[0]
                found.append(
                    ApplicationInfo(
                        name=_app_key(display),
                        display_name=display,
                        launch_location="path",
                        confidence=0.75,
                        discovery_source="path",
                        verified=os.path.isfile(full),
                        target=full,
                        last_seen=stamp,
                    )
                )
        return found

    # -- source 5: explicitly configured portable directories --------------
    def _discover_configured_app_dirs(self, stamp: str) -> List[ApplicationInfo]:
        found: List[ApplicationInfo] = []
        for raw_root in (os.environ.get("ZAYTOON_APP_DIRS") or "").split(os.pathsep):
            root = raw_root.strip().strip('"')
            if not root or not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                depth = os.path.relpath(dirpath, root).count(os.sep)
                if depth >= 2:
                    dirnames.clear()
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
                for name in filenames:
                    if not name.lower().endswith((".exe", ".lnk")):
                        continue
                    full = os.path.join(dirpath, name)
                    display = _clean_display_name(os.path.splitext(name)[0])
                    if not display:
                        continue
                    found.append(
                        ApplicationInfo(
                            name=_app_key(display),
                            display_name=display,
                            launch_location="configured",
                            confidence=0.85,
                            discovery_source="configured",
                            verified=os.path.isfile(full),
                            target=full,
                            shortcut_path=full if name.lower().endswith(".lnk") else "",
                            last_seen=stamp,
                        )
                    )
        return found

    @staticmethod
    def _dedupe_applications(
        applications: Sequence[ApplicationInfo],
    ) -> List[ApplicationInfo]:
        """One record per normalised name, preferring a verified target.

        Two sources describing the same application is normal — "Google
        Chrome" appears in the Start Menu *and* under ``App Paths``. Two
        records for it are not: a resolver then has to choose between them, and
        the one it picks may be the targetless registration entry. The merge
        keeps the best target and fills in metadata the winner was missing.
        """
        best: Dict[str, ApplicationInfo] = {}
        for app in applications:
            key = _app_key(app.display_name or app.name)
            if not key:
                continue
            current = best.get(key)
            if current is None:
                best[key] = app
                continue
            # A verified target beats an unverified one; a real target beats a
            # blank one; otherwise the more confident source wins.
            incumbent = (
                bool(current.target and current.verified),
                bool(current.target),
                current.confidence,
            )
            challenger = (
                bool(app.target and app.verified),
                bool(app.target),
                app.confidence,
            )
            winner, loser = (app, current) if challenger > incumbent else (current, app)
            for attr in ("version", "install_location", "shortcut_path", "last_seen"):
                if not getattr(winner, attr) and getattr(loser, attr):
                    setattr(winner, attr, getattr(loser, attr))
            best[key] = winner
        return sorted(
            best.values(),
            key=lambda a: (not a.verified, not bool(a.target), a.display_name.lower()),
        )


    def _get_start_menu_roots(self) -> List[str]:
        """Start Menu roots — delegated, so discovery and the launcher agree."""
        from tools.applications import start_menu_roots

        return start_menu_roots()

    def _build_shortcut_index(self, roots: List[str]) -> List[Tuple[str, str]]:
        """``(shortcut stem, path)`` for every Start Menu shortcut."""
        from tools.applications import build_shortcut_index

        return build_shortcut_index(roots)

    def _is_likely_application(self, name: str) -> bool:
        """Whether a PATH entry is an application we can launch directly.

        This used to return ``False`` for ``.exe`` and ``True`` for everything
        else — the exact inverse — so PATH discovery admitted text files and
        rejected every real program. Shortcuts are not on PATH; script types
        are refused at launch, so neither belongs in this answer.
        """
        return name.lower().endswith(".exe")

    def discover_running_processes(self) -> List[ProcessInfo]:
        """Discover running processes on Windows."""
        processes: List[ProcessInfo] = []
        try:
            import psutil
            for proc in psutil.process_iter(["pid", "name", "exe", "cmdline", "title"]):
                try:
                    info = proc.info
                    pid = info.get("pid", 0)
                    name = info.get("name", "") or ""
                    exe = info.get("exe", "") or ""
                    cmdline = info.get("cmdline", "") or ""
                    title = info.get("title", "") or ""

                    # Skip system processes we don't need to expose
                    is_system = name.lower() in {
                        "smss.exe", "csrss.exe", "wininit.exe", "services.exe",
                        "lsass.exe", "svchost.exe", "explorer.exe", "dwm.exe",
                        "rundll32.exe", "services.exe",
                    }

                    processes.append(
                        ProcessInfo(
                            pid=pid,
                            name=name,
                            executable=exe,
                            cmdline=cmdline,
                            window_title=title,
                            is_system_process=is_system,
                        )
                    )
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except ImportError:
            # Fallback without psutil: `tasklist` is present on every Windows
            # install and takes an argument vector. `wmic` is deprecated and
            # absent from current images, so the previous fallback printed
            # "'wmic' is not recognized" into the backend log and returned an
            # empty process list.
            try:
                result = subprocess.run(
                    ["tasklist", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    shell=False,
                )
                for row in csv.reader(io.StringIO(result.stdout or "")):
                    if len(row) < 2:
                        continue
                    try:
                        pid = int(row[1])
                    except ValueError:
                        continue
                    processes.append(
                        ProcessInfo(
                            pid=pid,
                            name=row[0],
                            executable=row[0],
                            cmdline="",
                            is_system_process=False,
                        )
                    )
            except (OSError, subprocess.SubprocessError, csv.Error):
                pass

        # Deduplicate by PID
        seen: set[int] = set()
        deduped: List[ProcessInfo] = []
        for p in processes:
            if p.pid not in seen:
                seen.add(p.pid)
                deduped.append(p)
        return deduped

    def discover_development_environment(self) -> Dict[str, DevelopmentTool]:
        """Discover development tools on Windows."""
        tools: Dict[str, DevelopmentTool] = {}

        # Python
        python_tool = self._find_python()
        if python_tool:
            tools["python"] = python_tool

        # Node.js
        node_tool = self._find_node()
        if node_tool:
            tools["nodejs"] = node_tool

        # npm
        npm_tool = self._find_npm()
        if npm_tool:
            tools["npm"] = npm_tool

        # Git
        git_tool = self._find_git()
        if git_tool:
            tools["git"] = git_tool

        # Java
        java_tool = self._find_java()
        if java_tool:
            tools["java"] = java_tool

        # Go
        go_tool = self._find_go()
        if go_tool:
            tools["go"] = go_tool

        # Rust
        rust_tool = self._find_rust()
        if rust_tool:
            tools["rust"] = rust_tool

        # Docker
        docker_tool = self._find_docker()
        if docker_tool:
            tools["docker"] = docker_tool

        # Ollama
        ollama_tool = self._find_ollama()
        if ollama_tool:
            tools["ollama"] = ollama_tool

        return tools

    def _find_python(self) -> Optional[DevelopmentTool]:
        """Find Python interpreter."""
        alias = "python"
        found = shutil.which(alias) or shutil.which(alias + ".exe")
        if found:
            return DevelopmentTool(
                name="python",
                executable_path=found,
                available=True,
                available_via_path=True,
                confidence=1.0,
            )
        # Check for python3
        found = shutil.which("python3") or shutil.which("python3.exe")
        if found:
            return DevelopmentTool(
                name="python",
                executable_path=found,
                available=True,
                available_via_path=True,
                confidence=1.0,
            )
        return None

    def _find_node(self) -> Optional[DevelopmentTool]:
        """Find Node.js interpreter."""
        found = shutil.which("node") or shutil.which("node.exe")
        if found:
            version = _tool_version(found, "--version").strip().lstrip("v")
            return DevelopmentTool(
                name="nodejs",
                executable_path=found,
                available=True,
                available_via_path=True,
                version=version,
                confidence=1.0,
            )
        return None

    def _find_npm(self) -> Optional[DevelopmentTool]:
        """Find npm."""
        found = shutil.which("npm")
        if found:
            version = _tool_version(found, "--version").strip().lstrip("v")
            return DevelopmentTool(
                name="npm",
                executable_path=found,
                available=True,
                available_via_path=True,
                version=version,
                confidence=1.0,
            )
        return None

    def _find_git(self) -> Optional[DevelopmentTool]:
        """Find git."""
        found = shutil.which("git")
        if found:
            result = _tool_version(found, "--version").strip()
            # git version output: "git version 2.41.0"
            version = result.replace("git version ", "") if "git version " in result else ""
            return DevelopmentTool(
                name="git",
                executable_path=found,
                available=True,
                available_via_path=True,
                version=version,
                confidence=1.0,
            )
        return None

    def _find_java(self) -> Optional[DevelopmentTool]:
        """Find Java runtime."""
        found = shutil.which("java") or shutil.which("java.exe")
        if found:
            result = _tool_version(found, "-version")
            # Parse Java version from stderr
            version_match = re.search(r"version\s+\"([^\"]+)\"", result)
            version = version_match.group(1) if version_match else ""
            return DevelopmentTool(
                name="java",
                executable_path=found,
                available=True,
                available_via_path=True,
                version=version,
                confidence=0.8,
            )
        return None

    def _find_go(self) -> Optional[DevelopmentTool]:
        """Find Go interpreter."""
        found = shutil.which("go") or shutil.which("go.exe")
        if found:
            result = _tool_version(found, "version")
            # go version output: "go version go1.21.0 windows/amd64"
            version_match = re.search(r"go version go([\d.]+)", result)
            version = version_match.group(1) if version_match else ""
            return DevelopmentTool(
                name="go",
                executable_path=found,
                available=True,
                available_via_path=True,
                version=version,
                confidence=0.8,
            )
        return None

    def _find_rust(self) -> Optional[DevelopmentTool]:
        """Find Rust compiler."""
        found = shutil.which("rustc") or shutil.which("rustc.exe")
        if found:
            result = _tool_version(found, "--version")
            # rustc version output: "rustc 1.77.2 (x86_64-pc-windows-msvc)"
            version_match = re.search(r"rustc ([\d.]+)", result)
            version = version_match.group(1) if version_match else ""
            return DevelopmentTool(
                name="rust",
                executable_path=found,
                available=True,
                available_via_path=True,
                version=version,
                confidence=0.8,
            )
        return None

    def _find_docker(self) -> Optional[DevelopmentTool]:
        """Find Docker."""
        found = shutil.which("docker")
        if found:
            return DevelopmentTool(
                name="docker",
                executable_path=found,
                available=True,
                available_via_path=True,
                confidence=0.8,
            )
        # Check for docker-compose
        found = shutil.which("docker-compose")
        if found:
            return DevelopmentTool(
                name="docker-compose",
                executable_path=found,
                available=True,
                available_via_path=True,
                confidence=0.7,
            )
        return None

    def _find_ollama(self) -> Optional[DevelopmentTool]:
        """Find Ollama and its models."""
        try:
            import httpx
            base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
            response = httpx.get(f"{base_url}/api/tags", timeout=5)
            if response.status_code == 200:
                models = response.json().get("models", [])
                model_names = [m.get("name", "") for m in models]
                if model_names:
                    return DevelopmentTool(
                        name="ollama",
                        executable_path=base_url,
                        available=True,
                        available_via_path=True,
                        version=", ".join(model_names[:3]),
                        capabilities=model_names,
                        confidence=1.0,
                    )
        except Exception:
            pass
        # Try without models (just the endpoint)
        try:
            import httpx
            base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
            response = httpx.get(f"{base_url}/api/tags", timeout=3)
            if response.status_code == 200:
                return DevelopmentTool(
                    name="ollama",
                    executable_path=base_url,
                    available=True,
                    available_via_path=True,
                    confidence=0.5,
                )
        except Exception:
            pass
        return None

    def discover_projects(self) -> List[ProjectInfo]:
        """Discover projects within the workspace."""
        projects: List[ProjectInfo] = []
        try:
            root = str(config.workspace_path)
            if not root or not os.path.isdir(root):
                return projects

            # Walk the workspace with bounded depth
            max_depth = 3
            skipped_dirs = {".git", "__pycache__", ".venv", "venv", "node_modules", "dist", "build"}

            for dirpath, dirnames, filenames in os.walk(root):
                # Limit depth
                rel_path = os.path.relpath(dirpath, root)
                depth = rel_path.count(os.sep) if rel_path != "." else 0
                if depth >= max_depth:
                    dirnames.clear()  # Don't descend further
                    continue

                # Prune skipped directories
                dirnames[:] = [
                    d for d in dirnames if d not in skipped_dirs and not d.startswith(".")
                ]
                dirnames.sort()

                # Check for project indicators
                for manifest_indicator, manifest_type in self._MANIFEST_INDICATORS:
                    if manifest_indicator in filenames:
                        manifest_path = os.path.join(dirpath, manifest_indicator)
                        project_root = dirpath

                        # Check if we already have a project at this root
                        if any(p.root == project_root for p in projects):
                            continue

                        project = ProjectInfo(
                            root=project_root,
                            name=self._derive_project_name(project_root, manifest_type),
                            languages=self._detect_languages(manifest_type),
                            manifests={manifest_type: manifest_path},
                        )

                        # Check git status
                        try:
                            import subprocess
                            result = subprocess.run(
                                ["git", "status", "--porcelain"],
                                cwd=project_root,
                                capture_output=True, text=True, timeout=5,
                            )
                            if result.returncode == 0:
                                project.git_status = {
                                    "status": result.stdout.strip() or "clean",
                                    "has_uncommitted": bool(result.stdout.strip()),
                                }
                        except Exception:
                            pass

                        # Check for startup scripts
                        startup_scripts = self._find_startup_scripts(dirpath, filenames)
                        if startup_scripts:
                            project.available_runtimes = startup_scripts

                        projects.append(project)
                        break  # One project per directory level

        except Exception as e:
            logger.warning("Project discovery failed: %s", e)

        return projects

    _MANIFEST_INDICATORS = [
        ("package.json", "javascript"),
        ("pyproject.toml", "python"),
        ("requirements.txt", "python"),
        ("composer.json", "php"),
        ("artisan", "laravel"),
        ("Cargo.toml", "rust"),
        ("go.mod", "go"),
        (".sln", "csharp"),
        ("pom.xml", "java"),
        ("build.gradle", "kotlin"),
    ]

    def _derive_project_name(self, root: str, manifest_type: str) -> str:
        """Derive a project name from its root path."""
        try:
            basename = os.path.basename(root)
            # Remove common suffixes
            for suffix in ["_project", "-project", "project"]:
                if basename.lower().endswith(suffix):
                    basename = basename[: -len(suffix)]
                    break
            return basename or root
        except Exception:
            return root

    def _detect_languages(self, manifest_type: str) -> List[str]:
        """Detect languages from a manifest type."""
        mapping = {
            "javascript": ["javascript", "node"],
            "python": ["python"],
            "php": ["php"],
            "laravel": ["php", "laravel"],
            "rust": ["rust"],
            "go": ["go"],
            "csharp": ["c#", ".net"],
            "java": ["java"],
            "kotlin": ["kotlin"],
        }
        return mapping.get(manifest_type, [])

    def _find_startup_scripts(
        self, dirpath: str, filenames: List[str]
    ) -> List[str]:
        """Find available startup scripts in a directory."""
        scripts: List[str] = []
        for fname in filenames:
            fname_lower = fname.lower()
            if fname_lower in {"package.json", "pyproject.toml", "requirements.txt",
                               "composer.json", "Cargo.toml", "go.mod", "build.gradle"}:
                scripts.append(fname_lower)
        return scripts

    def discover_local_ai(self) -> LocalAIModel:
        """Discover local AI runtimes (Ollama)."""
        try:
            import httpx
            base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
            response = httpx.get(f"{base_url}/api/tags", timeout=5)
            if response.status_code == 200:
                data = response.json()
                models = data.get("models", [])
                if models:
                    model_names = [m.get("name", "") for m in models]
                    # Get the first model's info
                    first_model = models[0]
                    size_bytes = 0
                    caps = first_model.get("details", {}).get("parameters", 0) or 0
                    # Rough size estimate from parameters
                    return LocalAIModel(
                        name=model_names[0] if model_names else "unknown",
                        endpoint=base_url,
                        version=", ".join(model_names[:3]) if len(model_names) >= 3 else ", ".join(model_names),
                        size_bytes=size_bytes,
                        capabilities=model_names,
                    )
        except Exception:
            pass
        # Return empty model if nothing found
        return LocalAIModel(name="", endpoint="")


# Similar providers for other platforms would be added here
# For now, the WindowsProvider is the primary implementation


# ---------------------------------------------------------------------------
# Discovery coordinator
# ---------------------------------------------------------------------------

class DeviceDiscoveryCoordinator:
    """Coordinates device discovery using the appropriate OS provider."""

    def __init__(self, provider: Optional[OperatingSystemProvider] = None):
        self._provider = provider or self._detect_provider()
        self._hardware_cache: Optional[HardwareInfo] = None
        self._apps_cache: Optional[List[ApplicationInfo]] = None
        self._processes_cache: Optional[List[ProcessInfo]] = None
        self._devtools_cache: Optional[Dict[str, DevelopmentTool]] = None
        self._projects_cache: Optional[List[ProjectInfo]] = None
        self._ai_cache: Optional[LocalAIModel] = None

    def _detect_provider(self) -> OperatingSystemProvider:
        """Detect and return the appropriate OS provider."""
        if os.name == "nt":
            return WindowsProvider()
        # Add other platforms as needed
        return OperatingSystemProvider()

    @property
    def provider(self) -> OperatingSystemProvider:
        return self._provider

    async def discover_all(
        self,
        scopes: Optional[List[str]] = None,
    ) -> DeviceInventory:
        """Run the full discovery pipeline.

        Every provider call is synchronous filesystem/registry work, so it runs
        in the default executor rather than on the event loop — a Start Menu
        walk plus 40 PATH entries is long enough to stall every other WebSocket
        frame. The gather is **awaited**; the previous version fired it and
        returned, so the inventory was always half-empty by the time a caller
        read it.
        """
        if scopes is None:
            scopes = self._default_scopes()

        inventory = DeviceInventory(
            timestamp=datetime.now(),
            hardware=await self._discover_hardware(),
            discovery_scopes=scopes,
        )

        await asyncio.gather(
            self._discover_applications(inventory),
            self._discover_running_processes(inventory),
            self._discover_development_environment(inventory),
            self._discover_projects(inventory),
            self._discover_local_ai(inventory),
        )

        return inventory

    def invalidate(self) -> None:
        """Forget every cache, so the next discovery is a real rescan."""
        self._hardware_cache = None
        self._apps_cache = None
        self._processes_cache = None
        self._devtools_cache = None
        self._projects_cache = None
        self._ai_cache = None

    def _default_scopes(self) -> List[str]:
        """Return the default discovery scopes."""
        return [
            "hardware",
            "applications",
            "running_processes",
            "development_environment",
            "projects",
            "local_ai",
        ]

    async def _discover_hardware(self) -> HardwareInfo:
        """Discover hardware information."""
        if self._hardware_cache is None:
            self._hardware_cache = await _in_thread(self._provider.discover_hardware)
        return self._hardware_cache

    async def _discover_applications(self, inventory: DeviceInventory) -> None:
        """Discover installed applications."""
        if self._apps_cache is None:
            self._apps_cache = await _in_thread(
                self._provider.discover_installed_applications
            )
        inventory.applications = self._apps_cache

    async def _discover_running_processes(self, inventory: DeviceInventory) -> None:
        """Discover running processes."""
        if self._processes_cache is None:
            self._processes_cache = await _in_thread(
                self._provider.discover_running_processes
            )
        inventory.running_processes = self._processes_cache

    async def _discover_development_environment(self, inventory: DeviceInventory) -> None:
        """Discover development environment."""
        if self._devtools_cache is None:
            self._devtools_cache = await _in_thread(
                self._provider.discover_development_environment
            )
        inventory.development_tools = self._devtools_cache

    async def _discover_projects(self, inventory: DeviceInventory) -> None:
        """Discover projects."""
        if self._projects_cache is None:
            self._projects_cache = await _in_thread(self._provider.discover_projects)
        inventory.projects = self._projects_cache

    async def _discover_local_ai(self, inventory: DeviceInventory) -> None:
        """Discover local AI capabilities."""
        if self._ai_cache is None:
            self._ai_cache = await _in_thread(self._provider.discover_local_ai)
        inventory.local_ai = self._ai_cache


# ---------------------------------------------------------------------------
# Permission manager
# ---------------------------------------------------------------------------

class PermissionScope:
    """Enum-like class for discovery permission scopes."""

    HARDWARE = "hardware"
    APPLICATIONS = "applications"
    RUNNING_PROCESSES = "running_processes"
    DEVELOPMENT_ENV = "development_environment"
    PROJECTS = "projects"
    LOCAL_AI = "local_ai"
    ALL = "all"

    @classmethod
    def all_scopes(cls) -> List[str]:
        return [
            cls.HARDWARE,
            cls.APPLICATIONS,
            cls.RUNNING_PROCESSES,
            cls.DEVELOPMENT_ENV,
            cls.PROJECTS,
            cls.LOCAL_AI,
        ]


@dataclass
class PermissionRecord:
    """Record of user consent for a discovery scope."""
    scope: str
    granted: bool
    granted_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    note: str = ""  # User's explanation if they denied


class PermissionManager:
    """Manages user consent for discovery scopes."""

    def __init__(self, storage_path: Optional[str] = None):
        self._storage_path = storage_path or os.path.join(
            _default_data_dir(), "discovery_permissions.json"
        )
        self._permissions: Dict[str, PermissionRecord] = {}
        self._load()

    def _load(self) -> None:
        """Load permissions from storage."""
        try:
            import json
            if os.path.exists(self._storage_path):
                with open(self._storage_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for scope, record in data.items():
                    self._permissions[scope] = PermissionRecord(
                        scope=scope,
                        granted=record.get("granted", False),
                        granted_at=(
                            datetime.fromisoformat(record["granted_at"])
                            if record.get("granted_at")
                            else None
                        ),
                        revoked_at=(
                            datetime.fromisoformat(record["revoked_at"])
                            if record.get("revoked_at")
                            else None
                        ),
                        note=record.get("note", ""),
                    )
        except Exception:
            pass  # Start with empty permissions if load fails

    def _save(self) -> None:
        """Save permissions to storage."""
        try:
            import json
            os.makedirs(os.path.dirname(self._storage_path), exist_ok=True)
            data: Dict[str, dict] = {}
            for scope, record in self._permissions.items():
                data[scope] = {
                    "granted": record.granted,
                    "granted_at": record.granted_at.isoformat() if record.granted_at else None,
                    "revoked_at": record.revoked_at.isoformat() if record.revoked_at else None,
                    "note": record.note,
                }
            with open(self._storage_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass  # Best-effort save

    def is_granted(self, scope: str) -> bool:
        """Check if a discovery scope is granted."""
        record = self._permissions.get(scope)
        if record is None:
            return False
        return record.granted

    def grant(self, scope: str) -> None:
        """Grant a discovery scope."""
        now = datetime.now()
        if scope not in self._permissions:
            self._permissions[scope] = PermissionRecord(scope=scope, granted=False)
        self._permissions[scope].granted = True
        self._permissions[scope].granted_at = now
        self._save()

    def revoke(self, scope: str) -> None:
        """Revoke a discovery scope."""
        if scope in self._permissions:
            self._permissions[scope].granted = False
            self._permissions[scope].revoked_at = datetime.now()
            self._save()

    def set_note(self, scope: str, note: str) -> None:
        """Set a user note for a scope."""
        if scope in self._permissions:
            self._permissions[scope].note = note
            self._save()

    def list_scopes(self) -> Dict[str, bool]:
        """List all scopes and their grant status."""
        return {scope: self.is_granted(scope) for scope in PermissionScope.all_scopes()}


# ---------------------------------------------------------------------------
# Inventory repository
# ---------------------------------------------------------------------------

class DeviceInventoryRepository:
    """Persists and retrieves device inventories."""

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or os.path.join(
            _default_data_dir(), "device_inventory.db"
        )
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the SQLite database."""
        import sqlite3

        # The data directory may not exist yet on a fresh portable install:
        # `data_dir()` never creates anything (core/paths.py), so the caller
        # that actually writes a database is the one that creates its parent.
        parent = os.path.dirname(self._db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS inventories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                timestamp DATETIME NOT NULL,
                hardware_json TEXT NOT NULL,
                applications_json TEXT,
                projects_json TEXT,
                local_ai_json TEXT,
                discovery_scopes TEXT,
                inventory_version TEXT DEFAULT '1.0',
                errors_json TEXT DEFAULT '[]'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS device_identity (
                device_id TEXT PRIMARY KEY,
                identifier TEXT NOT NULL,
                created_at DATETIME NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    async def save(self, inventory: DeviceInventory, device_id: str) -> None:
        """Save an inventory record.

        The previous version imported ``core.dna`` — a module that does not
        exist — before opening the connection, so *every* save raised
        ``ModuleNotFoundError``. The device identifier is the caller's job and
        is generated by :func:`generate_device_identifier`.
        """
        import aiosqlite

        parent = os.path.dirname(self._db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        async with aiosqlite.connect(self._db_path) as conn:
            await conn.execute(
                """INSERT INTO inventories
                   (device_id, timestamp, hardware_json, applications_json,
                    projects_json, local_ai_json, discovery_scopes,
                    inventory_version, errors_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    device_id,
                    inventory.timestamp.isoformat(),
                    json.dumps(self._serialize_hardware(inventory.hardware)),
                    json.dumps([self._serialize_app(a) for a in inventory.applications]),
                    json.dumps([self._serialize_project(p) for p in inventory.projects]),
                    json.dumps(self._serialize_ai(inventory.local_ai)),
                    ",".join(inventory.discovery_scopes),
                    inventory.inventory_version,
                    json.dumps(inventory.errors),
                ),
            )
            await conn.commit()

    async def latest(self, device_id: str) -> Optional[DeviceInventory]:
        """The most recent inventory for a device, or ``None``."""
        import aiosqlite

        async with aiosqlite.connect(self._db_path) as conn:
            async with conn.execute(
                "SELECT * FROM inventories WHERE device_id = ? "
                "ORDER BY timestamp DESC, id DESC LIMIT 1",
                (device_id,),
            ) as cursor:
                row = await cursor.fetchone()
        return self._row_to_inventory(row) if row else None

    def _serialize_hardware(self, hw: HardwareInfo) -> dict:
        """Serialize HardwareInfo to dict."""
        return {
            "cpu_model": hw.cpu_model,
            "logical_cores": hw.logical_cores,
            "total_ram_bytes": hw.total_ram_bytes,
            "available_ram_bytes": hw.available_ram_bytes,
            "gpu_info": hw.gpu_info,
            "os_family": hw.os_family,
            "os_version": hw.os_version,
            "os_architecture": hw.os_architecture,
        }

    def _serialize_app(self, app: ApplicationInfo) -> dict:
        """Serialize ApplicationInfo to dict."""
        return {
            "name": app.name,
            "display_name": app.display_name,
            "launch_location": app.launch_location,
            "version": app.version,
            "confidence": app.confidence,
            "discovery_source": app.discovery_source,
            "verified": app.verified,
            "target": app.target,
            "shortcut_path": app.shortcut_path,
            "install_location": app.install_location,
            "last_seen": app.last_seen,
        }

    def _serialize_project(self, proj: ProjectInfo) -> dict:
        """Serialize ProjectInfo to dict."""
        return {
            "root": proj.root,
            "name": proj.name,
            "languages": proj.languages,
            "manifests": proj.manifests,
            "git_status": proj.git_status,
            "available_runtimes": proj.available_runtimes,
        }

    def _serialize_ai(self, ai: LocalAIModel) -> dict:
        """Serialize LocalAIModel to dict."""
        return {
            "name": ai.name,
            "endpoint": ai.endpoint,
            "version": ai.version,
            "size_bytes": ai.size_bytes,
            "capabilities": ai.capabilities,
        }

    async def load(self, device_id: str) -> Optional[DeviceInventory]:
        """Load the most recent inventory for a device.

        Column indices are named here once. The previous version read
        ``row[1]`` (``device_id``) as the hardware JSON and ``row[0]`` (the
        auto-increment ``id``) as the timestamp, so a stored inventory could
        never be read back even after the save path was repaired.
        """
        import aiosqlite

        async with aiosqlite.connect(self._db_path) as conn:
            async with conn.execute(
                "SELECT * FROM inventories WHERE device_id = ? "
                "ORDER BY timestamp DESC, id DESC LIMIT 1",
                (device_id,),
            ) as cursor:
                row = await cursor.fetchone()
        return self._row_to_inventory(row) if row else None

    def _row_to_inventory(self, row: Sequence[Any]) -> DeviceInventory:
        # id, device_id, timestamp, hardware, applications, projects,
        # local_ai, discovery_scopes, inventory_version, errors_json
        timestamp = row[2]
        return DeviceInventory(
            timestamp=(
                datetime.fromisoformat(timestamp)
                if isinstance(timestamp, str)
                else (timestamp or datetime.now())
            ),
            hardware=self._deserialize_hardware(row[3]),
            applications=[
                self._deserialize_app(a) for a in json.loads(row[4] or "[]")
            ],
            projects=[self._deserialize_project(p) for p in json.loads(row[5] or "[]")],
            local_ai=self._deserialize_ai(row[6]),
            discovery_scopes=row[7].split(",") if row[7] else [],
            inventory_version=row[8] or "1.0",
            errors=json.loads(row[9] or "[]"),
        )

    def _deserialize_hardware(self, data: str) -> HardwareInfo:
        """Deserialize hardware JSON (or an already-decoded dict)."""
        d = _as_mapping(data)
        return HardwareInfo(
            cpu_model=d.get("cpu_model", ""),
            logical_cores=d.get("logical_cores", 0),
            total_ram_bytes=d.get("total_ram_bytes", 0),
            available_ram_bytes=d.get("available_ram_bytes", 0),
            gpu_info=d.get("gpu_info", ""),
            os_family=d.get("os_family", ""),
            os_version=d.get("os_version", ""),
            os_architecture=d.get("os_architecture", ""),
        )

    def _deserialize_app(self, data: dict) -> ApplicationInfo:
        """Deserialize dict to ApplicationInfo."""
        data = _as_mapping(data)
        return ApplicationInfo(
            name=data.get("name", ""),
            display_name=data.get("display_name", ""),
            launch_location=data.get("launch_location", ""),
            version=data.get("version", ""),
            confidence=data.get("confidence", 1.0),
            discovery_source=data.get("discovery_source", ""),
            verified=data.get("verified", False),
            target=data.get("target", ""),
            shortcut_path=data.get("shortcut_path", ""),
            install_location=data.get("install_location", ""),
            last_seen=data.get("last_seen", ""),
        )

    def _deserialize_project(self, data: dict) -> ProjectInfo:
        """Deserialize dict to ProjectInfo."""
        data = _as_mapping(data)
        return ProjectInfo(
            root=data.get("root", ""),
            name=data.get("name", ""),
            languages=data.get("languages", []),
            manifests=data.get("manifests", {}),
            git_status=data.get("git_status"),
            available_runtimes=data.get("available_runtimes", []),
        )

    def _deserialize_ai(self, data: dict) -> LocalAIModel:
        """Deserialize dict to LocalAIModel."""
        data = _as_mapping(data)
        return LocalAIModel(
            name=data.get("name", ""),
            endpoint=data.get("endpoint", ""),
            version=data.get("version", ""),
            size_bytes=data.get("size_bytes", 0),
            capabilities=data.get("capabilities", []),
        )


# ---------------------------------------------------------------------------
# Capability registry
# ---------------------------------------------------------------------------

class CapabilitySpec:
    """Describes a capability that Zaytoon can perform."""

    def __init__(
        self,
        identifier: str,
        name: str,
        description: str,
        required_tools: List[str],
        permission_tier: str,
        prerequisites: Optional[List[str]] = None,
        risk_level: str = "safe",
        supported_operations: Optional[List[str]] = None,
        failure_reason: Optional[str] = None,
    ):
        self.identifier = identifier
        self.name = name
        self.description = description
        self.required_tools = required_tools
        self.permission_tier = permission_tier
        self.prerequisites = prerequisites or []
        self.risk_level = risk_level
        self.supported_operations = supported_operations or []
        self.failure_reason = failure_reason


class CapabilityRegistry:
    """Maps device inventory to available capabilities."""

    def __init__(self, inventory: DeviceInventory, permission_manager: PermissionManager):
        self.inventory = inventory
        self.pm = permission_manager
        self._capabilities: Dict[str, CapabilitySpec] = {}
        self._build_registry()

    def _build_registry(self) -> None:
        """Build the capability registry from the current inventory."""
        self._capabilities = {}

        # 1. Application-based capabilities
        for app in self.inventory.applications:
            self._add_application_capabilities(app)

        # 2. Development environment capabilities
        self._add_development_capabilities()

        # 3. Local AI capabilities
        self._add_local_ai_capabilities()

        # 4. Project-based capabilities
        for project in self.inventory.projects:
            self._add_project_capabilities(project)

    def _add_application_capabilities(self, app: ApplicationInfo) -> None:
        """Add capabilities based on discovered applications."""
        name_lower = app.name.lower()

        # Open application
        if app.name.lower() in ["vscode", "visual studio code", "code"]:
            self._add_capability(
                identifier=f"open_{app.name}",
                name=f"Open {app.display_name}",
                description=f"Open {app.display_name} editor",
                required_tools=["open_app"],
                permission_tier=app.confidence > 0.5 and "confirm" or "safe",
                risk_level="safe",
                supported_operations=["edit_project", "open_file"],
            )

        if app.name.lower() in ["chrome", "google chrome", "firefox", "edge", "microsoft edge"]:
            self._add_capability(
                identifier=f"open_{app.name}",
                name=f"Open {app.display_name}",
                description=f"Open {app.display_name} browser",
                required_tools=["open_app"],
                permission_tier="safe",
                risk_level="safe",
                supported_operations=["browse_web", "search_web"],
            )

        if app.name.lower() in ["notepad", "notepad++"]:
            self._add_capability(
                identifier=f"open_{app.name}",
                name=f"Open {app.display_name}",
                description=f"Open {app.display_name}",
                required_tools=["open_app"],
                permission_tier="safe",
                risk_level="safe",
                supported_operations=["read_file", "edit_text"],
            )

        # Add more application capabilities as needed...

        # Calculator
        if app.name.lower() in ["calculator", "calc"]:
            self._add_capability(
                identifier="calculate",
                name="Calculate",
                description="Perform calculations",
                required_tools=["calculator"],
                permission_tier="safe",
                risk_level="safe",
                supported_operations=["arithmetic", "complex_math"],
            )

        # System info
        if app.name.lower() in ["system_info", "system"]:
            self._add_capability(
                identifier="get_system_info",
                name="System Information",
                description="Get system hardware and software information",
                required_tools=["system_info"],
                permission_tier="safe",
                risk_level="safe",
                supported_operations=["cpu_info", "ram_info", "storage_info"],
            )

    def _add_development_capabilities(self) -> None:
        """Add capabilities based on discovered development tools."""
        tools = self.inventory.development_tools

        # Python
        if "python" in tools and tools["python"].available:
            self._add_capability(
                identifier="run_python",
                name="Run Python",
                description="Execute Python scripts",
                required_tools=["python"],
                permission_tier="confirm" if tools["python"].confidence > 0.8 else "safe",
                risk_level="safe",
                supported_operations=["script_execution", "package_manager"],
            )

        # Node.js
        if "nodejs" in tools and tools["nodejs"].available:
            self._add_capability(
                identifier="run_node",
                name="Run Node.js",
                description="Execute Node.js scripts",
                required_tools=["nodejs"],
                permission_tier="confirm",
                risk_level="safe",
                supported_operations=["npm_scripts", "frontend_build"],
            )

        # Git
        if "git" in tools and tools["git"].available:
            self._add_capability(
                identifier="run_git",
                name="Run Git",
                description="Execute git commands",
                required_tools=["git"],
                permission_tier="confirm",
                risk_level="safe",
                supported_operations=["status", "commit", "push", "pull", "checkout"],
            )

        # Docker
        if "docker" in tools and tools["docker"].available:
            self._add_capability(
                identifier="run_docker",
                name="Run Docker",
                description="Execute Docker commands",
                required_tools=["docker"],
                permission_tier="confirm",
                risk_level="safe",
                supported_operations=["build", "run", "push", "pull"],
            )

    def _add_local_ai_capabilities(self) -> None:
        """Add capabilities based on local AI models."""
        ai = self.inventory.local_ai
        if ai.name and ai.endpoint:
            self._add_capability(
                identifier="query_local_model",
                name="Query Local AI Model",
                description=f"Query local AI model: {ai.name}",
                required_tools=["ollama"] if "ollama" in str(ai.endpoint).lower() else [],
                permission_tier="confirm",
                risk_level="safe",
                supported_operations=["text_generation", "code_generation", "analysis"],
                failure_reason=f"Model {ai.name} not available at {ai.endpoint}" if not ai.capabilities else None,
            )

    def _add_project_capabilities(self, project: ProjectInfo) -> None:
        """Add capabilities based on discovered projects."""
        if not project.root or not os.path.isdir(project.root):
            return

        # Python project
        if "pyproject.toml" in project.manifests or "requirements.txt" in project.manifests:
            self._add_capability(
                identifier="run_python_project",
                name="Run Python Project",
                description=f"Run project at {project.name}",
                required_tools=["python"],
                permission_tier="confirm",
                risk_level="safe",
                supported_operations=["run_server", "run_tests", "install_deps"],
                prerequisites=["python_available"],
            )

        # Node.js project
        if "package.json" in project.manifests:
            self._add_capability(
                identifier="run_node_project",
                name="Run Node.js Project",
                description=f"Run project at {project.name}",
                required_tools=["nodejs", "npm"],
                permission_tier="confirm",
                risk_level="safe",
                supported_operations=["npm_start", "npm_test", "npm_build"],
                prerequisites=["nodejs_available", "npm_available"],
            )

        # Laravel project
        if "artisan" in project.manifests or project.languages == ["php", "laravel"]:
            self._add_capability(
                identifier="run_laravel_project",
                name="Run Laravel Project",
                description=f"Run Laravel project at {project.name}",
                required_tools=["php", "composer"],
                permission_tier="confirm",
                risk_level="safe",
                supported_operations=["php_artisan", "php_server"],
                prerequisites=["php_available", "composer_available"],
            )

    def _add_capability(
        self,
        identifier: str,
        name: str,
        description: str,
        required_tools: List[str],
        permission_tier: str,
        prerequisites: Optional[List[str]] = None,
        risk_level: str = "safe",
        supported_operations: Optional[List[str]] = None,
        failure_reason: Optional[str] = None,
    ) -> None:
        """Add a capability to the registry."""
        # Check if we already have this capability
        if identifier in self._capabilities:
            # Upgrade if the new one has better info
            existing = self._capabilities[identifier]
            if risk_level == "safe" and existing.risk_level != "safe":
                # Only downgrade safety, never upgrade risk
                pass
            elif (
                existing.risk_level == "safe"
                and risk_level != "safe"
                and existing.prerequisites == prerequisites
            ):
                # Keep the safe one
                return
            # Merge supported operations
            if supported_operations:
                existing.supported_operations.extend(
                    [op for op in supported_operations if op not in existing.supported_operations]
                )
            return

        self._capabilities[identifier] = CapabilitySpec(
            identifier=identifier,
            name=name,
            description=description,
            required_tools=required_tools,
            permission_tier=permission_tier,
            prerequisites=prerequisites,
            risk_level=risk_level,
            supported_operations=supported_operations or [],
            failure_reason=failure_reason,
        )

    def get_capability(self, identifier: str) -> Optional[CapabilitySpec]:
        """Get a capability by identifier."""
        return self._capabilities.get(identifier)

    def get_available_capabilities(self) -> List[CapabilitySpec]:
        """Get all capabilities that are available on this device."""
        available: List[CapabilitySpec] = []
        for cap in self._capabilities.values():
            # Check prerequisites
            prereqs_met = self._check_prerequisites(cap.prerequisites)
            # Check permission
            if self.pm.is_granted(cap.permission_tier) or cap.permission_tier == "safe":
                if prereqs_met:
                    available.append(cap)
        return available

    def _check_prerequisites(self, prerequisites: Optional[List[str]]) -> bool:
        """Check if all prerequisites are met."""
        if not prerequisites:
            return True

        for prereq in prerequisites:
            met = False
            if prereq == "python_available":
                met = "python" in self.inventory.development_tools and self.inventory.development_tools["python"].available
            elif prereq == "nodejs_available":
                met = "nodejs" in self.inventory.development_tools and self.inventory.development_tools["nodejs"].available
            elif prereq == "php_available":
                met = "java" in self.inventory.development_tools  # Java check is closest we have
            elif prereq == "composer_available":
                met = "npm" in self.inventory.development_tools  # Using npm as proxy
            elif prereq == "ollama_available":
                met = bool(self.inventory.local_ai.name)

            if not met:
                return False
        return True


# ---------------------------------------------------------------------------
# First-run onboarding
# ---------------------------------------------------------------------------

class FirstRunOnboarding:
    """Handles the first-run device onboarding flow."""

    def __init__(self, permission_manager: PermissionManager, repository: DeviceInventoryRepository):
        self.pm = permission_manager
        self.repo = repository
        self.device_id = self._generate_device_id()

    def _generate_device_id(self) -> str:
        """Generate a stable device identifier."""
        # One implementation, so onboarding and the repository cannot disagree
        # about which device an inventory belongs to.
        return generate_device_identifier()

    async def run_onboarding(self) -> DeviceInventory:
        """Run the first-run onboarding flow."""
        # Step 1: Explain discovery scope and get consent
        scopes = PermissionScope.all_scopes()
        granted = await self._show_discovery_scope_explanation(scopes)

        if not granted:
            # Return limited inventory
            return DeviceInventory(
                timestamp=datetime.now(),
                hardware=HardwareInfo(),
                discovery_scopes=[],
                errors=["User denied discovery consent"],
            )

        # Step 2: Run discovery with granted scopes
        coordinator = DeviceDiscoveryCoordinator()
        inventory = await coordinator.discover_all(scopes)

        # Step 3: Build capability registry
        capability_registry = CapabilityRegistry(inventory, self.pm)

        # Step 4: Save inventory
        await self.repo.save(inventory, self.device_id)

        # Step 5: Initialize permissions
        self._initialize_permissions(inventory)

        return inventory

    async def _show_discovery_scope_explanation(self, scopes: List[str]) -> bool:
        """Show the discovery scope explanation and get user consent.

        In a real implementation, this would show a UI dialog.
        For now, we simulate it.
        """
        # This would be integrated with the settings/confirmation UI
        # For now, assume consent is given
        # TODO: Integrate with actual UI
        return True

    def _initialize_permissions(self, inventory: DeviceInventory) -> None:
        """Initialize permission grants based on discovery results."""
        # Automatically grant safe-tier capabilities
        for app in inventory.applications:
            if app.confidence > 0.8:
                self.pm.grant("applications")

        if inventory.local_ai.name:
            self.pm.grant("local_ai")

        # Development environment is always available for authorized users
        if inventory.development_tools:
            self.pm.grant("development_environment")


# Example usage
async def run_first_run_onboarding() -> DeviceInventory:
    """Entry point for first-run onboarding."""
    from common.reporting import client_error
    import json

    pm = PermissionManager()
    repo = DeviceInventoryRepository()

    onboarding = FirstRunOnboarding(pm, repo)
    inventory = await onboarding.run_onboarding()

    logger.info(
        "First-run onboarding complete: %d applications, %d projects, "
        "%d capabilities",
        len(inventory.applications),
        len(inventory.projects),
        len(inventory.local_ai.name) > 0,
    )

    return inventory


def generate_device_identifier() -> str:
    """Generate a stable device identifier.

    Uses invariant machine information *and* the user name: two people sharing
    a PC must not share an inventory. ``getpass.getuser()`` is used instead of
    ``os.getlogin()`` because the latter raises ``OSError`` in a service or a
    frozen windowed process, where there is no controlling terminal — and an
    identifier helper that raises takes the whole inventory down with it.
    """
    import hashlib

    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover - defensive
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"
    parts = [
        os.environ.get("COMPUTERNAME", platform.node()),
        user,
        platform.system(),
        platform.release(),
        platform.machine(),
    ]
    hardware_hash = hashlib.sha256("|".join(parts).encode("utf-8", "replace")).hexdigest()[:12]
    return f"zaytoon-{hardware_hash}"


# ---------------------------------------------------------------------------
# Application-discovery helpers
# ---------------------------------------------------------------------------

#: Directories never descended into while looking for portable applications.
_SKIP_DIR_NAMES = frozenset(
    {
        "node_modules", "__pycache__", ".git", ".svn", ".venv", "venv",
        "dist", "build", "obj", ".idea", ".vscode", "cache",
    }
)

#: Start Menu shortcuts that are documentation or maintenance, not applications.
_NOISE_SHORTCUT_WORDS = (
    "uninstall", "readme", "release notes", "changelog", "license",
    "documentation", "help", "website", "manual", "support",
)

#: Registration entries that are runtimes/updates rather than applications.
_NOISE_REGISTRATION_WORDS = (
    "update for", "hotfix", "security update", "redistributable",
    "runtime", "sdk", "driver", "language pack",
)


async def _in_thread(fn):
    """Run a synchronous discovery function off the event loop."""
    return await asyncio.get_running_loop().run_in_executor(None, fn)


def _tool_version(executable: str, *args: str) -> str:
    """Run a version probe with an argument vector, never a shell string.

    The probes used to be ``os.popen(f"{found} --version")``, which is two bugs
    in one line: a path containing a space (``C:\\Program Files\\...``) is split
    by the shell into ``C:\\Program`` and fails, and the executable path is
    interpolated into a command string. ``subprocess.run`` with a list has
    neither problem. Output from stderr is included because Java prints its
    version there.
    """
    try:
        proc = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return f"{proc.stdout or ''}{proc.stderr or ''}"


def _is_windows_dir(path: str) -> bool:
    """Whether ``path`` is the Windows directory or inside it."""
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    try:
        candidate = os.path.normcase(os.path.realpath(path))
        base = os.path.normcase(os.path.realpath(root))
        return os.path.commonpath([candidate, base]) == base
    except (OSError, ValueError):
        return False


def _as_mapping(data: Any) -> Dict[str, Any]:
    """A JSON column as a dict, whether it arrived encoded or decoded.

    The repository stores JSON text but its row helpers used to be inconsistent
    about who decoded it: ``applications`` was parsed at the call site while
    ``hardware`` and ``local_ai`` were handed the raw string, so the
    deserialisers called ``.get`` on a ``str``. Accepting both shapes makes the
    helpers correct regardless of which side decoded.
    """
    if isinstance(data, dict):
        return data
    if not data:
        return {}
    try:
        decoded = json.loads(data)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _clean_display_name(stem: str) -> str:
    """A human name from a shortcut/file stem, or ``""`` when unusable."""
    text = re.sub(r"\s+", " ", str(stem or "")).strip(" .-_")
    if not text or len(text) > 120:
        return ""
    return text


def _app_key(name: str) -> str:
    """The normalised key two records for the same application share.

    Case, spacing and punctuation are not meaningful in an application name
    ("VS Code" and "vs-code" are one application), so they are folded here
    rather than at every comparison site. CamelCase is deliberately *not* split:
    doing so would also turn "YouTube" into "you tube", and a key that invents
    word boundaries merges names that are not the same application.
    """
    text = str(name or "").casefold()
    text = re.sub(r"[^\w\u0600-\u06FF]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _is_noise_shortcut(display: str) -> bool:
    low = display.casefold()
    return any(word in low for word in _NOISE_SHORTCUT_WORDS)


def _is_noise_registration(display: str) -> bool:
    low = display.casefold()
    if re.match(r"^kb\d{6,}", low):
        return True
    return any(word in low for word in _NOISE_REGISTRATION_WORDS)


def _clean_registry_path(raw: Any) -> str:
    """A registry-stored path without quotes, ``,0`` icon suffixes or env vars."""
    if not isinstance(raw, str):
        return ""
    text = raw.strip().strip('"').strip()
    # `DisplayIcon` is `path,index`; an index is not part of the path.
    text = re.sub(r",\s*-?\d+$", "", text).strip().strip('"')
    text = os.path.expandvars(text)
    return text


def _read_registry_values(key) -> Dict[str, str]:
    """Every string value of a registry key, best-effort."""
    import winreg

    values: Dict[str, str] = {}
    try:
        count = winreg.QueryInfoKey(key)[1]
    except OSError:
        return values
    for index in range(count):
        try:
            name, value, _ = winreg.EnumValue(key, index)
        except OSError:
            continue
        if isinstance(value, str):
            values[name] = value
    return values


def _app_path_registry_locations():
    """``(hive, view, path)`` for the App Paths key, 32- and 64-bit."""
    import winreg

    return (
        (winreg.HKEY_CURRENT_USER, 0, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
    )


def _uninstall_registry_locations():
    """``(hive, view, path)`` for installed-application registrations."""
    import winreg

    return (
        (winreg.HKEY_CURRENT_USER, 0,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
