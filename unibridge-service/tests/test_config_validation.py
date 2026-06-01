"""Tests for settings derivation and validate_settings."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def _restore_app_config():
    """Keep ``app.config.settings`` consistent across the process.

    Each test here calls ``importlib.reload(app.config)``, which rebinds
    ``app.config.settings`` to a brand-new ``Settings()`` object. Modules that
    captured the singleton at import time via ``from app.config import settings``
    (connection_manager, s3_manager, nas_manager, ...) keep the ORIGINAL object,
    so without a restore a later test that monkeypatches ``app.config.settings``
    would desync from what those managers actually read. Rebind the original
    settings object after each test so the singleton stays shared.
    """
    import app.config as cfg

    original_settings = cfg.settings
    yield
    cfg.settings = original_settings


def _fresh_settings(env: dict[str, str], monkeypatch):
    for key in (
        "HOST_IP",
        "KEYCLOAK_PORT",
        "KEYCLOAK_JWT_AUDIENCE",
        "KEYCLOAK_URL",
        "KEYCLOAK_REALM",
        "KEYCLOAK_ISSUER_URL",
        "KEYCLOAK_JWKS_URL",
        "CORS_ALLOWED_ORIGINS",
        "UNIBRIDGE_UI_PORT",
    ):
        monkeypatch.delenv(key, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    import app.config as cfg
    importlib.reload(cfg)
    return cfg


# ── URL derivation (_derive_urls) ─────────────────────────────────────────


def test_keycloak_urls_derived_when_audience_set(monkeypatch):
    cfg = _fresh_settings(
        {
            "HOST_IP": "host.example.com",
            "KEYCLOAK_PORT": "9443",
            "KEYCLOAK_REALM": "myrealm",
            "KEYCLOAK_JWT_AUDIENCE": "myaud",
        },
        monkeypatch,
    )
    s = cfg.settings
    assert s.KEYCLOAK_URL == "https://keycloak:8443"
    assert s.KEYCLOAK_ISSUER_URL == "https://host.example.com:9443/realms/myrealm"
    assert s.KEYCLOAK_JWKS_URL.startswith("https://keycloak:8443/realms/myrealm/")


def test_keycloak_urls_not_derived_when_audience_missing(monkeypatch):
    cfg = _fresh_settings({}, monkeypatch)
    s = cfg.settings
    assert s.KEYCLOAK_ISSUER_URL == ""
    assert s.KEYCLOAK_JWKS_URL == ""
    assert s.KEYCLOAK_URL == ""


def test_keycloak_urls_preserve_explicit_values(monkeypatch):
    cfg = _fresh_settings(
        {
            "KEYCLOAK_JWT_AUDIENCE": "aud",
            "KEYCLOAK_URL": "https://explicit-kc:8443",
            "KEYCLOAK_ISSUER_URL": "https://issuer.example/realms/x",
            "KEYCLOAK_JWKS_URL": "https://jwks.example/certs",
        },
        monkeypatch,
    )
    s = cfg.settings
    assert s.KEYCLOAK_URL == "https://explicit-kc:8443"
    assert s.KEYCLOAK_ISSUER_URL == "https://issuer.example/realms/x"
    assert s.KEYCLOAK_JWKS_URL == "https://jwks.example/certs"


def test_cors_default_from_host_and_ui_port(monkeypatch):
    cfg = _fresh_settings(
        {"HOST_IP": "192.168.1.10", "UNIBRIDGE_UI_PORT": "3210"}, monkeypatch
    )
    assert cfg.settings.CORS_ALLOWED_ORIGINS == "https://192.168.1.10:3210"


def test_cors_explicit_preserved(monkeypatch):
    cfg = _fresh_settings(
        {"CORS_ALLOWED_ORIGINS": "https://a.example,https://b.example"}, monkeypatch
    )
    assert cfg.settings.CORS_ALLOWED_ORIGINS == "https://a.example,https://b.example"


# ── validate_settings ─────────────────────────────────────────────────────


def test_validate_settings_rejects_insecure_encryption_key(monkeypatch):
    cfg = _fresh_settings({}, monkeypatch)
    cfg.settings.ENCRYPTION_KEY = "change-me-in-production"
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        cfg.validate_settings()


def test_validate_settings_rejects_empty_encryption_key(monkeypatch):
    cfg = _fresh_settings({}, monkeypatch)
    cfg.settings.ENCRYPTION_KEY = ""
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        cfg.validate_settings()


def test_validate_settings_accepts_hashed_encryption_key(monkeypatch):
    cfg = _fresh_settings({}, monkeypatch)
    cfg.settings.ENCRYPTION_KEY = "test-key-for-testing-only-32bytes!"
    cfg.settings.JWT_SECRET = "a-secure-jwt-secret-not-default"
    cfg.settings.APISIX_ADMIN_KEY = "apisix-key"
    cfg.validate_settings()


def test_validate_settings_accepts_valid_fernet_key(monkeypatch):
    from cryptography.fernet import Fernet
    cfg = _fresh_settings({}, monkeypatch)
    cfg.settings.ENCRYPTION_KEY = Fernet.generate_key().decode()
    cfg.settings.JWT_SECRET = "non-default-secret"
    cfg.settings.APISIX_ADMIN_KEY = "apisix-key"
    cfg.validate_settings()


def test_validate_settings_rejects_invalid_fernet_key(monkeypatch):
    cfg = _fresh_settings({}, monkeypatch)
    # 44 chars but not valid base64 fernet
    cfg.settings.ENCRYPTION_KEY = "X" * 44
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY is invalid"):
        cfg.validate_settings()


def test_validate_settings_requires_jwt_secret_in_dev_mode(monkeypatch):
    cfg = _fresh_settings({}, monkeypatch)
    cfg.settings.ENCRYPTION_KEY = "test-key-for-testing-only-32bytes!"
    cfg.settings.KEYCLOAK_ISSUER_URL = ""
    cfg.settings.JWT_SECRET = "change-me-to-a-secure-secret"
    cfg.settings.APISIX_ADMIN_KEY = "apisix-key"
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        cfg.validate_settings()


def test_validate_settings_allows_default_jwt_secret_when_keycloak_configured(monkeypatch):
    cfg = _fresh_settings({}, monkeypatch)
    cfg.settings.ENCRYPTION_KEY = "test-key-for-testing-only-32bytes!"
    cfg.settings.KEYCLOAK_ISSUER_URL = "https://kc.example/realms/x"
    cfg.settings.JWT_SECRET = ""  # default insecure; allowed when KC configured
    cfg.settings.APISIX_ADMIN_KEY = "apisix-key"
    cfg.validate_settings()


def test_validate_settings_requires_apisix_admin_key(monkeypatch):
    cfg = _fresh_settings({}, monkeypatch)
    cfg.settings.ENCRYPTION_KEY = "test-key-for-testing-only-32bytes!"
    cfg.settings.JWT_SECRET = "non-default"
    cfg.settings.APISIX_ADMIN_KEY = ""
    with pytest.raises(RuntimeError, match="APISIX_ADMIN_KEY"):
        cfg.validate_settings()
