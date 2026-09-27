import pytest
from agents.tool_agent import ToolAgent


@pytest.fixture
def tool_agent():
    return ToolAgent()


def test_safe_calculator_basic(tool_agent):
    result = tool_agent._calculator_tool("15 * 23 + 45")
    assert "390" in result


def test_safe_calculator_division(tool_agent):
    result = tool_agent._calculator_tool("100 / 4")
    assert "25" in result


def test_safe_calculator_power(tool_agent):
    result = tool_agent._calculator_tool("2 ** 10")
    assert "1024" in result


def test_safe_calculator_blocks_injection(tool_agent):
    result = tool_agent._calculator_tool("__import__('os').system('ls')")
    assert "Error" in result


def test_file_basename_security(tool_agent):
    import asyncio
    result = asyncio.run(tool_agent._toolkit_file_op("../../../etc/passwd"))
    assert "Error" in result or "Access denied" in result or "not found" in result


def test_empty_search(tool_agent):
    import asyncio
    result = asyncio.run(tool_agent._search_tool(""))
    assert "Error" in result


def test_empty_calculator(tool_agent):
    result = tool_agent._calculator_tool("")
    assert "Error" in result


def test_empty_file_read(tool_agent):
    import asyncio
    result = asyncio.run(tool_agent._toolkit_file_op(""))
    assert "Error" in result


def test_empty_cmd(tool_agent):
    import asyncio
    result = asyncio.run(tool_agent._toolkit_cmd(""))
    assert "Error" in result


def test_cmd_rejects_unknown(tool_agent):
    import asyncio
    result = asyncio.run(tool_agent._toolkit_cmd("rm -rf /"))
    assert "not allowed" in result.lower()
