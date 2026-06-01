"""Shared config and fixtures for the live-deployment E2E tests.

These tests run against a RUNNING UniBridge deployment (UI nginx → APISIX →
llm-converter → LiteLLM), not against unit fixtures. They are skipped unless
``LLM_API_KEY`` is set, so they never run in the normal unit-test sweep.

Configure via environment:
  LLM_BASE_URL       Gateway base incl. the /api/llm prefix.
                     Default: https://localhost:3000/api/llm
  LLM_API_KEY        APISIX consumer key (required to run these tests).
  LLM_API_KEY_HEADER Header carrying the key. Default: apikey (APISIX key-auth).
  LLM_MODEL          Model id to target. If unset, discovered from /v1/models.
  LLM_TLS_VERIFY     true | false | <ca-path>. Default: false (self-signed UI cert).
  LLM_TIMEOUT        Per-request timeout seconds. Default: 60.

These may also be set in an ``e2e/.env`` file (see ``.env.example``); it is
loaded automatically at collection time. Real environment variables always take
precedence over values in ``.env``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is optional
    load_dotenv = None

# Load e2e/.env (next to this file) before reading any config below. Existing
# environment variables win over .env entries (override=False).
if load_dotenv is not None:
    load_dotenv(Path(__file__).with_name(".env"), override=False)

BASE_URL = os.getenv("LLM_BASE_URL", "https://localhost:3000/api/llm").rstrip("/")
API_KEY = os.getenv("LLM_API_KEY", "")
API_KEY_HEADER = os.getenv("LLM_API_KEY_HEADER", "apikey")
MODEL = os.getenv("LLM_MODEL", "")
TIMEOUT = float(os.getenv("LLM_TIMEOUT", "60"))
# Anthropic Messages requires max_tokens. Reasoning models spend tokens on a
# ``thinking`` block before any answer text, so a tight cap can truncate the
# whole budget into reasoning and leave no text. Default generously; override
# with LLM_MAX_TOKENS for slow/expensive models.
MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))


def _tls_verify():
    raw = os.getenv("LLM_TLS_VERIFY", "false")
    low = raw.strip().lower()
    if low in ("", "false", "0", "no", "off"):
        return False
    if low in ("true", "1", "yes", "on"):
        return True
    return raw  # treat as CA bundle path


TLS_VERIFY = _tls_verify()

# Applied to every test in this directory: skip the whole suite unless a live
# deployment was configured.
requires_deployment = pytest.mark.skipif(
    not API_KEY,
    reason="live E2E: set LLM_API_KEY (and LLM_BASE_URL) to run against a deployment",
)


@pytest.fixture(scope="session")
def auth_headers() -> dict:
    return {API_KEY_HEADER: API_KEY, "content-type": "application/json"}


@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=BASE_URL, verify=TLS_VERIFY, timeout=TIMEOUT) as c:
        yield c


@pytest.fixture(scope="session")
def model(client, auth_headers) -> str:
    """The model id to use — explicit LLM_MODEL, else discovered via /v1/models."""
    if MODEL:
        return MODEL
    try:
        resp = client.get("/v1/models", headers=auth_headers)
        resp.raise_for_status()
        data = resp.json().get("data") or []
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"LLM_MODEL unset and /v1/models discovery failed: {exc}")
    if not data:
        pytest.skip("LLM_MODEL unset and /v1/models returned no models")
    return data[0]["id"]


def read_sse(client: httpx.Client, path: str, headers: dict, body: dict) -> list[tuple[str, dict]]:
    """POST a streaming request and return a list of (event_type, data) tuples.

    Handles both Anthropic-style (``event:`` + ``data:``) and OpenAI/Responses
    SSE; the ``[DONE]`` sentinel is skipped. ``event_type`` falls back to the
    JSON payload's ``type`` field when no ``event:`` line is present.
    """
    events: list[tuple[str, dict]] = []
    current_event: str | None = None
    with client.stream("POST", path, headers=headers, json=body) as resp:
        resp.raise_for_status()
        for raw in resp.iter_lines():
            line = raw.rstrip("\r") if isinstance(raw, str) else raw.decode().rstrip("\r")
            if line == "":
                current_event = None
                continue
            if line.startswith("event:"):
                current_event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    continue
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                etype = current_event or (data.get("type") if isinstance(data, dict) else None)
                events.append((etype, data))
    return events
