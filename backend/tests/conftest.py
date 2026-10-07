"""Shared test fixtures.

The suite predates multi-provider support: the routing, free-model and
contract tests all exercise the *OpenRouter* chain, so they pin the provider
explicitly rather than depending on the ambient default (Ollama). Tests that
cover the local path opt in by setting ``config.PROVIDER = "ollama"``
themselves (see ``test_ollama_provider.py``).
"""

import pytest

from config import PROVIDER_OPENROUTER, config


@pytest.fixture(autouse=True)
def _openrouter_provider(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", PROVIDER_OPENROUTER)
    yield


@pytest.fixture(autouse=True)
def _isolated_core_db(monkeypatch, tmp_path):
    """Point the core database at a fresh temp file for every test.

    The LLM client consults the provider registry (``core.db``) before it
    falls back to the configured chain, so a registry populated on a
    developer's machine would steer — and a stale row could break — tests
    that are about the chain itself. Isolation here is what keeps
    "which model answered?" a question the test controls.

    Deeper fixtures override this: ``tests/core``'s ``temp_core_db`` sets the
    same variable later (conftest depth orders parent before child), so the
    core suite keeps its own explicit database.
    """
    monkeypatch.setenv("CORE_DB_PATH", str(tmp_path / "core.db"))
