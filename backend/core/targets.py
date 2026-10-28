"""Target resolution for "open X" and "find X" requests.

An Egyptian-Arabic or English request reaches the tool layer as a sentence, not
as a typed argument: *"افتح يوتيوب"*, *"شغل كروم"*, *"دورلي على ملف اسمه
report"*. This module turns that sentence into one of five answers — a website,
an installed application, a file, a folder, or an honest "I found several / I
found nothing" — and it is deliberately the **only** place that decides.

Two separations matter here and are the reason this is not part of
``tools/applications.py``:

* **Discovery is read-only.** :func:`search_paths` and :func:`authorized_roots`
  only stat and list. They never open a file's content and never launch
  anything. Launching is :mod:`tools.launcher`, behind the registry's gate.
* **The search has edges.** A filename search over a user profile must not
  become a whole-drive scan: roots come from an explicit policy list
  (workspace, the OS's own known folders, ``ZAYTOON_SEARCH_ROOTS``), depth and
  entry counts are capped, and the walk does not follow directory symlinks.

Website resolution lives here too, because "open YouTube" on a machine with no
YouTube application has a correct answer — ``https://www.youtube.com/`` — and
reporting failure would be a fact about the resolver, not about the machine.
Nothing in this module sends a filename, a path, or the application inventory
anywhere: the tables are local and the only outbound value is a hard-coded
public URL.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("Zaytoon.Targets")

#: Suffixes that are never opened on the user's behalf even when the file
#: exists and sits inside an authorized root. Opening is a ShellExecute: a
#: script, installer, macro-enabled document or shell-link re-opens the exact
#: injection surface the tool gate exists to close, and a filename is not a
#: permission. Ordinary documents and media are unaffected.
REFUSED_OPEN_SUFFIXES = frozenset(
    {
        # executables and scripts
        ".exe", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe",
        ".js", ".jse", ".wsf", ".wsh", ".hta", ".scr", ".cpl", ".msi",
        ".msp", ".msu", ".reg", ".jar", ".py", ".pyw", ".sh", ".bash",
        ".zsh", ".fish", ".appref-ms", ".application",
        # link containers — the target is somewhere else and unverified
        ".lnk", ".url", ".website", ".scf", ".pif", ".inf",
        # macro-enabled office formats: opening can run code
        ".docm", ".dotm", ".xlsm", ".xltm", ".xlam", ".pptm", ".potm",
        ".ppam", ".sldm",
    }
)

#: Directories a filename search never descends into. Build output, package
#: caches and VCS metadata contain enormous numbers of generated files whose
#: names collide with real documents; walking them costs the budget and returns
#: noise.
_SKIP_DIR_NAMES = frozenset(
    {
        "node_modules", "__pycache__", ".git", ".svn", ".hg", ".venv", "venv",
        "env", "dist", "build", "obj", "bin", ".idea", ".vscode", ".next",
        ".nuxt", ".cache", "cache", "temp", "tmp", "$recycle.bin",
        "system volume information", "windows", "program files",
        "program files (x86)", "programdata", "appdata",
    }
)

#: The public sites a bare name may mean. Keys are normalised by
#: :func:`normalise_text`, so English case and Arabic spelling variants both
#: land on the same entry. Every value is https and hard-coded: no user text is
#: interpolated into a URL, which is what keeps this from becoming an
#: "open whatever the model typed" primitive.
KNOWN_SITES: Dict[str, str] = {
    # English
    "youtube": "https://www.youtube.com/",
    "you tube": "https://www.youtube.com/",
    "google": "https://www.google.com/",
    "gmail": "https://mail.google.com/",
    "google drive": "https://drive.google.com/",
    "drive": "https://drive.google.com/",
    "google maps": "https://www.google.com/maps",
    "maps": "https://www.google.com/maps",
    "google translate": "https://translate.google.com/",
    "translate": "https://translate.google.com/",
    "facebook": "https://www.facebook.com/",
    "instagram": "https://www.instagram.com/",
    "twitter": "https://x.com/",
    "x": "https://x.com/",
    "linkedin": "https://www.linkedin.com/",
    "reddit": "https://www.reddit.com/",
    "tiktok": "https://www.tiktok.com/",
    "netflix": "https://www.netflix.com/",
    "amazon": "https://www.amazon.com/",
    "github": "https://github.com/",
    "stack overflow": "https://stackoverflow.com/",
    "stackoverflow": "https://stackoverflow.com/",
    "wikipedia": "https://www.wikipedia.org/",
    "whatsapp": "https://web.whatsapp.com/",
    "telegram": "https://web.telegram.org/",
    "chatgpt": "https://chatgpt.com/",
    "spotify web": "https://open.spotify.com/",
    "outlook web": "https://outlook.live.com/",
    # Egyptian Arabic (normalised spellings; ة→ه and ى→ي are applied)
    "يوتيوب": "https://www.youtube.com/",
    "يو تيوب": "https://www.youtube.com/",
    "جوجل": "https://www.google.com/",
    "جيميل": "https://mail.google.com/",
    "فيس بوك": "https://www.facebook.com/",
    "فيسبوك": "https://www.facebook.com/",
    "انستجرام": "https://www.instagram.com/",
    "انستقرام": "https://www.instagram.com/",
    "تويتر": "https://x.com/",
    "لينكد ان": "https://www.linkedin.com/",
    "ريديت": "https://www.reddit.com/",
    "تيك توك": "https://www.tiktok.com/",
    "نتفليكس": "https://www.netflix.com/",
    "امازون": "https://www.amazon.com/",
    "جيت هاب": "https://github.com/",
    "جيثب": "https://github.com/",
    "ستاك اوفرفلو": "https://stackoverflow.com/",
    "ويكيبيديا": "https://www.wikipedia.org/",
    "واتساب": "https://web.whatsapp.com/",
    "تليجرام": "https://web.telegram.org/",
    "شات جي بي تي": "https://chatgpt.com/",
    "خرايط": "https://www.google.com/maps",
    "خرايط جوجل": "https://www.google.com/maps",
    "ترجمه": "https://translate.google.com/",
}

_ARABIC_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")

_LEADING_WAKE = re.compile(
    r"^(?:يا\s+|hey\s+|ok\s+|okay\s+)?(?:zaytoon|زيتون)\s*[,،:]?\s*", re.IGNORECASE
)
_TRAILING_POLITE = re.compile(
    r"\s+(?:please|plz|thanks|thank you|now|من فضلك|لو سمحت|بليز|شكرا|دلوقتي)$",
    re.IGNORECASE,
)

#: Search asks. Checked **before** open verbs because "دورلي على ملف" also
#: contains an imperative and a search that is answered by opening the wrong
#: thing is worse than no answer.
_SEARCH_PREFIX = re.compile(
    r"^(?:please\s+|can you\s+|could you\s+)?"
    r"(?:find|locate|search for|look for|where is|where's|where are|"
    r"دورلي|دور لي|دوّرلي|دوّر لي|دور على|دوّر على|دورلي على|"
    r"ابحث عن|ابحثلي|ابحث لي|فين|لاقيلي|لاقي لي|لاقي|هاتلي|هات لي)\s+",
    re.IGNORECASE,
)

_OPEN_PREFIX = re.compile(
    r"^(?:please\s+|can you\s+|could you\s+)?"
    r"(?:open|launch|start|run|show me|go to|visit|"
    r"افتح|فتح|شغل|شغّل|ابدا|ابدأ|اعرض|وريني|روح على|خدني على)\s+",
    re.IGNORECASE,
)

#: Filler left over after the verb: "دورلي **على** ملف …".
_LEADING_FILLER = re.compile(
    r"^(?:على|عن|لي|لى|في|ف|the|a|an|my)\s+", re.IGNORECASE
)

#: "file named X", "مجلد اسمه X" — the noun is the request, the rest is the name.
_NAMED_THING = re.compile(
    r"^(?:ال)?(?:file|folder|directory|document|فايل|ملف|مجلد|فولدر|مستند)"
    r"(?:\s+(?:called|named|اسمه|اسمها|باسم))?\s*[:\-]?\s*",
    re.IGNORECASE,
)

_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,8}$")
_HAS_PATH_SEPARATOR = re.compile(r"[\\/]")

#: Openers that make a sentence a question rather than a request to open or
#: find something. Checked only when no open/search verb matched, so
#: "can you open chrome" is unaffected. Without it, "what is the weather" fell
#: through to a filename search and came back with eight unrelated Downloads
#: files — an answer that is worse than saying "that is a question".
_QUESTION = re.compile(
    r"^(?:what|why|how|who|whom|whose|when|which|where|"
    r"is|are|was|were|can|could|should|would|will|do|does|did|"
    r"tell me|explain|define|"
    r"ايه|إيه|مين|امتى|إمتى|ليه|ليش|ازاي|إزاي|هل|كام|اشرح|قولي|عايز اعرف)\b",
    re.IGNORECASE,
)


def normalise_text(text: str) -> str:
    """Fold English case and Arabic spelling variants into one key.

    ``ة``/``ه``, ``ى``/``ي``, the alef forms and the optional tatweel are not
    meaningful distinctions in a filename search or a site name, and the
    codebase already normalises Arabic this way in :mod:`arabic`. Digit shapes
    are deliberately *not* folded here: a filename may legitimately contain
    either.
    """
    s = _ARABIC_DIACRITICS.sub("", str(text or ""))
    s = s.casefold()
    s = (
        s.replace("أ", "ا")
        .replace("إ", "ا")
        .replace("آ", "ا")
        .replace("ة", "ه")
        .replace("ى", "ي")
        .replace("ؤ", "و")
        .replace("ئ", "ي")
    )
    s = re.sub(r"\s+", " ", s).strip()
    return s.strip(" \t\r\n.,!?؟:;\"'`«»[](){}")


#: Site lookup uses the same normalisation the request does, once, at import.
_SITE_INDEX: Dict[str, str] = {
    normalise_text(key): url for key, url in KNOWN_SITES.items()
}


def find_site_url(name: str) -> Optional[str]:
    """The URL for a known public site named by ``name``, or ``None``.

    Tolerates the forms people actually say: a trailing ``.com``, ``website``,
    or the Arabic ``دوت كوم``. A miss returns ``None`` rather than searching for
    a lookalike: the caller has an application resolver and a file search to
    fall back on, and a fuzzy site match is how "open my report" would become a
    web search.
    """
    query = normalise_text(name)
    if not query:
        return None
    for suffix in (" dot com", " دوت كوم", " website", " site", ".com", ".net", ".org"):
        if query.endswith(suffix):
            query = query[: -len(suffix)].strip()
            break
    if not query:
        return None
    return _SITE_INDEX.get(query)


def _strip_edges(text: str) -> str:
    s = _LEADING_WAKE.sub("", str(text or "").strip())
    s = _TRAILING_POLITE.sub("", s).strip()
    return s.strip(" \t\r\n.,!?؟:;\"'`«»")


def _clean_query(query: str) -> str:
    """Remove the words around a name, never the name itself."""
    s = _strip_edges(query)
    for _ in range(2):
        before = s
        s = _LEADING_FILLER.sub("", s)
        s = _NAMED_THING.sub("", s)
        s = s.strip(" \t\r\n.,!?؟:;\"'`«»")
        if s == before:
            break
    return s


def parse_request(text: str) -> Tuple[str, str]:
    """``(query, intent)`` for a spoken request.

    ``intent`` is ``"search"`` when the user asked to *find* something and
    ``"open"`` otherwise. The distinction is load-bearing: "find youtube" is a
    filename search, "open youtube" is the website, and collapsing the two is
    how a file search turns into an unrelated browser tab.
    """
    raw = _strip_edges(text)
    intent = "open"
    match = _SEARCH_PREFIX.match(raw)
    if match:
        intent = "search"
        raw = raw[match.end():]
    else:
        match = _OPEN_PREFIX.match(raw)
        if match:
            raw = raw[match.end():]
    return _clean_query(raw), intent


# ---------------------------------------------------------------------------
# Authorized roots
# ---------------------------------------------------------------------------

#: The `User Shell Folders` values that name real directories the user owns.
#: `Downloads` is normally stored under its GUID rather than the friendly name,
#: so both spellings are asked for.
_SHELL_FOLDER_NAMES = (
    "Desktop",
    "Personal",
    "Downloads",
    "Pictures",
    "Music",
    "Videos",
    "{374DE290-123F-4565-9164-39C4925E467B}",  # Downloads
    "OneDrive",
)

_HOME_DIR_NAMES = ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos")


def _windows_shell_folders() -> List[str]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows
        return []

    base = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
    found: List[str] = []
    for hive in (winreg.HKEY_CURRENT_USER,):
        try:
            with winreg.OpenKey(hive, base) as key:
                for name in _SHELL_FOLDER_NAMES:
                    try:
                        value, _ = winreg.QueryValueEx(key, name)
                    except OSError:
                        continue
                    if isinstance(value, str) and value.strip():
                        found.append(os.path.expandvars(value.strip()))
        except OSError:
            continue
    return found


def _config_roots() -> List[str]:
    raw = os.environ.get("ZAYTOON_SEARCH_ROOTS") or ""
    return [part.strip().strip('"') for part in raw.split(os.pathsep) if part.strip()]


def authorized_roots() -> List[str]:
    """Every directory a filename search may look in, in priority order.

    The list is a policy, not a disk scan: the Zaytoon workspace, an explicit
    operator list, the directories Windows itself declares as the user's
    (Desktop/Documents/Downloads and friends — which may live on another
    drive), then the conventional home-directory names. Directories that do not
    exist are dropped, so a OneDrive-less machine produces no phantom roots.
    """
    from tools.sandbox import workspace_root

    candidates: List[str] = []
    try:
        candidates.append(workspace_root())
    except Exception:  # pragma: no cover - defensive
        pass
    candidates.extend(_config_roots())
    candidates.extend(_windows_shell_folders())

    home = os.path.expanduser("~")
    if home and home != "~":
        candidates.extend(os.path.join(home, name) for name in _HOME_DIR_NAMES)

    roots: List[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        expanded = os.path.expandvars(os.path.expanduser(candidate))
        try:
            if not os.path.isdir(expanded):
                continue
            key = os.path.normcase(os.path.realpath(expanded))
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        roots.append(expanded)
    return roots


def is_authorized_path(path: str, roots: Optional[Sequence[str]] = None) -> bool:
    """True when ``path`` resolves inside one of :func:`authorized_roots`.

    ``commonpath`` on real paths, not a string prefix: a sibling directory
    called ``Documents_old`` must not inherit ``Documents``' authority. A
    different drive on Windows raises ``ValueError`` from ``commonpath`` and is
    correctly not contained.
    """
    if not path or not str(path).strip():
        return False
    try:
        candidate = os.path.realpath(
            os.path.abspath(os.path.expandvars(os.path.expanduser(str(path))))
        )
    except (OSError, ValueError):
        return False
    for root in roots if roots is not None else authorized_roots():
        try:
            base = os.path.realpath(root)
            if os.path.commonpath([candidate, base]) == base:
                return True
        except (OSError, ValueError):
            continue
    return False


# ---------------------------------------------------------------------------
# Bounded filename search
# ---------------------------------------------------------------------------

def _name_score(name: str, query: str, tokens: Sequence[str]) -> int:
    """How well ``name`` matches, or 0 for no match."""
    norm = normalise_text(name)
    if not norm:
        return 0
    stem = norm
    if "." in norm:
        stem = norm.rsplit(".", 1)[0]
    if norm == query or stem == query:
        return 100
    if stem.startswith(query) or norm.startswith(query):
        return 85
    if query in norm:
        return 70
    if tokens and all(token in norm for token in tokens):
        return 65
    if len(tokens) > 1 and any(token in norm for token in tokens):
        # Partial wording ("report q3" for `q3-report.pdf`) is a weak but real
        # match; it stays below every whole-query hit so it only wins when
        # nothing better exists.
        return 35
    return 0


def search_paths(
    query: str,
    *,
    roots: Optional[Sequence[str]] = None,
    limit: int = 12,
    max_depth: int = 4,
    max_entries: int = 30_000,
    time_budget_s: float = 3.0,
) -> List[Dict[str, Any]]:
    """Find files and folders whose **name** matches ``query``.

    Read-only and bounded, by construction:

    * roots are :func:`authorized_roots` unless the caller passes an explicit
      subset, and each candidate result is re-checked with
      :func:`is_authorized_path`;
    * ``max_depth`` counts directory levels below each root, so Documents'
      five nested project folders are reachable and a home-directory scan is
      not;
    * ``max_entries`` and ``time_budget_s`` stop a pathological tree from
      turning one request into minutes of disk I/O;
    * directory symlinks are never followed, so a link cannot pull an
      out-of-policy tree into the results;
    * file **contents are never read** — only names, and only ``stat`` for the
      file/folder flag.

    Results are sorted by match quality, then by shallow/short path, so an
    exact hit near the root beats a weak hit buried in a project.
    """
    cleaned = _clean_query(query)
    if not cleaned:
        return []
    needle = normalise_text(cleaned)
    tokens = [t for t in re.split(r"[^\w\u0600-\u06FF]+", needle) if t]
    if not tokens:
        return []

    search_roots = list(roots) if roots is not None else authorized_roots()
    deadline = time.monotonic() + max(0.1, float(time_budget_s))
    cap = max(1, int(limit))
    max_matches = max(60, cap * 8)
    seen_paths: set[str] = set()
    matches: List[Dict[str, Any]] = []
    visited = 0

    for root in search_roots:
        if not os.path.isdir(root):
            continue
        stack: List[Tuple[str, int]] = [(root, 0)]
        while stack:
            if visited >= max_entries or time.monotonic() > deadline:
                break
            if len(matches) >= max_matches:
                break
            directory, depth = stack.pop()
            try:
                entries = list(os.scandir(directory))
            except (PermissionError, OSError):
                continue
            for entry in entries:
                visited += 1
                if visited >= max_entries or time.monotonic() > deadline:
                    break
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                name = entry.name
                if name.startswith(".") and name not in (".", ".."):
                    continue
                score = _name_score(name, needle, tokens)
                if score:
                    try:
                        key = os.path.normcase(os.path.realpath(entry.path))
                    except OSError:
                        key = os.path.normcase(entry.path)
                    if key not in seen_paths:
                        seen_paths.add(key)
                        matches.append(
                            {
                                "path": entry.path,
                                "name": name,
                                "is_dir": bool(is_dir),
                                "score": score,
                            }
                        )
                if is_dir and depth < max_depth:
                    if name.casefold() in _SKIP_DIR_NAMES:
                        continue
                    stack.append((entry.path, depth + 1))

    matches.sort(
        key=lambda m: (
            -int(m["score"]),
            m["path"].count(os.sep),
            len(m["path"]),
            m["name"].casefold(),
        )
    )
    return matches[:cap]


# ---------------------------------------------------------------------------
# The one decision
# ---------------------------------------------------------------------------

def is_refused_open_target(path: str) -> Optional[str]:
    """The refusal reason for a path we must not open, or ``None``."""
    suffix = os.path.splitext(str(path or ""))[1].casefold()
    if suffix in REFUSED_OPEN_SUFFIXES:
        return (
            f"Refusing to open '{os.path.basename(path)}': "
            f"'{suffix}' files can execute code, and opening one is not a "
            "read-only action."
        )
    return None


def _as_candidate_path(query: str, roots: Sequence[str]) -> str:
    """Resolve a spoken path against the workspace, or return ``""``."""
    text = os.path.expandvars(os.path.expanduser(query.strip().strip('"')))
    if not text:
        return ""
    if not os.path.isabs(text):
        base = roots[0] if roots else os.getcwd()
        text = os.path.join(base, text)
    try:
        return os.path.realpath(text)
    except (OSError, ValueError):
        return ""


def _describe_match(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "path": item["path"],
        "name": item["name"],
        "is_dir": item["is_dir"],
    }


def resolve_open_request(
    text: str,
    *,
    roots: Optional[Sequence[str]] = None,
    limit: int = 8,
) -> Dict[str, Any]:
    """Decide what ``text`` should open, and say why.

    Returns a dict with ``kind`` one of ``website | app | file | folder |
    ambiguous | blocked | none`` plus the evidence behind it:

    - ``website``  → ``url`` and ``site``
    - ``app``      → ``target``, ``app``, ``via``
    - ``file`` / ``folder`` → ``path``
    - ``ambiguous`` → ``candidates`` (the caller must ask, never guess)
    - ``blocked``  → ``reason`` (the path exists but must not be opened)
    - ``none``     → ``reason`` (what was looked for and where)

    The order is deliberate. An explicit, authorized path wins because the user
    named it. A verified application wins over a website of the same name
    because it is the local thing they asked for. A website is *never* a
    failure fallback for a file search — that would answer "find report.pdf"
    with a browser tab — so it is only consulted when the request was an
    ``open`` and the name is not file-shaped.
    """
    private_roots = list(roots) if roots is not None else authorized_roots()
    query, intent = parse_request(text)
    evidence: Dict[str, Any] = {
        "request": text,
        "query": query,
        "intent": intent,
    }
    if not query:
        return {**evidence, "kind": "none", "reason": "No name was given."}

    # A question is not a target. Only when the sentence carried no open/find
    # verb of its own — "can you open chrome" is a request, "what is chrome"
    # is not — so this never shadows a real request.
    stripped = _strip_edges(text)
    if not (_OPEN_PREFIX.match(stripped) or _SEARCH_PREFIX.match(stripped)):
        if _QUESTION.match(stripped):
            return {
                **evidence,
                "kind": "none",
                "reason": (
                    "That reads as a question, not a request to open or find "
                    "something. Answer it directly instead."
                ),
            }

    # 1. A path the user spelled out, or a file that genuinely exists at the
    #    workspace-relative location they named.
    candidate = _as_candidate_path(query, private_roots)
    if candidate and os.path.exists(candidate):
        if not is_authorized_path(candidate, private_roots):
            return {
                **evidence,
                "kind": "none",
                "reason": (
                    f"'{query}' is outside the folders Zaytoon is allowed to "
                    "search. Add its parent to ZAYTOON_SEARCH_ROOTS to include it."
                ),
            }
        return _result_for_path(candidate, evidence)

    # 2. A search request never becomes a site or an application.
    if intent != "search":
        # 3. Installed application: positive evidence only.
        app_detail = _resolve_app(query)
        if app_detail and app_detail.get("found") and app_detail.get("target"):
            return {
                **evidence,
                "kind": "app",
                "target": app_detail["target"],
                "app": query,
                "via": app_detail.get("via", ""),
            }

        # 4. A known public site. After the app check, so an installed program
        #    keeps winning over its website.
        url = find_site_url(query)
        if url:
            return {**evidence, "kind": "website", "site": query, "url": url}

    # 5. Filename / folder search, which is also the answer when an "open"
    #    request named something that is not an app or a site.
    matches = search_paths(
        query,
        roots=private_roots,
        limit=max(1, int(limit)),
        max_depth=4,
    )
    if not matches:
        return {
            **evidence,
            "kind": "none",
            "reason": (
                f"No application, website, file or folder named '{query}' was "
                "found in the workspace or the known user folders."
            ),
        }
    if len(matches) == 1:
        return _result_for_path(matches[0]["path"], evidence)
    if matches[0]["score"] >= 100 and matches[0]["score"] > matches[1]["score"] + 20:
        # One exact name and nothing close: choosing it is not a guess.
        return _result_for_path(matches[0]["path"], evidence)
    return {
        **evidence,
        "kind": "ambiguous",
        "candidates": [_describe_match(m) for m in matches],
        "reason": (
            f"{len(matches)} items match '{query}'. Ask which one the user meant."
        ),
    }


def _resolve_app(query: str) -> Optional[Dict[str, Any]]:
    """Application evidence, or ``None`` when the resolver cannot answer.

    Kept behind a boundary because the application module imports this one for
    the website fallback; the import is local so the two directions never form
    a module cycle.
    """
    try:
        from tools.applications import resolve_app_detail

        return resolve_app_detail(query)
    except Exception as e:  # a resolver problem is not a crash in the turn
        logger.warning("Application resolution failed for %r: %s", query, e)
        return None


def _result_for_path(path: str, evidence: Dict[str, Any]) -> Dict[str, Any]:
    is_dir = os.path.isdir(path)
    refusal = None if is_dir else is_refused_open_target(path)
    if refusal:
        return {**evidence, "kind": "blocked", "path": path, "reason": refusal}
    return {
        **evidence,
        "kind": "folder" if is_dir else "file",
        "path": path,
        "name": os.path.basename(path.rstrip(os.sep)) or path,
    }
