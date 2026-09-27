import pytest
import os
import tempfile
from agents.memory_agent import MemoryAgent


@pytest.fixture
async def memory_agent():
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    os.environ['DATABASE_URL'] = f'sqlite:///{db_path}'
    agent = MemoryAgent()
    yield agent
    # The aiosqlite connection must be closed before the file is removed:
    # on Windows an open handle makes unlink() fail with WinError 32.
    await agent.close()
    try:
        os.unlink(db_path)
    except OSError:
        pass  # best effort; the OS will reap the temp file
    os.environ.pop('DATABASE_URL', None)


@pytest.mark.asyncio
async def test_store_and_retrieve(memory_agent):
    await memory_agent.store_message("user", "Hello")
    context = await memory_agent.get_recent_context(limit=1)
    assert len(context) == 1
    assert context[0]['role'] == 'user'
    assert context[0]['content'] == 'Hello'


@pytest.mark.asyncio
async def test_add_reminder(memory_agent):
    result = await memory_agent.add_reminder("Test reminder", "2026-12-31 23:59:59")
    assert 'id' in result
    assert result['title'] == "Test reminder"


@pytest.mark.asyncio
async def test_invalid_reminder_time(memory_agent):
    result = await memory_agent.add_reminder("Test", "invalid-time")
    assert 'error' in result


@pytest.mark.asyncio
async def test_due_reminders(memory_agent):
    await memory_agent.add_reminder("Past reminder", "2020-01-01 00:00:00")
    due = await memory_agent.get_due_reminders()
    assert len(due) == 1
    assert due[0]['title'] == "Past reminder"


@pytest.mark.asyncio
async def test_clear_memory(memory_agent):
    await memory_agent.store_message("user", "Hello")
    await memory_agent.clear_memory()
    context = await memory_agent.get_recent_context(limit=10)
    assert len(context) == 0


@pytest.mark.asyncio
async def test_timezone_reminder(memory_agent):
    result = await memory_agent.add_reminder("Test", "2026-12-31 23:59:59", timezone_offset=3)
    assert 'utc_time' in result
    assert result['utc_time'] == "2026-12-31 20:59:59"
