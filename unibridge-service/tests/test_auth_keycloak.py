"""Tests for Keycloak RS256/JWKS authentication."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jwt.algorithms import RSAAlgorithm
from pytest_httpx import HTTPXMock

import app.auth as auth
from app.auth import _verify_keycloak_token, get_current_user
from app.config import settings


ISSUER = "https://keycloak.example.test/realms/unibridge"
AUDIENCE = "unibridge-api"
JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"


@pytest.fixture
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture
def make_jwks_key(rsa_keypair):
    _private_key, public_key = rsa_keypair

    def _make(kid: str) -> dict[str, Any]:
        jwk = RSAAlgorithm.to_jwk(public_key, as_dict=True)
        jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
        return jwk

    return _make


@pytest.fixture
def make_token(rsa_keypair):
    private_key, _public_key = rsa_keypair

    def _make(
        *,
        kid: str = "kid-1",
        issuer: str = ISSUER,
        audience: str = AUDIENCE,
        expires_delta: timedelta = timedelta(minutes=5),
        roles: list[str] | str | None = None,
        algorithm: str = "RS256",
    ) -> str:
        payload: dict[str, Any] = {
            "sub": "keycloak-user-id",
            "preferred_username": "alice",
            "iss": issuer,
            "aud": audience,
            "exp": datetime.now(timezone.utc) + expires_delta,
        }
        if roles is None:
            payload["realm_access"] = {"roles": ["developer"]}
        else:
            payload["roles"] = roles

        key: Any = private_key
        if algorithm == "HS256":
            key = "not-the-rsa-public-key-but-long-enough"

        return jwt.encode(payload, key, algorithm=algorithm, headers={"kid": kid})

    return _make


@pytest.fixture(autouse=True)
def keycloak_settings(monkeypatch):
    monkeypatch.setattr(settings, "KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setattr(settings, "KEYCLOAK_JWKS_URL", JWKS_URL)
    monkeypatch.setattr(settings, "KEYCLOAK_JWT_AUDIENCE", AUDIENCE)
    monkeypatch.setattr(settings, "SSL_CA_CERT_PATH", "")
    monkeypatch.setattr(settings, "SSL_VERIFY", True)
    monkeypatch.setattr(auth, "_jwks_cache", None)
    monkeypatch.setattr(auth, "_jwks_cache_ts", 0.0)
    yield
    auth._jwks_cache = None
    auth._jwks_cache_ts = 0.0


def _credentials(token: str):
    return type("Creds", (), {"credentials": token})()


@pytest.mark.asyncio
async def test_rs256_token_with_matching_kid_is_accepted(
    httpx_mock: HTTPXMock,
    make_jwks_key,
    make_token,
):
    httpx_mock.add_response(url=JWKS_URL, json={"keys": [make_jwks_key("kid-1")]})

    user = await _verify_keycloak_token(make_token(kid="kid-1", roles=["viewer", "developer"]))

    assert user.username == "alice"
    assert user.role == "developer"


@pytest.mark.asyncio
async def test_missing_kid_forces_jwks_refresh_and_accepts_rotated_key(
    httpx_mock: HTTPXMock,
    make_jwks_key,
    make_token,
):
    httpx_mock.add_response(url=JWKS_URL, json={"keys": [make_jwks_key("old-kid")]})
    httpx_mock.add_response(url=JWKS_URL, json={"keys": [make_jwks_key("new-kid")]})

    user = await _verify_keycloak_token(make_token(kid="new-kid"))

    assert user.username == "alice"
    assert user.role == "developer"
    assert len(httpx_mock.get_requests(url=JWKS_URL)) == 2


@pytest.mark.asyncio
async def test_expired_keycloak_token_is_rejected(
    httpx_mock: HTTPXMock,
    make_jwks_key,
    make_token,
):
    httpx_mock.add_response(url=JWKS_URL, json={"keys": [make_jwks_key("kid-1")]})

    with pytest.raises(HTTPException) as exc_info:
        await _verify_keycloak_token(
            make_token(kid="kid-1", expires_delta=timedelta(seconds=-1))
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid or expired token"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("issuer", "audience"),
    [
        ("https://issuer.example.invalid", AUDIENCE),
        (ISSUER, "wrong-audience"),
    ],
)
async def test_wrong_issuer_or_audience_is_rejected(
    httpx_mock: HTTPXMock,
    make_jwks_key,
    make_token,
    issuer,
    audience,
):
    httpx_mock.add_response(url=JWKS_URL, json={"keys": [make_jwks_key("kid-1")]})

    with pytest.raises(HTTPException) as exc_info:
        await _verify_keycloak_token(
            make_token(kid="kid-1", issuer=issuer, audience=audience)
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid or expired token"


@pytest.mark.asyncio
async def test_hs256_token_is_rejected_in_keycloak_mode(
    httpx_mock: HTTPXMock,
    make_jwks_key,
    make_token,
):
    httpx_mock.add_response(url=JWKS_URL, json={"keys": [make_jwks_key("kid-1")]})

    with pytest.raises(HTTPException) as exc_info:
        await _verify_keycloak_token(make_token(kid="kid-1", algorithm="HS256"))

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid or expired token"


@pytest.mark.asyncio
async def test_none_algorithm_token_is_rejected_in_keycloak_mode(
    httpx_mock: HTTPXMock,
    make_jwks_key,
):
    payload = {
        "sub": "keycloak-user-id",
        "preferred_username": "alice",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        "realm_access": {"roles": ["developer"]},
    }
    token = jwt.encode(payload, key="", algorithm="none", headers={"kid": "kid-1"})
    httpx_mock.add_response(url=JWKS_URL, json={"keys": [make_jwks_key("kid-1")]})

    with pytest.raises(HTTPException) as exc_info:
        await _verify_keycloak_token(token)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid or expired token"


@pytest.mark.asyncio
async def test_jwks_fetch_failure_uses_stale_cache_once_available(
    httpx_mock: HTTPXMock,
    make_jwks_key,
    make_token,
    monkeypatch,
):
    monkeypatch.setattr(auth, "_JWKS_CACHE_TTL", 0.01)
    httpx_mock.add_response(url=JWKS_URL, json={"keys": [make_jwks_key("kid-1")]})

    first_user = await _verify_keycloak_token(make_token(kid="kid-1"))
    assert first_user.username == "alice"

    await asyncio.sleep(0.02)
    httpx_mock.add_response(url=JWKS_URL, status_code=503)

    second_user = await _verify_keycloak_token(make_token(kid="kid-1"))

    assert second_user.username == "alice"
    assert len(httpx_mock.get_requests(url=JWKS_URL)) == 2


@pytest.mark.asyncio
async def test_jwks_fetch_failure_without_cache_returns_502(
    httpx_mock: HTTPXMock,
    make_token,
):
    httpx_mock.add_response(url=JWKS_URL, status_code=503)

    with pytest.raises(HTTPException) as exc_info:
        await _verify_keycloak_token(make_token(kid="kid-1"))

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "Auth service unavailable"


@pytest.mark.asyncio
async def test_concurrent_verification_fetches_jwks_once(
    httpx_mock: HTTPXMock,
    make_jwks_key,
    make_token,
):
    httpx_mock.add_response(url=JWKS_URL, json={"keys": [make_jwks_key("kid-1")]})
    token = make_token(kid="kid-1")

    users = await asyncio.gather(*(_verify_keycloak_token(token) for _ in range(50)))

    assert {user.username for user in users} == {"alice"}
    assert {user.role for user in users} == {"developer"}
    assert len(httpx_mock.get_requests(url=JWKS_URL)) == 1


@pytest.mark.asyncio
async def test_get_current_user_uses_keycloak_when_issuer_configured(
    httpx_mock: HTTPXMock,
    make_jwks_key,
    make_token,
):
    httpx_mock.add_response(url=JWKS_URL, json={"keys": [make_jwks_key("kid-1")]})

    user = await get_current_user(credentials=_credentials(make_token(kid="kid-1")))

    assert user.username == "alice"
    assert user.role == "developer"
