"""The per-turn source ledger.

The product decision this implements (2026-10-11): claims are **stamped, not
refused**. An answer that could not be sourced is labelled unverified rather than
deleted, and every source carries the time it was retrieved — because "as of when"
is the difference between a fact and a screenshot of the internet.

Three rules shape the design:

* **A source is created by what actually ran.** Entries come from tool outcomes
  (`fetch_url`, `search_web`), never from the model's prose. A URL the model
  *mentions* is not evidence; a page this turn *read* is.
* **A fetched page and a search snippet are different kinds of thing.** The
  snippet is marked "page not read", and the citing rule tells the model dates,
  versions and numbers must come from a read page. That is the difference between
  "sourced" and "quoted from a search engine's summary of something".
* **Same URL, one entry.** Two fetches of a page (or a snippet that later got
  read) must not become two citations pointing at one document; a duplicate
  upgrades the existing entry instead.

Numbers are positional and stable for the whole turn, so `[2]` in the answer means
the same thing as `[2]` in the prompt block the model was shown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

#: How many sources are listed in the prompt block. Beyond this, the list itself
#: crowds out the user's question — the same failure the instructions cap exists
#: to prevent (`MAX_INSTRUCTIONS_CHARS` in the skill manifest).
MAX_LISTED_SOURCES = 12

#: `[1]`, `[12]` — and only digits, so a markdown link `[text](url)` is not a
#: citation and a bracketed note like `[see below]` is not one either.
_CITATION_RE = re.compile(r"\[(\d{1,3})\]")

KIND_PAGE = "page"
KIND_RESULT = "result"


def _now() -> str:
    """Same shape as `tools.fetch._stamp()`, on purpose."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc or url
    except ValueError:  # pragma: no cover - defensive
        return url


@dataclass
class Source:
    """One document this turn actually touched."""

    index: int
    kind: str
    url: str
    title: str = ""
    retrieved_at: str = field(default_factory=_now)

    @property
    def read(self) -> bool:
        return self.kind == KIND_PAGE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n": self.index,
            "kind": self.kind,
            "url": self.url,
            "host": _host(self.url),
            "title": self.title,
            "retrieved_at": self.retrieved_at,
        }

    def line(self, language: str = "en") -> str:
        what = (
            f"{_host(self.url)} — {self.title}" if self.title else _host(self.url)
        )
        state = (
            ("read" if self.read else "snippet only, page not read")
            if language != "ar"
            else ("مقروءة" if self.read else "مقتطف فقط، الصفحة مقروءة")
        )
        return f"[{self.index}] {what} — {self.url} ({state}, {self.retrieved_at})"


