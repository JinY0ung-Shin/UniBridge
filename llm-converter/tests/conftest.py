"""Shared test isolation for module-level converter state."""

from __future__ import annotations

import pytest

import app.main as converter_main
from tests.helpers import mock_upstream

_REAL_MAKE_CLIENT = converter_main._make_client


@pytest.fixture(autouse=True)
def _client_factory_restored():
    """Fail the test that leaves a fake upstream client factory behind.

    Defined before every fixture that uses ``monkeypatch``, so this check runs
    after monkeypatch has undone its patches: only a factory assigned past
    monkeypatch is still there, and it would answer the next test's requests.
    """
    yield
    left_behind = converter_main._make_client
    converter_main._make_client = _REAL_MAKE_CLIENT
    assert left_behind is _REAL_MAKE_CLIENT, (
        "app.main._make_client was replaced without monkeypatch; use tests.helpers.mock_upstream"
    )


@pytest.fixture
def make_app(monkeypatch):
    """``make_app(handler)`` → the converter app, its upstream at
    ``http://upstream.test`` and every upstream request answered by *handler*."""
    monkeypatch.setenv("CONVERTER_BIFROST_URL", "http://upstream.test")

    def install(handler):
        mock_upstream(monkeypatch, handler)
        return converter_main.app

    return install


@pytest.fixture
def clock(monkeypatch):
    """The converter's monotonic clock, frozen: ``clock[0] += 31`` moves it on."""
    now = [10_000.0]
    monkeypatch.setattr(converter_main, "_clock", lambda: now[0])
    return now


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
