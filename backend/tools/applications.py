"""Application launching, closing and window control.

The Windows resolver moved here from ``tools/executor.py`` unchanged in
behaviour: it only ever returns an allow-listed alias, a PATH-resolved
executable, a Start Menu shortcut, or a caller-supplied path that exists and is
not a script type. Names carrying shell syntax are rejected rather than
returned verbatim, because the result may be handed to ``cmd /c start``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import re
import shutil
from typing import Any, Dict, List, Optional, Sequence, Tuple

from tools.base import BaseTool, ToolResult, ToolValidationError, _prop
from tools.sandbox import (
    APP_LAUNCH_ALLOWLIST,
    SecurityError,
    has_shell_metachars,
    is_allowed_app,
    is_blocked_executable,
    sanitize_app_name,
    validate_app_target,
)

logger = logging.getLogger("Zaytoon.Tools.Applications")

#: Friendly name → executable/alias.
APP_PATHS: Dict[str, List[str]] = {
    "spotify": ["spotify"],
    "chrome": ["chrome"],
    "google chrome": ["chrome"],
    "firefox": ["firefox"],
    "edge": ["msedge"],
    "microsoft edge": ["msedge"],
    "vscode": ["code"],
    "vs code": ["code"],
    "visual studio code": ["code"],
    "cursor": ["cursor"],
    "terminal": ["cmd"],
    "command prompt": ["cmd"],
    "cmd": ["cmd"],
    "powershell": ["powershell"],
    "notepad": ["notepad"],
    "calculator": ["calc"],
    "discord": ["discord"],
    "slack": ["slack"],
    "zoom": ["zoom"],
    "teams": ["teams"],
    "microsoft teams": ["teams"],
    "outlook": ["outlook"],
    "file explorer": ["explorer"],
    "explorer": ["explorer"],
    "control panel": ["control"],
    "settings": ["ms-settings:"],
    "task manager": ["taskmgr"],
    "regedit": ["regedit"],
    "notepad++": ["notepad++"],
    "paint": ["mspaint"],
    "word": ["winword"],
    "excel": ["excel"],
    "powerpoint": ["powerpnt"],
    "fxsound": ["fxsound"],
    "fx audio": ["fxsound"],
    "fx sound": ["fxsound"],
}

#: Arabic (Egyptian) names people actually say, mapped to the English key that
#: ``APP_PATHS`` and the Start Menu use. A closed list, not transliteration:
#: guessing that "كروم" means "chrome" from letter shapes is how an application
#: nobody asked for gets opened, so every entry here is deliberate.
APP_ALIASES_AR: Dict[str, str] = {
    "كروم": "chrome",
    "جوجل كروم": "google chrome",
    "فايرفوكس": "firefox",
    "فاير فوكس": "firefox",
    "ايدج": "edge",
    "ادج": "edge",
    "مايكروسوفت ايدج": "microsoft edge",
    "في اس كود": "vscode",
    "فيجوال ستوديو كود": "visual studio code",
    "كود": "vscode",
    "كورسور": "cursor",
    "تيرمنال": "terminal",
    "الطرفيه": "terminal",
    "موجه الاوامر": "command prompt",
    "باور شيل": "powershell",
    "نوتباد": "notepad",
    "الاله الحاسبه": "calculator",
    "الحاسبه": "calculator",
    "ديسكورد": "discord",
    "سلاك": "slack",
    "زووم": "zoom",
    "تيمز": "teams",
    "مايكروسوفت تيمز": "microsoft teams",
    "اوتلوك": "outlook",
    "مستكشف الملفات": "file explorer",
    "لوحه التحكم": "control panel",
    "الاعدادات": "settings",
    "مدير المهام": "task manager",
    "الرسام": "paint",
    "وورد": "word",
    "اكسل": "excel",
    "بوربوينت": "powerpoint",
    "سبوتيفاي": "spotify",
    "سبوتي فاي": "spotify",
}

#: Targets that need the shell because they are URI schemes or builtins.
WINDOWS_APP_LAUNCH: Dict[str, List[str]] = {
    "spotify": ["spotify:", r"shell:AppsFolder\SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"],
    "calculator": ["calc"],
    "settings": ["ms-settings:"],
}

_DETECT_EXT = re.compile(r"\.\w+$")

_IS_WINDOWS = os.name == "nt"
_IS_MACOS = sys.platform == "darwin"

#: Names that may be closed: the launch allow-list plus every table entry the
#: resolver knows (so an app we can *find* is an app we can close, but nothing
#: outside our own tables is reachable). See `CloseApp.execute`.
_KNOWN_APP_NAMES = APP_LAUNCH_ALLOWLIST | frozenset(APP_PATHS)

#: Where macOS keeps application bundles. A `.app` is the artifact, and it is
#: never on `PATH` — which is why a POSIX resolver written for Linux finds
#: nothing on a Mac: `shutil.which("spotify")` is empty there by design, and the
#: app is sitting in `/Applications/Spotify.app`.
_MAC_APP_DIRS: List[str] = ["/Applications", os.path.expanduser("~/Applications")]


def _mac_bundle(app: str, roots: Optional[Sequence[str]] = None) -> Optional[str]:
    """The `.app` bundle for a name, matched case- and space-insensitively.

    `Spotify`, `spotify` and `Spotify.app` all land on the same bundle
    because the human word for an app and its folder name rarely agree on
    spacing. Depth one only: a recursive scan of /Applications would find helper
    bundles inside other bundles and launch something nobody named.
    """
    want = "".join(app.strip().lower().split())
    if not want:
        return None
    for base in roots or _MAC_APP_DIRS:
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue
        for entry in entries:
            if not entry.endswith(".app"):
                continue
            stem = "".join(entry[:-4].lower().split())
            if stem == want or want in stem:
                full = os.path.join(base, entry)
                if os.path.isdir(full):
                    return full
    return None


def start_menu_roots() -> List[str]:
    """The Start Menu roots that exist on this machine, in priority order."""
    search_dirs = [
        os.path.join(
            os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"
        ),
        os.path.join(
            os.environ.get("ProgramData", r"C:\ProgramData"),
            "Microsoft", "Windows", "Start Menu", "Programs",
        ),
    ]
    return [d for d in search_dirs if d and os.path.isdir(d)]


#: Kept because the module's own callers and older imports use the private
#: spelling. There is still exactly one implementation.
_start_menu_roots = start_menu_roots


def build_shortcut_index(roots: Optional[Sequence[str]] = None) -> List[Tuple[str, str]]:
    """One walk of the Start Menu: ``(shortcut stem, path)``, in walk order.

    The stem keeps its original casing because the inventory uses it as a
    display name — lower-casing here is what made every discovered application
    read as ``my tool`` in the UI. Matching is case-insensitive where it
    happens (:func:`_shortcut_score`), so the index has no reason to destroy the
    label.

    ``roots`` lets :mod:`core.discovery` scan exactly the roots it asked for
    while sharing this walk; leaving it out uses every Start Menu root, so the
    launcher and the inventory cannot disagree about what a shortcut is.

    The order is exactly the order the per-call walk produced (per root, then
    ``os.walk``'s order within it), so the first match here is the match that
    would have been returned before. Scanning a whole tree to find a match
    that may sit in the first directory costs a little more than an early exit
    on a single lookup; that is a bounded, one-off cost on a path that already
    launches a process.
    """
    index: List[Tuple[str, str]] = []
    for base in roots if roots is not None else start_menu_roots():
        for root, _, files in os.walk(base):
            for name in files:
                if not name.lower().endswith(".lnk"):
                    continue
                stem = os.path.splitext(name)[0]
                index.append((stem, os.path.join(root, name)))
    return index


def _name_variants(name: str) -> List[str]:
    """Every spelling of ``name`` that names the same application.

    ``APP_PATHS`` knows that ``vscode``, ``vs code`` and ``visual studio code``
    are one program because they share an alias. The Start Menu, however, is
    labelled with the *display* name — ``Visual Studio Code.lnk`` — so a lookup
    for ``vscode`` alone never matched it and fell through to the blocked
    ``code.CMD`` on PATH. Expanding through the shared alias makes the table's
    own knowledge available to the shortcut matcher without a second table.
    """
    key = name.strip().lower()
    variants = [key]
    aliases = APP_PATHS.get(key)
    if aliases:
        primary = aliases[0]
        for other, other_aliases in APP_PATHS.items():
            if other_aliases and other_aliases[0] == primary and other not in variants:
                variants.append(other)
    return variants


#: Shortcuts that are documentation, uninstallers or updaters, never the app.
_SHORTCUT_NOISE = (
    "uninstall", "readme", "release notes", "changelog", "license",
    "documentation", "help", "website", "manual", "support", "check for updates",
)


def _shortcut_score(stem: str, variants: Sequence[str]) -> int:
    """How well a shortcut stem matches: exact beats prefix beats substring.

    A plain ``app_lower in stem`` check is why ``chrome`` could resolve to
    ``Chrome Apps`` rather than ``Google Chrome``: both contain the word, and
    the first one walked won. Ranking makes the exact/prefix match win no matter
    what order the filesystem returned.
    """
    stem_norm = " ".join(stem.lower().split())
    if any(word in stem_norm for word in _SHORTCUT_NOISE):
        return 0
    best = 0
    for variant in variants:
        want = " ".join(variant.strip().lower().split())
        if not want:
            continue
        if stem_norm == want:
            score = 100
        elif stem_norm.startswith(want):
            score = 80
        elif want in stem_norm:
            score = 60
        elif stem_norm in want:
            score = 30
        else:
            continue
        best = max(best, score)
    return best


def _find_start_menu_shortcut(
    app: str, *, index: Optional[Sequence[Tuple[str, str]]] = None
) -> Optional[str]:
    """Best Start Menu shortcut whose stem names ``app``, or ``None``.

    ``index`` lets a caller that resolves many names share one walk; leaving it
    out builds the index here, so a single lookup still works and there is only
    one implementation of what "matches" means. The best-ranked match wins and
    ties keep walk order, which is user Start Menu before machine Start Menu.
    """
    if index is None:
        index = build_shortcut_index()
    variants = _name_variants(app)
    best_path: Optional[str] = None
    best_score = 0
    for stem, path in index:
        score = _shortcut_score(stem, variants)
        if score > best_score:
            best_score = score
            best_path = path
    return best_path


def _locate(
    name: str, *, shortcut_index: Optional[Sequence[Tuple[str, str]]] = None
) -> Optional[Tuple[str, str]]:
    """Positive evidence that ``name`` is present: ``(target, from_where)``.

    One implementation of "have I actually seen this application?", shared by
    every branch of the resolver so two of them cannot disagree about what
    counts as a hit. It looks for a real artifact — an executable on ``PATH``
    or a Start Menu shortcut — and never treats a *table* of how to launch
    something as proof that it exists.

    **A blockable script loses to a real shortcut.** ``vscode`` resolves on
    PATH to ``code.CMD``, which the sandbox rightly refuses to execute; the
    Start Menu has the actual application. Preferring the PATH hit because it
    was checked first meant the resolver confidently returned the one target it
    would then refuse to launch. The ordering here is: a launchable executable,
    then a platform bundle, then a shortcut, and only then a blocked script
    (still evidence that the app is installed).
    """
    alias = APP_PATHS.get(name, [name])[0]
    on_path = shutil.which(alias) or shutil.which(alias + ".exe") or shutil.which(name)
    lnk = _find_start_menu_shortcut(name, index=shortcut_index)

    if on_path and not is_blocked_executable(on_path):
        return on_path, "path"
    if _IS_MACOS:
        bundle = _mac_bundle(alias) or _mac_bundle(name)
        if bundle:
            # Positive evidence in the same sense as the two checks around it: a
            # directory that exists, not a row in a table.
            return bundle, "bundle"
    if lnk:
        return lnk, "shortcut"
    if on_path:
        return on_path, "path"
    return None


def _english_app_name(name: str) -> str:
    """Fold an Arabic application name onto its English key, or return ``name``.

    The table is consulted through the same :func:`core.targets.normalise_text`
    the rest of the resolver uses, so ``كروم`` and ``كـروم`` (tatweel) are one
    name and there is no second spelling table to drift. Imported locally: the
    two modules reference each other (this one for the website fallback) and a
    module-level import would make the cycle real.
    """
    if not name:
        return name
    try:
        from core.targets import normalise_text
    except Exception:  # pragma: no cover - defensive
        return name
    key = normalise_text(name)
    for alias, english in APP_ALIASES_AR.items():
        if normalise_text(alias) == key:
            return english
    return name


def resolve_app_detail(
    app: str, *, shortcut_index: Optional[Sequence[Tuple[str, str]]] = None
) -> Dict[str, Any]:
    """Resolve an application name to a launch target, and say whether the
    application is actually *present*.

    The distinction is the whole point. The old resolver returned a bare alias
    (``"winword"``) when it had looked everywhere and found nothing, and a real
    target when it had found one — both truthy, so a caller could not tell
    "installed" from "will be attempted and may well fail". The Computer view
    needs that answer before it puts an application forward as available, and a
    panel that guesses is worse than one that says nothing.

    Returns a dict with:

    - ``target`` — what a launcher should be handed, or ``None`` if the name
      was rejected outright. It may be a bare alias *without* the application
      being found; that is a deliberate best-effort, because ``OpenApp`` still
      tries it and its failure at exec time is more informative than a silent
      no-op.
    - ``found`` — **positive evidence** that the application is on this
      machine: an executable on ``PATH``, an existing path, or a Start Menu
      shortcut. ``False`` means "I found nothing", never "not installed": an
      install this machine does not advertise (a portable copy, a custom
      location, a portable app with no Start Menu entry) stays invisible, and
      the UI says so rather than calling it absent.
    - ``via`` — where ``target`` came from, so a caller can explain itself.

    ``resolve_windows_app`` is this function with the verdict stripped off;
    there is one decision tree, not two.
    """
    try:
        name = sanitize_app_name(app).lower()
    except SecurityError:
        logger.warning("Blocked app resolution for unsafe name: %r", app)
        return {"target": None, "found": False, "via": "rejected"}

    # An Arabic name the user actually said ("شغل كروم") is folded onto the
    # English key before any table is consulted. Without this the resolver
    # reported "nothing named كروم" on a machine that has Chrome installed —
    # the request was understood, the name simply was not in our vocabulary.
    name = _english_app_name(name)

    if name in WINDOWS_APP_LAUNCH:
        # The builtin table says *how* to launch, never *that* anything is
        # installed — `spotify:` is a URI scheme that resolves only when
        # Spotify is present. So the target stays the builtin form (it is what
        # `OpenApp` wants) while `found` is earned from real evidence, checked
        # exactly as it would be for any other name.
        return {
            "target": WINDOWS_APP_LAUNCH[name][0],
            "found": _locate(name, shortcut_index=shortcut_index) is not None,
            "via": "builtin",
        }

    if name in APP_PATHS:
        hit = _locate(name, shortcut_index=shortcut_index)
        if hit:
            return {"target": hit[0], "found": True, "via": hit[1]}
        # Best effort: `OpenApp` attempts the alias and reports honestly.
        return {"target": APP_PATHS[name][0], "found": False, "via": "alias"}

    if _DETECT_EXT.search(name) or os.path.sep in name:
        # A caller-supplied path is honoured only when it exists and is not a
        # script type.
        try:
            validate_app_target(name)
        except SecurityError:
            return {"target": None, "found": False, "via": "rejected"}
        if os.path.exists(name):
            return {"target": name, "found": True, "via": "path"}
        return {"target": None, "found": False, "via": "missing"}

    hit = _locate(name, shortcut_index=shortcut_index)
    if hit:
        return {"target": hit[0], "found": True, "via": hit[1]}
    return {"target": None, "found": False, "via": "none"}


def resolve_windows_app(app: str) -> Optional[str]:
    """Resolve an application name to a launch target, or ``None``.

    A thin view over :func:`resolve_app_detail` — kept at module level (and
    re-exported from ``tools.executor``) because the security-sandbox suite
    tests it directly, and every one of its return values must stay exactly
    what they were before the verdict existed.
    """
    return resolve_app_detail(app)["target"]


def _under_start_menu(path: str) -> bool:
    """Whether ``path`` is a file inside one of this machine's Start Menus.

    This is the only place a ``.lnk`` becomes launchable. A shortcut *is* an
    executable indirection — that is why ``BLOCKED_EXECUTABLE_SUFFIXES``
    contains it — but the Start Menu is a directory Windows itself populates
    with the user's own programs, and refusing every entry there made
    ``open_app("chrome")`` fail on the single artifact the OS advertises for
    it. Containment is checked with ``commonpath`` so a sibling directory
    sharing a name prefix cannot impersonate the Start Menu.
    """
    if not path:
        return False
    try:
        candidate = os.path.normcase(os.path.realpath(path))
    except OSError:
        return False
    for root in start_menu_roots():
        try:
            base = os.path.normcase(os.path.realpath(root))
            if os.path.commonpath([candidate, base]) == base:
                return True
        except ValueError:
            # Different drives, or a malformed path: not contained.
            continue
    return False


#: A URI scheme (`spotify:`, `ms-settings:`), as opposed to a Windows drive.
_URI_TARGET = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")


def _is_uri_target(target: str) -> bool:
    if not _URI_TARGET.match(target or ""):
        return False
    # `C:\...` also starts with `X:`; a drive letter is a path, not a scheme.
    return not re.match(r"^[A-Za-z]:[\\/]", target)


def validate_launch_target(target: str) -> str:
    """Sanity-check anything about to be handed to the OS, or raise.

    Distinct from :func:`tools.sandbox.validate_app_target` on purpose. That
    one refuses ``.lnk`` unconditionally and must keep doing so: it is the
    generic gate for a caller-supplied target. This one *adds* a narrow
    exception — a shortcut inside a Start Menu root — and delegates every other
    rule (shell syntax, script suffixes) to it unchanged.
    """
    text = str(target or "").strip()
    if not text:
        raise SecurityError("Refusing to launch: no target")
    if has_shell_metachars(text):
        raise SecurityError(
            f"Refusing to launch: target contains shell metacharacters: {text!r}"
        )

    suffix = os.path.splitext(text)[1].lower()
    if suffix == ".lnk":
        if not _under_start_menu(text):
            raise SecurityError(
                "Refusing to launch a shortcut outside the Start Menu: "
                f"{os.path.basename(text)}"
            )
        if not os.path.isfile(text):
            raise SecurityError(f"Refusing to launch a missing shortcut: {text}")
        return text

    validate_app_target(text)
    # Verify *now*, not when the scan that produced this target ran. An
    # application can be uninstalled between discovery and a request, and a
    # stale target must produce an honest failure rather than a launch attempt.
    if suffix == ".exe" and not os.path.isfile(text):
        raise SecurityError(f"Application is no longer installed: {text}")
    return text


async def launch_target(target: str) -> ToolResult:
    """Launch a vetted target with the OS's own handler.

    ``os.startfile`` (ShellExecute) is used on Windows instead of
    ``cmd /c start "" <target>``. The shell form exists to give ``start`` a
    title argument, which makes a target that begins with a quote or contains
    an ``&`` a parsing question at every call site; ShellExecute takes the
    path as a single argument and performs the same association lookup the
    Start Menu does. It is also what makes a Start Menu ``.lnk`` work at all:
    the shortcut must be *resolved* by the shell, not executed as a file.
    """
    try:
        checked = validate_launch_target(target)
    except SecurityError as e:
        return ToolResult.denied(str(e))

    if not _is_uri_target(checked) and not os.path.exists(checked):
        return ToolResult.fail(f"Target no longer exists: {checked}")

    try:
        if _IS_WINDOWS:
            startfile = getattr(os, "startfile", None)
            if startfile is None:  # pragma: no cover - non-Windows os module
                raise OSError("This platform has no shell launcher.")
            await asyncio.get_running_loop().run_in_executor(None, startfile, checked)
        else:
            opener = "open" if _IS_MACOS else "xdg-open"
            proc = await asyncio.create_subprocess_exec(
                opener,
                checked,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode not in (0, None):
                return ToolResult.fail(
                    f"Could not open {os.path.basename(checked)} "
                    f"(exit {proc.returncode})"
                )
    except FileNotFoundError:
        return ToolResult.fail(
            f"Could not open {os.path.basename(checked)}: no handler is installed."
        )
    except OSError as e:
        return ToolResult.fail(f"Could not open {os.path.basename(checked)}: {e}")

    return ToolResult.ok(
        f"Opened {os.path.basename(checked) or checked}",
        {"target": checked},
    )


def _normalise(params: Dict[str, Any]) -> str:
    raw = params.get("app") or params.get("name") or params.get("application")
    if not isinstance(raw, str) or not raw.strip():
        raise ToolValidationError("'app' is required")
    try:
        return sanitize_app_name(raw).lower()
    except SecurityError as e:
        raise ToolValidationError(str(e)) from None


def _site_fallback(name: str) -> Optional[str]:
    """The URL for a well-known site named by ``name``, or ``None``.

    Imported lazily: :mod:`core.targets` resolves *applications* by calling
    into this module, so a module-level import in both directions would be a
    cycle. The lookup itself is a table read.
    """
    try:
        from core.targets import find_site_url

        return find_site_url(name)
    except Exception:  # a resolver failure must not break app launching
        return None


async def _open_site(name: str, url: str) -> ToolResult:
    """Open ``url`` through the one browser-policy entry point."""
    from tools.web import OpenUrl

    result = await OpenUrl().execute(url=url)
    if not result.success:
        return result
    return ToolResult.ok(
        f"Opened {name} at {url}",
        {"app": name, "url": url, "via": "website"},
    )


class OpenApp(BaseTool):
    permission = "safe"

    def name(self) -> str:
        return "open_app"

    def aliases(self) -> List[str]:
        return ["start_app", "launch_app"]

    def description(self) -> str:
        return (
            "Open a desktop application by name, e.g. spotify, vscode, chrome, "
            "notepad, calculator. The same name works on all three systems: "
            "Windows searches the Start Menu shortcuts, macOS finds the .app "
            "bundle in /Applications, Linux uses the PATH. If nothing was found "
            "the tool says which places it looked, and never launches a guess."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "app": _prop("string", "Application name, or a path to an executable."),
            },
            "required": ["app"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        _normalise(params)

    async def execute(self, app: str = "", **_: Any) -> ToolResult:
        target = _normalise({"app": app})
        try:
            return await self._open(target)
        except SecurityError as e:
            return ToolResult.denied(str(e))
        except FileNotFoundError:
            return ToolResult.fail(f"Application '{target}' is not installed.")
        except OSError as e:
            return ToolResult.fail(f"Could not open {target}: {e}")

    @staticmethod
    async def _open(target: str) -> ToolResult:
        detail = resolve_app_detail(target)

        if _IS_WINDOWS:
            if detail.get("found") and detail.get("target"):
                return await launch_target(detail["target"])

            # A name that is also a well-known website opens the site. The
            # request "open YouTube" has a correct answer on a machine with no
            # YouTube application, and replying "could not find application"
            # is a failure invented by the resolver, not a fact about the
            # machine. The table is hard-coded https, and the URL still travels
            # through `OpenUrl`, so the browser policy has one implementation.
            site = _site_fallback(target)
            if site:
                return await _open_site(target, site)

            # `spotify:` / `ms-settings:` are launch recipes, not proof of
            # installation; attempt them last, so a real artifact always wins.
            if detail.get("target") and detail.get("via") in ("builtin", "alias"):
                return await launch_target(detail["target"])

            return ToolResult.fail(
                f"Could not find application '{target}' on this system. "
                "Looked on PATH, in the Start Menu (user and machine), in the "
                "App Paths and installed-application registry keys, and in "
                "ZAYTOON_APP_DIRS."
            )

        argv = _resolve_posix(target)
        if argv is None:
            site = _site_fallback(target)
            if site:
                return await _open_site(target, site)
            return ToolResult.fail(f"Could not find application '{target}'.")
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return ToolResult.ok(
            f"Opened {target}", {"app": target, "pid": getattr(proc, "pid", None)}
        )


def _resolve_posix(app: str) -> Optional[List[str]]:
    """The argv that launches `app`, or None when nothing on disk says it exists.

    On macOS the bundle is handed to `open`, not executed directly: launching
    `Spotify.app/Contents/MacOS/Spotify` starts the binary outside its launchd
    session, which is how apps end up without their environment, their single
    -instance guard, or their dock icon.
    """
    if _IS_MACOS:
        for candidate in (APP_PATHS.get(app, [app])[0], app):
            bundle = _mac_bundle(candidate)
            if bundle:
                return ["open", bundle]
    if app in APP_PATHS:
        alias = APP_PATHS[app][0]
        found = shutil.which(alias)
        return [found] if found else None
    found = shutil.which(app)
    return [found] if found else None


class CloseApp(BaseTool):
    permission = "confirm"

    def name(self) -> str:
        return "close_app"

    def aliases(self) -> List[str]:
        return ["kill_process", "quit_app"]

    def description(self) -> str:
        return "Close a running application. Requires confirmation."

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {"app": _prop("string", "Application name.")},
            "required": ["app"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        _normalise(params)

    def risk_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"app": (params.get("app") or params.get("name") or "")}

    async def execute(self, app: str = "", **_: Any) -> ToolResult:
        target = _normalise({"app": app})
        # Only applications Zaytoon knows about may be closed.
        #
        # The previous code passed an arbitrary name straight into
        # `taskkill /IM <name>.exe /F` on Windows and, worse, `pkill -f <name>`
        # elsewhere — `-f` matches the whole command line, so one confirmed
        # dialog could terminate anything whose command line contained the
        # text, and any name at all could be aimed at the task list. Bounded to
        # our own tables, resolved to an exact binary, and refused (not
        # guessed) on a miss.
        if target not in _KNOWN_APP_NAMES:
            return ToolResult.denied(
                f"Refusing to close '{target}': it is not one of Zaytoon's known "
                "applications. Closing arbitrary processes by name is not supported."
            )

        if _IS_WINDOWS:
            exe = target if target.endswith(".exe") else f"{target}.exe"
            if target in APP_PATHS:
                alias = APP_PATHS[target][0]
                exe = alias if _DETECT_EXT.search(alias) else f"{alias}.exe"
            argv = ["taskkill", "/IM", exe, "/F"]
        else:
            # `-x` matches the process *name* exactly; `-f` (full command line)
            # is what made an arbitrary target dangerous.
            proc_name = os.path.basename(APP_PATHS.get(target, [target])[0])
            argv = ["pkill", "-x", proc_name]

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
        except FileNotFoundError:
            return ToolResult.fail(f"Could not close {target}: helper not available.")
        except OSError as e:
            return ToolResult.fail(f"Could not close {target}: {e}")

        if proc.returncode == 0:
            return ToolResult.ok(f"Closed {target}")
        detail = _decode(stderr)
        return ToolResult.fail(
            detail or f"No running process found for '{target}'"
        )


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace").strip()


def build_tools() -> List[BaseTool]:
    return [OpenApp(), CloseApp()]
