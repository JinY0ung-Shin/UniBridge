"""Shared test isolation for module-level converter state."""

from __future__ import annotations

import pytest

import app.main as converter_main

# Knobs added for the Bifrost gateway: a developer shell that exports one must
# not flip every test into another mode.
_GATEWAY_ENV = (
    "LLM_GATEWAY",
    "CONVERTER_BIFROST_URL",
    "CONVERTER_MODELS_CACHE_TTL",
    "CONVERTER_MODELS_TIMEOUT",
    "CONVERTER_MODELS_STALE_MAX",
)


@pytest.fixture(autouse=True)
def _isolate_converter_state(monkeypatch):
    for name in _GATEWAY_ENV:
        monkeypatch.delenv(name, raising=False)
    # The model listing cache is process-wide by design; one test's listing must
    # not answer the next test's request.
    converter_main.reset_models_cache()
    yield
    converter_main.reset_models_cache()
