"""Shared test isolation for module-level converter state."""

from __future__ import annotations

import pytest

import app.main as converter_main

# Gateway knobs: a developer shell that exports one must not point every test
# somewhere else.
_GATEWAY_ENV = (
    "CONVERTER_BIFROST_URL",
    "CONVERTER_MODELS_CACHE_TTL",
    "CONVERTER_MODELS_TIMEOUT",
    "CONVERTER_MODELS_STALE_MAX",
    "CONVERTER_SMG_PROVIDERS",
    "BIFROST_ADMIN_USERNAME",
    "BIFROST_ADMIN_PASSWORD",
    "CONVERTER_SMG_API_KEY",
    "CONVERTER_SMG_TIMEOUT",
    "CONVERTER_SMG_CACHE_TTL",
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
