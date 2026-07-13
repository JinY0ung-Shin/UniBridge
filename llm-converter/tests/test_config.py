"""Boundary tests for the converter's environment-backed configuration."""

from __future__ import annotations

import ssl

import httpx
import pytest

from app import config


def test_litellm_url_is_required_and_normalized(monkeypatch):
    monkeypatch.delenv("LITELLM_URL", raising=False)
    with pytest.raises(RuntimeError, match="LITELLM_URL is required"):
        config._get_litellm_url()

    monkeypatch.setenv("LITELLM_URL", "  https://litellm.test///  ")
    assert config._get_litellm_url() == "https://litellm.test"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, False),
        ("", False),
        (" off ", False),
        ("YES", True),
        ("/tmp/custom-ca.pem", "/tmp/custom-ca.pem"),
    ],
)
def test_tls_verify_env_modes(monkeypatch, raw, expected):
    monkeypatch.delenv("CONVERTER_TLS_CA", raising=False)
    if raw is None:
        monkeypatch.delenv("CONVERTER_TLS_VERIFY", raising=False)
    else:
        monkeypatch.setenv("CONVERTER_TLS_VERIFY", raw)

    assert config._get_tls_verify() == expected


def test_explicit_tls_ca_builds_context_without_hostname_check(monkeypatch):
    class DummyContext:
        check_hostname = True

    context = DummyContext()
    seen = {}

    def fake_create_default_context(*, cafile):
        seen["cafile"] = cafile
        return context

    monkeypatch.setenv("CONVERTER_TLS_CA", "/run/secrets/litellm-ca.pem")
    monkeypatch.setattr(ssl, "create_default_context", fake_create_default_context)

    assert config.settings.tls_verify is context
    assert seen == {"cafile": "/run/secrets/litellm-ca.pem"}
    assert context.check_hostname is False


def test_invalid_integer_falls_back_and_boolean_defaults(monkeypatch):
    monkeypatch.setenv("BROKEN_INT", "not-an-int")
    assert config._int_env("BROKEN_INT", 17) == 17

    monkeypatch.delenv("FLAG", raising=False)
    assert config._bool_env("FLAG", True) is True
    monkeypatch.setenv("FLAG", "   ")
    assert config._bool_env("FLAG", False) is False
    monkeypatch.setenv("FLAG", "on")
    assert config._bool_env("FLAG", False) is True


def test_timeout_settings_bound_transport_but_allow_unbounded_read(monkeypatch):
    monkeypatch.setenv("CONVERTER_REQUEST_TIMEOUT", "0")
    monkeypatch.setenv("CONVERTER_CONNECT_TIMEOUT", "3")
    monkeypatch.setenv("CONVERTER_WRITE_TIMEOUT", "4")
    monkeypatch.setenv("CONVERTER_POOL_TIMEOUT", "5")
    timeout = config.settings.request_timeout
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read is None
    assert timeout.connect == 3.0
    assert timeout.write == 4.0
    assert timeout.pool == 5.0

    monkeypatch.setenv("CONVERTER_REQUEST_TIMEOUT", "9")
    assert config.settings.request_timeout.read == 9.0


def test_lazy_settings_cover_disabled_deadlines_and_optional_values(monkeypatch):
    monkeypatch.setenv("CONVERTER_NONSTREAM_TIMEOUT", "0")
    monkeypatch.setenv("CONVERTER_RESPONSE_STORE_TTL", "12")
    monkeypatch.setenv("CONVERTER_RESPONSE_STORE_MAX", "13")
    monkeypatch.setenv("CONVERTER_RESPONSE_STORE_MAX_BYTES", "14")
    monkeypatch.setenv("CONVERTER_RESPONSE_STORE_MAX_ENTRY_BYTES", "15")
    monkeypatch.setenv("CONVERTER_RESPONSE_STORE_PATH", " /tmp/store.sqlite ")
    monkeypatch.setenv("CONVERTER_EMIT_REASONING", "false")
    monkeypatch.setenv("CONVERTER_TRACE", "true")
    monkeypatch.setenv("CONVERTER_SSE_HEARTBEAT_SECONDS", "7")

    assert config.settings.nonstream_timeout is None
    assert config.settings.response_store_ttl == 12.0
    assert config.settings.response_store_max == 13
    assert config.settings.response_store_max_bytes == 14
    assert config.settings.response_store_max_entry_bytes == 15
    assert config.settings.response_store_path == "/tmp/store.sqlite"
    assert config.settings.emit_reasoning is False
    assert config.settings.trace is True
    assert config.settings.sse_heartbeat_seconds == 7.0
