"""Web tools: search and open a URL.

``search_web`` existed before and used DuckDuckGo via ``ddgs``. ``open_url`` is
new and is deliberately limited to http/https: a ``file://`` or ``javascript:``
URL handed to a desktop assistant's "open this" tool is a local-read and
script-execution primitive respectively, and there is no legitimate reason for
the assistant to open one on the user's behalf.
"""

from __future__ import annotations

import asyncio
import logging
import re
import webbrowser
from typing import Any, Dict, List
from urllib.parse import urlparse

from tools.base import BaseTool, ToolResult, ToolValidationError, _prop
from tools.sandbox import SecurityError

logger = logging.getLogger("Zaytoon.Tools.Web")

#: Only these schemes may be opened. See the module docstring.
ALLOWED_SCHEMES = ("http", "https")

MAX_RESULTS = 8

#: RFC 3986 scheme grammar. Used to tell "has a scheme" from "has no scheme",
#: which a naive ``"://" in url`` test gets wrong for ``javascript:`` and
#: ``data:`` — the two schemes most worth refusing.
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")


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
        return "Open an http or https URL in the default browser."

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
        # A refused scheme is a policy decision, so it surfaces as `blocked`
        # rather than as a validation error the user could retry around.
        self._check(url)

    @staticmethod
    def _check(url: str) -> str:
        candidate = url.strip()
        # Detect a scheme properly. Testing for "://" is not enough: both
        # "javascript:alert(1)" and "data:text/html,..." are schemeless-looking
        # to that test, so prefixing them with "https://" would turn a refused
        # scheme into an accepted one with a nonsense netloc.
        if not _SCHEME_RE.match(candidate):
            candidate = f"https://{candidate}"
        parsed = urlparse(candidate)
        if parsed.scheme.lower() not in ALLOWED_SCHEMES:
            raise SecurityError(
                f"Only {' and '.join(ALLOWED_SCHEMES)} URLs may be opened "
                f"(got {parsed.scheme or 'no scheme'!r})"
            )
        if not parsed.netloc:
            raise ToolValidationError(f"Not a valid URL: {url}")
        return candidate

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
