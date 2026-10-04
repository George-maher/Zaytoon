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
