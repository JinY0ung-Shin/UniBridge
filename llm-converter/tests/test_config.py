"""Boundary tests for the converter's environment-backed configuration."""

from __future__ import annotations

import pytest

from app import config


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "claude/"),  # unset keeps the default
        ("", ""),  # explicitly empty disables aliasing and stripping
        ("   ", ""),  # whitespace-only is empty too
        (" anthropic/ ", "anthropic/"),
    ],
)
def test_model_alias_prefix_env_modes(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("CONVERTER_MODEL_ALIAS_PREFIX", raising=False)
    else:
        monkeypatch.setenv("CONVERTER_MODEL_ALIAS_PREFIX", raw)

    assert config.settings.model_alias_prefix == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, frozenset({"low", "medium", "high"})),  # unset keeps the default
        ("", frozenset({"low", "medium", "high"})),  # blank too
        (",  ,", frozenset({"low", "medium", "high"})),  # nothing survives parsing
        ("*", None),  # passthrough: forward the client's value verbatim
        ("low, HIGH ,,", frozenset({"low", "high"})),
    ],
)
def test_reasoning_effort_levels_env_modes(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("CONVERTER_REASONING_EFFORT_LEVELS", raising=False)
    else:
        monkeypatch.setenv("CONVERTER_REASONING_EFFORT_LEVELS", raw)

    assert config.settings.reasoning_effort_levels == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "auto"),  # unset keeps header-based detection
        ("", "auto"),
        ("auto", "auto"),
        ("maybe", "auto"),  # unrecognized falls back rather than failing requests
        ("true", "true"),
        (" ON ", "true"),
        ("1", "true"),
        ("false", "false"),
        ("off", "false"),
        ("0", "false"),
    ],
)
def test_length_as_completed_env_modes(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("CONVERTER_LENGTH_AS_COMPLETED", raising=False)
    else:
        monkeypatch.setenv("CONVERTER_LENGTH_AS_COMPLETED", raw)

    assert config.settings.length_as_completed == expected


def test_upstream_url_is_bifrost(monkeypatch):
    assert config.settings.upstream_url == "http://bifrost:8080"

    monkeypatch.setenv("CONVERTER_BIFROST_URL", "  http://bifrost.test:9000// ")
    assert config.settings.upstream_url == "http://bifrost.test:9000"


@pytest.mark.parametrize("raw", ["bifrost", "litellm", ""])
def test_a_leftover_llm_gateway_is_ignored(monkeypatch, raw):
    """Compose still passes ``LLM_GATEWAY=bifrost`` for one release (so an older
    image recreated on rollback does not default to LiteLLM); whatever it says,
    the converter forwards to Bifrost and needs no ``LITELLM_URL``."""
    monkeypatch.setenv("LLM_GATEWAY", raw)
    monkeypatch.delenv("LITELLM_URL", raising=False)

    assert config.settings.upstream_url == "http://bifrost:8080"
    assert config.settings.models_timeout == 2.0


def test_model_listing_cache_knobs(monkeypatch):
    assert config.settings.models_cache_ttl == 30.0
    assert config.settings.models_stale_max == 600.0

    monkeypatch.setenv("CONVERTER_MODELS_CACHE_TTL", "0")
    monkeypatch.setenv("CONVERTER_MODELS_STALE_MAX", "not-a-number")
    assert config.settings.models_cache_ttl == 0.0
    assert config.settings.models_stale_max == 600.0  # unparseable falls back


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Unset: Bifrost answers inside Claude Code's ~3s discovery budget.
        (None, 2.0),
        ("", 2.0),
        ("  ", 2.0),
        ("1.5", 1.5),
        ("0", 0.0),
        ("soon", 2.0),  # unparseable falls back to the default
    ],
)
def test_model_listing_timeout(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("CONVERTER_MODELS_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("CONVERTER_MODELS_TIMEOUT", raw)

    assert config.settings.models_timeout == expected
