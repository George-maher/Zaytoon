"""Web tools: search and open a URL.

``search_web`` existed before and used DuckDuckGo via ``ddgs``. ``open_url`` is
new and is deliberately limited to http/https: a ``file://`` or ``javascript:``
URL handed to a desktop assistant's "open this" tool is a local-read and
script-execution primitive respectively, and there is no legitimate reason for
the assistant to open one on the user's behalf.

The URL policy itself is **not** implemented here: it is
:func:`tools.netguard.parse_public_url`, the same function ``fetch_url`` runs on
its way out. Two copies of an address policy is how one of them ends up accepting
``http://169.254.169.254/`` while the other refuses it — and the page that
*tells* the assistant to open that URL is exactly the kind of text a fetched page
contains. What `open_url` deliberately does not do is resolve the name first:
the browser resolves it, so DNS-rebinding is mitigated for ``fetch_url`` and not
eliminated here (see the module docstring in ``tools/netguard.py``).
"""

from __future__ import annotations

import asyncio
import logging
import re
import webbrowser
from typing import Any, Dict, List

from tools import netguard
from tools.base import BaseTool, ToolResult, ToolValidationError, _prop
from tools.sandbox import SecurityError

logger = logging.getLogger("Zaytoon.Tools.Web")

#: Re-exported so a caller that read the scheme list off this module still gets
#: the truth: the policy lives in `tools/netguard.py`, and a second tuple here
#: would be a second policy to forget.
ALLOWED_SCHEMES = netguard.ALLOWED_SCHEMES

MAX_RESULTS = 8


class SearchWeb(BaseTool):
    permission = "safe"

    def name(self) -> str:
        return "search_web"

    def aliases(self) -> List[str]:
        return ["web_search", "search", "google"]

    def description(self) -> str:
        return "Search the web and return the top results with titles and snippets."

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": _prop("string", "Search terms."),
                "max_results": _prop("integer", "How many results.", default=5),
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        query = params.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolValidationError("'query' is required")

    async def execute(self, query: str = "", max_results: int = 5, **_: Any) -> ToolResult:
        try:
            from ddgs import DDGS
        except ImportError:
            return ToolResult.fail(
                "Web search is unavailable: the 'ddgs' package is not installed."
            )

        limit = max(1, min(int(max_results or 5), MAX_RESULTS))
        try:
            loop = asyncio.get_running_loop()
            results = await loop.run_in_executor(
                None, lambda: list(DDGS().text(query, max_results=limit))
            )
        except Exception as e:
            return ToolResult.fail(f"Search failed: {e}")

        if not results:
            return ToolResult.ok(f"No results for {query!r}.", {"results": []})

        lines = []
        for index, item in enumerate(results, 1):
            title = item.get("title", "No title")
            body = (item.get("body") or "")[:220]
            href = item.get("href") or item.get("url") or ""
            lines.append(f"{index}. {title}\n   {href}\n   {body}")
        return ToolResult.ok(
            f"{len(results)} result(s) for {query!r}:\n" + "\n".join(lines),
            {"results": results, "count": len(results)},
        )


class OpenUrl(BaseTool):
    permission = "safe"

    def name(self) -> str:
        return "open_url"

    def aliases(self) -> List[str]:
        return ["browse", "visit_url", "open_website"]

    def description(self) -> str:
        return (
            "Open an http or https URL in the user's default browser. Only web "
            "schemes are accepted, and addresses no browser should be sent to "
            "(a cloud metadata endpoint, a link-local or multicast address, this "
            "machine's own name) are refused with the reason."
        )

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {"url": _prop("string", "An http(s) URL.")},
            "required": ["url"],
            "additionalProperties": False,
        }

    def validate(self, params: Dict[str, Any]) -> None:
        url = params.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ToolValidationError("'url' is required")
        # A refused scheme or address is a policy decision, so it surfaces as
        # `blocked` rather than as a validation error the user could retry
        # around. `validate` is sync, so this is the string-level half of
        # netguard's policy only — the same split `fetch_url` uses.
        self._check(url)

    @staticmethod
    def _allow_private() -> bool:
        from config import config

        return bool(getattr(config, "OPEN_URL_ALLOW_PRIVATE", True))

    @staticmethod
    def _check(url: str) -> str:
        """The canonical, policy-approved URL to hand the browser.

        ``netguard`` owns every rule in here (scheme grammar, credentials in the
        netloc, address class), so the answer is its ``Target.url`` — a rebuilt
        URL — rather than the caller's string. That is the part that matters: an
        unmodified ``http://host@metadata.attacker/`` reads as a host to a
        sloppy parser and as credentials to a browser.
        """
        # Nothing is decided here, including the missing-scheme case: netguard
        # knows that `pypi.org:443/x` has a *port* where a naive test sees a
        # scheme, and that a bare `example.com` is an https page. A second copy of
        # that reasoning is a second thing to get wrong.
        target = netguard.parse_public_url(
            (url or "").strip(),
            allow_private=OpenUrl._allow_private(),
            verb="openable",
            action="opened",
        )
        return target.url

    async def execute(self, url: str = "", **_: Any) -> ToolResult:
        try:
            target = self._check(url)
        except (SecurityError, ToolValidationError) as e:
            return ToolResult.denied(str(e))
        try:
            opened = await asyncio.get_running_loop().run_in_executor(
                None, webbrowser.open, target
            )
        except webbrowser.Error as e:
            return ToolResult.fail(f"Could not open a browser: {e}")
        except Exception as e:
            return ToolResult.fail(f"Could not open {target}: {e}")
        if not opened:
            return ToolResult.fail(
                f"Could not open {target}: no browser is available on this system."
            )
        return ToolResult.ok(f"Opened {target}", {"url": target})


def build_tools() -> List[BaseTool]:
    return [SearchWeb(), OpenUrl()]
