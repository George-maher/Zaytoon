"""The single tool dispatch point.

There used to be two independent tool stacks:

* ``tools/executor.py::ToolExecutor`` — reached by the REST routes and the
  raw-JSON path. Had the risk gate, argument pre-validation and the better
  Windows implementation.
* ``agents/tool_agent.py::ToolAgent`` — reached by the LLM, i.e. the path that
  actually matters. Carried its own, weaker copies of ``open_app``,
  ``type_text``, ``key_press``, ``run_code``, ``search_web``, ``file_read``,
  ``file_write`` and ``system_command``.

The two had already drifted: the ``ToolAgent`` copies interpolated user text
into a ``powershell -Command`` string (injectable) while the ``ToolExecutor``
copies used ``-EncodedCommand``; the ``ToolExecutor`` pre-validated
``file_read``/``file_write`` but had no handler for either, so those two
returned ``"Unknown action"``; ``run_code`` supported five languages in one
place and three in the other.

This module is the fix: one registry, one implementation per tool, one gate.
``ToolExecutor`` is now a thin facade over it so existing callers and tests keep
working, and ``ToolAgent`` delegates here instead of duplicating anything.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

from tools.base import BaseTool, ToolResult, ToolValidationError
from common.reporting import safe_error
from tools.sandbox import SecurityError
from tools.safety import (
    PERMISSION_CONFIRM,
    PERMISSION_DANGEROUS,
    PERMISSION_SAFE,
    RiskAssessor,
    RiskAssessment,
    RiskLevel,
)

logger = logging.getLogger("Zaytoon.ToolRegistry")


class ToolRegistry:
    """Name → tool, plus the permission gate and argument validation.

    The gate lives here rather than in each tool so that no tool can forget it.
    ``execute`` is the *only* way to run a tool, and it always goes:

        validate()  →  risk gate  →  tool.execute()

    in that order. See :mod:`tools.base` for why the order is load-bearing.
    """

    def __init__(self, assessor: Optional[RiskAssessor] = None) -> None:
        self._tools: Dict[str, BaseTool] = {}
        self._aliases: Dict[str, str] = {}
        #: Canonical name -> a tier the tool may never run below, recorded by the
        #: *registry* rather than stored on the tool. See :meth:`register`.
        self._floors: Dict[str, str] = {}
        self.assessor = assessor or RiskAssessor()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def register(
        self,
        tool: BaseTool,
        replace: bool = False,
        *,
        floor: Optional[str] = None,
    ) -> BaseTool:
        """Register ``tool``, optionally remembering a tier it cannot go below.

        ``floor`` exists because the alternative — raising ``tool.permission``
        once at load, which is what the skill loader used to do — is a claim about
        an object the object can undo. ``BaseTool.permission`` is a plain mutable
        attribute, so a tool that assigns ``self.permission = "safe"`` inside
        ``execute()`` (or clears it to ``""``) drops out of the gate on the *next*
        call, after having been approved once. A skill is third-party code; the
        prompt has to hold on facts the registry knows, not on the honesty of the
        thing it is about to run.

        Floors are **sticky and monotone**: they are only ever set or raised here,
        and only :meth:`unregister` removes one. A later ``register(same_name,
        replace=True)`` without a ``floor`` therefore keeps the floor it already
        had, so "re-register it quietly" is not an escape hatch. The cost is that
        a name can stay floored after the tool behind it changes — which is the
        safe direction of being wrong, and what a trusted built-in re-registering
        itself would have to live with (built-ins are never floored, so in
        practice nothing does).
        """
        name = tool.name()
        if not name:
            raise ValueError(f"{type(tool).__name__} has an empty name()")
        if name in self._tools and not replace:
            raise ValueError(f"Tool '{name}' is already registered")
        if floor is not None:
            if floor not in _TIER_ORDER:
                raise ValueError(
                    f"unknown floor {floor!r}; expected one of "
                    f"{sorted(_TIER_ORDER)}"
                )
            current = self._floors.get(name)
            if current is None or _tier_rank(floor) > _tier_rank(current):
                self._floors[name] = floor
        self._tools[name] = tool
        for alias in tool.aliases():
            if alias == name:
                continue
            owner = self._aliases.get(alias)
            if owner and owner != name and not replace:
                raise ValueError(
                    f"Alias '{alias}' for '{name}' collides with existing tool '{owner}'"
                )
            self._aliases[alias] = name
        logger.debug("Registered tool %s (tier=%s)", name, tool.permission)
        return tool

    def register_all(self, tools: Iterable[BaseTool]) -> None:
        for tool in tools:
            self.register(tool, replace=True)

    def unregister(self, name: str) -> None:
        tool = self._tools.pop(name, None)
        if tool is None:
            return
        # The only way a floor is removed: the tool is gone. See `register`.
        self._floors.pop(tool.name(), None)
        for alias in [a for a, owner in self._aliases.items() if owner == name]:
            self._aliases.pop(alias, None)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------
    def get(self, name: str) -> Optional[BaseTool]:
        if not name:
            return None
        key = name.strip().lower()
        canonical = self._aliases.get(key, key)
        return self._tools.get(canonical)

    def has(self, name: str) -> bool:
        return self.get(name) is not None

    def list(self) -> List[str]:
        return sorted(self._tools)

    def tools(self) -> List[BaseTool]:
        return [self._tools[n] for n in sorted(self._tools)]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.has(name)

    def canonical(self, name: str) -> Optional[str]:
        """Resolve an alias to its registered name, or ``None`` if unknown."""
        if not name:
            return None
        key = name.strip().lower()
        return self._aliases.get(key, key) if key in self._tools or key in self._aliases else None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def describe(self) -> List[Dict[str, Any]]:
        """Everything the UI and the LLM need to know, as plain data."""
        return [
            {
                "name": tool.name(),
                "description": tool.description(),
                "schema": tool.schema(),
                "permission": self.effective_tier(tool.name()),
                "aliases": tool.aliases(),
            }
            for tool in self.tools()
        ]

    def openai_tools(self) -> List[Dict[str, Any]]:
        """Tool specs in OpenAI function-calling format.

        The backend does not use native tool calling yet (the model is asked
        for JSON and it is parsed), but having the spec available means adding
        it is a provider change rather than a prompt change — and it is the
        natural input to the agent loop in Increment 3.
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name(),
                    "description": tool.description(),
                    "parameters": tool.schema(),
                },
            }
            for tool in self.tools()
        ]

    def by_tier(self) -> Dict[str, List[str]]:
        """Tool names grouped by permission tier, for the settings panel."""
        grouped: Dict[str, List[str]] = {}
        for tool in self.tools():
            # Not `tool.permission`: the panel is the user's only view of what a
            # skill's tool can do, and it must show the tier that will actually
            # gate it.
            grouped.setdefault(
                self.effective_tier(tool.name()) or tool.permission, []
            ).append(tool.name())
        return {tier: sorted(names) for tier, names in sorted(grouped.items())}

    def floor_for(self, name: str) -> Optional[str]:
        """The recorded minimum tier for a name, if any. For callers and tests."""
        canonical = self.canonical(name) or (name or "").strip().lower()
        return self._floors.get(canonical)

    def effective_tier(self, name: str) -> str:
        """The tier a name will actually be gated at: declaration vs floor.

        Everything that *tells* someone about a tier — the settings panel, the
        model's tool list, the audit row — should read this instead of
        ``tool.permission``, or a lying tool gets to advertise itself as harmless.
        """
        tool = self.get(name)
        canonical = tool.name() if tool is not None else (name or "").strip().lower()
        declared = str(getattr(tool, "permission", "") or "") if tool else ""
        floor = self._floors.get(canonical, "")
        if _tier_rank(floor) > _tier_rank(declared):
            return floor
        return declared or floor

    def _apply_floor(
        self, assessment: RiskAssessment, name: str, floor: Optional[str]
    ) -> RiskAssessment:
        """Raise an assessment to the registry's recorded floor for this name."""
        if not floor or _tier_rank(floor) <= _tier_rank(assessment.permission):
            return assessment
        assessment.permission = floor
        if assessment.level is not RiskLevel.HIGH:
            assessment.level = RiskLevel.HIGH
            assessment.reasons = list(assessment.reasons) + [
                f"'{name}' may not run below {floor}-level"
            ]
        return assessment

    def assess(self, name: str, params: Optional[Dict[str, Any]] = None) -> RiskAssessment:
        """Risk assessment for a named tool, reconciled with its declared tier.

        A tool's ``permission`` attribute is the source of truth for its tier;
        the assessor's tables are the fallback for names it has never seen (a
        future tool, or a REST route).

        The two are reconciled by taking the **stricter** of the two, in either
        direction. Only honouring the declaration when it is stricter was a real
        bug: ``play_media`` declares ``confirm`` but appears in no table, so the
        assessor returned ``safe`` and the keystroke tool ran with no prompt.
        Equally, the tables can escalate a tool that under-declares itself.
        """
        tool = self.get(name)
        # The floor is looked up *before* the early return below, because clearing
        # `permission` is exactly how an untrusted tool would try to leave the
        # gate: an empty tier must not mean "no floor either".
        floor = self._floors.get(tool.name()) if tool is not None else None
        if tool is None or not tool.permission:
            return self._apply_floor(
                self.assessor.assess(name, params or {}),
                tool.name() if tool is not None else (name or "").strip().lower(),
                floor,
            )

        assessment = self.assessor.assess(tool.name(), tool.risk_params(params or {}))
        declared = tool.permission
        if _tier_rank(declared) > _tier_rank(assessment.permission):
            assessment.permission = declared
            # Promoting the tier must also promote the gate, or the dialog
            # would render "CONFIRMATION REQUIRED" and nothing would happen.
            if assessment.level is not RiskLevel.HIGH:
                assessment.level = RiskLevel.HIGH
                assessment.reasons = list(assessment.reasons) + [
                    f"'{tool.name()}' is declared {declared}-level"
                ]
        return self._apply_floor(assessment, tool.name(), floor)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    async def execute(
        self,
        name: str,
        params: Optional[Dict[str, Any]] = None,
        confirmation_manager=None,
        approval: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Validate, gate, then run. The only path to a tool's ``execute``.

        ``approval`` exists for callers that have *already* run their own gate
        — currently :class:`agents.tool_agent.ToolAgent`, which asks for
        approval itself so it can emit language-aware decline/timeout notices.
        Without it, a call the agent had already approved would be refused by
        this second gate, because the registry cannot see the first decision.

        Pass ``approval=True``/``False`` only when a human genuinely decided.
        Leave it ``None`` (the default) to have the gate run here.
        """
        params = params or {}
        tool = self.get(name)
        if tool is None:
            return {
                "success": False,
                "error": f"Unknown tool: {name}. Available: {', '.join(self.list())}",
            }

        # 1. Validation first. A malformed request should get a precise error
        #    rather than an approval prompt for something that could not have
        #    run anyway — and unconditional sandbox blocks live here too.
        try:
            tool.validate(params)
        except SecurityError as e:
            # The sandbox refused outright. `blocked` matters to the UI: it
            # tells the user that approving would not help, as opposed to a
            # high-risk action they could legitimately say yes to.
            logger.info("Tool %s blocked by the sandbox: %s", name, e)
            return {"success": False, "error": str(e), "blocked": True}
        except ToolValidationError as e:
            logger.info("Tool %s rejected: %s", name, e)
            return {"success": False, "error": str(e)}
        except Exception as e:  # a tool's own validate() misbehaving
            logger.warning("Tool %s validate() raised: %s", name, e)
            # Increment A: an unexpected exception may carry a path, an argv
            # or a token in its message. The client gets the type only.
            return {
                "success": False,
                "error": safe_error(
                    e, operation=f"{name}.validate", component="tools.registry"
                ),
                "data": {"exception": type(e).__name__},
            }

        # 2. The gate. Tier comes from the tool declaration, so the label the
        #    user sees is the label the executor enforces.
        assessment = self.assess(tool.name(), params)
        if assessment.level is RiskLevel.HIGH:
            if approval is False:
                return {
                    "success": False,
                    "error": "Cancelled: the action was not approved.",
                    "cancelled": True,
                }
            if approval is None:
                logger.info(
                    "Tool %s is %s-level and requires approval: %s",
                    tool.name(), assessment.permission, assessment.prompt_text(),
                )
                decision = await self._request_approval(
                    assessment, confirmation_manager, tool.name(), params
                )
                if decision is None:
                    return {
                        "success": False,
                        "requires_confirmation": True,
                        "level": assessment.level.value,
                        "permission": assessment.permission,
                        "reasons": assessment.reasons,
                        "summary": assessment.summary,
                        "error": f"{assessment.permission}-level action requires confirmation.",
                    }
                if not decision:
                    return {
                        "success": False,
                        "error": "Cancelled: the action was not approved.",
                        "cancelled": True,
                    }

        # 3. Run.
        try:
            result = await tool.execute(**params)
        except SecurityError as e:
            # A tool may discover a policy violation during execution rather
            # than validation (e.g. a path that only resolves to something
            # outside the root once symlinks are followed). Same treatment:
            # refused outright, not "ask the user".
            logger.info("Tool %s blocked during execution: %s", name, e)
            return {"success": False, "error": str(e), "blocked": True}
        except Exception as e:
            logger.exception("Tool %s raised during execution", name)
            # Increment A: never echo an arbitrary exception to the client.
            # `str(e)` here was the one place a traceback fragment, an absolute
            # path or a command line could reach the transcript.
            return {
                "success": False,
                "error": safe_error(
                    e, operation=name, component="tools.registry"
                ),
                "data": {"exception": type(e).__name__},
            }

        return self._render(result)

    @staticmethod
    async def _request_approval(
        assessment: RiskAssessment,
        confirmation_manager,
        action: str,
        params: Dict[str, Any],
    ) -> Optional[bool]:
        """Ask for approval. Returns the decision, or ``None`` if impossible.

        ``None`` means "no channel", which is distinct from "denied": the
        caller reports it differently, because a user who never saw a prompt
        needs different wording than one who said no.
        """
        if confirmation_manager is None:
            return None
        try:
            return bool(await confirmation_manager.request(action, params))
        except Exception as e:
            # Fail closed: an error in the approval channel is a denial.
            safe_error(
                e, operation="confirmation.request", component="tools.registry",
                detail=f"action={action}",
            )
            return False

    @staticmethod
    def _render(result: ToolResult) -> Dict[str, Any]:
        """Normalise a :class:`ToolResult` into the wire dict."""
        if not isinstance(result, ToolResult):
            # A tool returned something unexpected; do not trust it.
            return {"success": False, "error": f"Malformed tool result: {type(result).__name__}"}
        payload = result.to_dict()
        if not payload.get("success") and not payload.get("error"):
            payload["error"] = "Tool reported failure without a reason"
        return payload

    # ------------------------------------------------------------------
    # Batch
    # ------------------------------------------------------------------
    async def execute_many(
        self,
        calls: List[Dict[str, Any]],
        confirmation_manager=None,
    ) -> List[Dict[str, Any]]:
        """Run a list of ``{"name": ..., "params": {...}}`` in order.

        Sequential rather than concurrent on purpose: a batch of tool calls is
        usually a plan with dependencies (create a folder, then write into it),
        and an approval prompt in the middle of a concurrent batch has no
        meaningful ordering.
        """
        out: List[Dict[str, Any]] = []
        for call in calls or []:
            name = call.get("name") or call.get("action") or ""
            params = call.get("params") or call.get("args") or {}
            if not isinstance(params, dict):
                params = {"value": params}
            out.append(await self.execute(name, params, confirmation_manager))
        return out


#: Ordering used to reconcile a tool's declared tier with the assessor's.
_TIER_ORDER = {PERMISSION_SAFE: 0, PERMISSION_CONFIRM: 1, PERMISSION_DANGEROUS: 2}


def _tier_rank(tier: str) -> int:
    return _TIER_ORDER.get(tier, 0)


def build_default_registry(assessor: Optional[RiskAssessor] = None) -> ToolRegistry:
    """Construct a registry with every built-in tool registered.

    Import of the concrete tool modules is deferred into the function body so
    that importing :mod:`tools.registry` does not pull in the whole tool set —
    which matters for the sandbox tests, that import only primitives.
    """
    from tools import (
        applications,
        coding,
        fetch,
        filesystem,
        git,
        local_exec,
        media,
        processes,
        system,
        terminal,
        web,
    )

    registry = ToolRegistry(assessor=assessor)
    for module in (
        filesystem, terminal, local_exec, processes, applications, system,
        web, fetch, git, coding, media,
    ):
        registry.register_all(module.build_tools())
    logger.info("Tool registry ready with %d tools: %s", len(registry), registry.list())
    return registry
