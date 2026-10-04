"""ToolAgent behaviour, asserted through the registry seam.

These tests used to call ``ToolAgent._calculator_tool``, ``._toolkit_file_op``,
``._search_tool`` and ``._toolkit_cmd`` directly. Those were private duplicates
of implementations that already lived in ``ToolExecutor``; when the two stacks
were collapsed into :class:`tools.registry.ToolRegistry` they were deleted.

The *properties* under test are unchanged and still matter — the calculator
must not evaluate arbitrary code, a path escape must be refused, a chained
command must be rejected — so they are now asserted through
``_execute_single_tool``, which is the same entry point the model reaches.
"""

import pytest

from agents.tool_agent import ToolAgent


class _Approve:
    async def request(self, action, params=None, session_id=""):
        return True


@pytest.fixture
def tool_agent():
    return ToolAgent(confirmation_manager=_Approve())


async def call(agent: ToolAgent, name: str, params: dict) -> str:
    """Invoke a tool the way the model path does."""
    return await agent._execute_single_tool(name, params)


# ---------------------------------------------------------------------------
# Calculator
# ---------------------------------------------------------------------------
async def test_safe_calculator_basic(tool_agent):
    assert "390" in await call(tool_agent, "calculator", {"expression": "15 * 23 + 45"})


async def test_safe_calculator_division(tool_agent):
    assert "25" in await call(tool_agent, "calculator", {"expression": "100 / 4"})


async def test_safe_calculator_power(tool_agent):
    assert "1024" in await call(tool_agent, "calculator", {"expression": "2 ** 10"})


async def test_safe_calculator_blocks_injection(tool_agent):
    # An AST allow-list admits only numeric operators, so a call expression
    # can never be reached.
    result = await call(
        tool_agent, "calculator", {"expression": "__import__('os').system('ls')"}
    )
    assert "Error" in result


async def test_empty_calculator(tool_agent):
    result = await call(tool_agent, "calculator", {"expression": ""})
    assert "Error" in result


# ---------------------------------------------------------------------------
# Filesystem containment
# ---------------------------------------------------------------------------
async def test_file_basename_security(tool_agent):
    result = await call(tool_agent, "file_read", {"path": "../../../etc/passwd"})
    assert "Blocked" in result or "Error" in result or "denied" in result.lower()


async def test_empty_file_read(tool_agent):
    assert "Error" in await call(tool_agent, "file_read", {"path": ""})


async def test_file_read_reports_missing_files_clearly(tool_agent):
    result = await call(tool_agent, "file_read", {"path": "definitely-not-here.txt"})
    assert "Not found" in result or "Error" in result


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
async def test_empty_cmd(tool_agent):
    assert "Error" in await call(tool_agent, "system_command", {"command": ""})


async def test_cmd_rejects_chaining(tool_agent):
    """Approval cannot authorise whatever follows a separator."""
    result = await call(
        tool_agent, "system_command", {"command": "echo hi & del C:\\x.txt"}
    )
    assert "Refusing" in result or "Blocked" in result
    assert "del" not in result.split("Refusing")[0] if "Refusing" in result else True


async def test_cmd_rejects_variable_expansion(tool_agent):
    result = await call(tool_agent, "system_command", {"command": "echo %PATH%"})
    assert "Error" in result or "Blocked" in result or "Refusing" in result


# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------
async def test_empty_search(tool_agent):
    assert "Error" in await call(tool_agent, "search_web", {"query": ""})


# ---------------------------------------------------------------------------
# The agent delegates rather than implementing
# ---------------------------------------------------------------------------
def test_agent_holds_no_tool_implementations():
    """Guards against the duplication coming back."""
    forbidden = [
        name for name in dir(ToolAgent)
        if name.startswith(("_app_control", "_run_code_tool", "_search_tool",
                            "_calculator_tool", "_toolkit_", "_type_text_tool",
                            "_key_press_tool", "_get_time_tool", "_send_keys"))
    ]
    assert forbidden == [], f"tool logic leaked back into ToolAgent: {forbidden}"


def test_agent_exposes_a_registry():
    from tools.registry import ToolRegistry

    assert isinstance(ToolAgent().registry, ToolRegistry)


def test_tool_catalogue_comes_from_the_registry():
    """The prompt lists real tools, not a hand-maintained subset."""
    from agents.tool_agent import TOOL_DESCRIPTION

    for name in ("file_read", "file_write", "create_folder", "git_status",
                 "create_project", "open_app", "system_command"):
        assert name in TOOL_DESCRIPTION, name


def test_catalogue_mentions_nothing_that_does_not_exist():
    from agents.tool_agent import TOOL_DESCRIPTION
    from tools.registry import build_default_registry

    listed = TOOL_DESCRIPTION.split("Available tools: ")[1].split(", ")
    known = set(build_default_registry().list())
    for name in listed:
        assert name in known, f"prompt advertises non-existent tool {name!r}"


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------
async def test_successful_results_are_cached(tool_agent):
    first = await call(tool_agent, "calculator", {"expression": "2 + 2"})
    second = await call(tool_agent, "calculator", {"expression": "2 + 2"})
    assert "4" in first
    assert second.startswith("[Cached]")


async def test_failures_are_not_cached(tool_agent):
    """A transient error must not be remembered as a result."""
    await call(tool_agent, "file_read", {"path": "missing-1.txt"})
    second = await call(tool_agent, "file_read", {"path": "missing-1.txt"})
    assert not second.startswith("[Cached]")