class SourceLedger:
    """Numbered sources for one agent turn."""

    def __init__(self, *, now: Optional[Any] = None) -> None:
        self.items: List[Source] = []
        self.queries: List[str] = []
        self._by_url: Dict[str, Source] = {}
        self._now = now or _now

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def ingest(
        self, name: str, params: Dict[str, Any], result: Dict[str, Any]
    ) -> List[Source]:
        """Record what a tool outcome read. Returns the entries *newly added*.

        Never raises, and ignores anything it does not understand: an unexpected
        shape from a tool must not become a failed turn. The model simply gets no
        citation to use for it, which is the correct outcome anyway — unread
        material is not a source.
        """
        if not isinstance(result, dict) or not result.get("success"):
            return []
        data = result.get("data")
        if not isinstance(data, dict):
            return []

        added: List[Source] = []
        try:
            if name == "fetch_url":
                url = str(data.get("url") or "").strip()
                if url:
                    source = self._add(url, kind=KIND_PAGE)
                    if source is not None:
                        added.append(source)
            elif name == "search_web":
                query = str((params or {}).get("query") or "").strip()
                if query and query not in self.queries:
                    self.queries.append(query)
                for item in data.get("results") or []:
                    if not isinstance(item, dict):
                        continue
                    url = str(item.get("href") or item.get("url") or "").strip()
                    if not url:
                        continue
                    source = self._add(
                        url,
                        kind=KIND_RESULT,
                        title=str(item.get("title") or "")[:120],
                    )
                    if source is not None:
                        added.append(source)
        except Exception:  # noqa: BLE001 - a ledger failure must not fail a turn
            return added
        return added

    def _add(
        self, url: str, *, kind: str, title: str = ""
    ) -> Optional[Source]:
        key = url.strip().rstrip("/")
        existing = self._by_url.get(key)
        stamp = self._now()
        if existing is None:
            source = Source(
                index=len(self.items) + 1,
                kind=kind,
                url=key,
                title=title,
                retrieved_at=stamp,
            )
            self.items.append(source)
            self._by_url[key] = source
            return source
        # A duplicate never mints a second number. Reading a page that was only
        # seen as a snippet upgrades it in place, and refreshes the stamp: the
        # turn's answer should cite the time it last looked, not the first.
        if kind == KIND_PAGE and not existing.read:
            existing.kind = KIND_PAGE
        if title and not existing.title:
            existing.title = title
        existing.retrieved_at = stamp
        return None

    # ------------------------------------------------------------------
    # What the model sees
    # ------------------------------------------------------------------
    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def pages(self) -> int:
        return sum(1 for item in self.items if item.read)

    def listed(self) -> List[Source]:
        """Pages first: they are the citable evidence, snippets are leads."""
        return sorted(self.items, key=lambda s: (not s.read, s.index))

    def prompt_block(self, language: str = "en") -> str:
        if not self.items:
            return ""
        shown = self.listed()[:MAX_LISTED_SOURCES]
        lines = [source.line(language) for source in shown]
        hidden = self.count - len(shown)
        if hidden > 0:
            lines.append(f"(…{hidden} more result(s) not listed)")
        rule = (
            "Cite as [n] immediately after the claim it supports. A date, version, "
            "number or name must come from a READ page, not from a snippet. Anything "
            "you cannot place on this list starts with 'Unverified:'."
            if language != "ar"
            else "اكتب [n] بعد كل معلومة مباشرة. الأرقام والتواريخ والنسخ لازم تكون من "
            "صفحة مقروءة مش من مقتطف. أي حاجة مش موجودة في القائمة دي اكتب قبلها "
            "'غير موثّق:'."
        )
        heading = (
            f"SOURCES for this turn ({self.count} read or seen, {self.pages} "
            "fetched in full):"
            if language != "ar"
            else f"مصادر الجولة دي ({self.count}، منها {self.pages} اتقروا بالكامل):"
        )
        return heading + "\n" + "\n".join(lines) + "\n" + rule

    # ------------------------------------------------------------------
    # What the user sees
    # ------------------------------------------------------------------
    def method_line(self, language: str = "en") -> str:
        read, seen = self.pages, self.count - self.pages
        queries = len(self.queries)
        if language == "ar":
            return (
                f"الطريقة: {queries} عملية بحث، {read} صفحة اتقروا بالكامل، "
                f"{seen} مقتطف بس. كلوقت الاسترجاع UTC."
            )
        return (
            f"Method: {queries} search quer{'y' if queries == 1 else 'ies'}, "
            f"{read} page(s) read in full, {seen} snippet(s) not opened. "
            "Retrieved-at times are UTC."
        )

    def appendix(self, language: str = "en") -> str:
        if not self.items:
            return ""
        heading = "Sources" if language != "ar" else "المصادر"
        ordered = sorted(self.items, key=lambda s: s.index)
        return (
            f"\n\n---\n**{heading}**\n"
            + "\n".join(source.line(language) for source in ordered)
            + f"\n\n_{self.method_line(language)}_"
        )

    def unbacked_citations(self, text: str) -> List[int]:
        """`[n]` in the answer with no such source — the one falsifiable claim.

        A citation to a source this turn never read is not a stylistic problem: it
        is the model inventing provenance, which is precisely what the stamping
        scheme was adopted to prevent. Everything else about citations (style,
        placement, whether a claim *should* have had one) is not checkable here.
        """
        if not text:
            return []
        return sorted(
            {
                int(n)
                for n in _CITATION_RE.findall(text)
                if not 1 <= int(n) <= self.count
            }
        )

    def stamp(self, answer: str, language: str = "en") -> tuple[str, List[str]]:
        """The answer, plus notes on what was appended and why.

        Appends are additive by design (the same choice as P5's claim
        verification): the model's sentences stay, the ledger's truth goes under
        them. Returns ``(text, notes)`` so the caller can record the notes in
        `LoopResult.errors` and a test can assert on them.
        """
        notes: List[str] = []
        text = answer or ""
        fake = self.unbacked_citations(text)
        if fake:
            listed = ", ".join(f"[{n}]" for n in fake)
            notes.append(f"cited {len(fake)} unknown source(s)")
            warning = (
                f"Note: {listed} cite no source read in this turn, so nothing here "
                "is supported by one."
                if language != "ar"
                else f"ملاحظة: {listed} مش بيشير لأي مصدر اتقرا في الجولة دي."
            )
            text = f"{text}\n\n{warning}".strip()
        if self.count:
            text = text + self.appendix(language)
        elif "[1]" in text:
            # No sources at all, and a citation-shaped token in the answer.
            notes.append("citations with no sources this turn")
        return text, notes


__all__ = ["KIND_PAGE", "KIND_RESULT", "Source", "SourceLedger"]
