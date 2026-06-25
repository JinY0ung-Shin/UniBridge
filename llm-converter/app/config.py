"""Runtime configuration for the LLM endpoint converter.

The converter sits behind APISIX (which already performed key-auth and injected
Bifrost attribution/governance headers) and forwards the translated request to
the upstream LLM gateway's ``/v1/chat/completions`` route. All settings are read
from the environment so they can be overridden in ``docker-compose.yml`` without
rebuilding the image.
"""

from __future__ import annotations

import os
import ssl

import httpx


def _get_llm_gateway_url() -> str:
    """Base URL of the upstream OpenAI-compatible LLM gateway (no trailing slash).

    Required. The converter targets ``{LLM_GATEWAY_URL}/v1/chat/completions``.
    ``LITELLM_URL`` is accepted as a deprecated compatibility alias.
    """
    raw = os.getenv("LLM_GATEWAY_URL", "").strip()
    if not raw:
        raw = os.getenv("LITELLM_URL", "").strip()
    if not raw:
        raise RuntimeError("LLM_GATEWAY_URL is required")
    return raw.rstrip("/")


def _get_tls_verify() -> bool | str | ssl.SSLContext:
    """TLS verification setting for the upstream httpx client.

    Resolution order:
    1. ``CONVERTER_TLS_CA`` (a CA bundle file path) — verify the chain against
       that CA with hostname checking DISABLED. This is useful for older
       LiteLLM deployments that served a self-signed cert bound to ``HOST_IP``
       while the converter dialed it by Docker service name.
    2. ``CONVERTER_TLS_VERIFY`` — ``true``/``1``/``yes``/``on`` → verify with
       system CAs; any other non-empty non-boolean value → treat as a CA bundle
       path (with hostname checking ON).
    3. Default (both unset) → ``False`` (no verification). Trusted private
       network only.
    """
    ca = os.getenv("CONVERTER_TLS_CA", "").strip()
    if ca:
        ctx = ssl.create_default_context(cafile=ca)
        # Some legacy gateway certs use HOST_IP rather than the Docker service name.
        ctx.check_hostname = False
        return ctx

    raw = os.getenv("CONVERTER_TLS_VERIFY")
    if raw is None:
        return False
    stripped = raw.strip()
    if not stripped:
        return False
    if stripped.lower() in {"false", "0", "no", "off"}:
        return False
    if stripped.lower() in {"true", "1", "yes", "on"}:
        return True
    return stripped


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"true", "1", "yes", "on"}


def _get_timeout() -> httpx.Timeout:
    """httpx timeout for the upstream client.

    ``connect``/``write``/``pool`` are always bounded so an unreachable or
    wedged upstream fails fast instead of pinning a worker forever. ``read`` is
    left unbounded by default (``CONVERTER_REQUEST_TIMEOUT`` <= 0) because LLM
    completions — and especially SSE streams — are legitimately long-lived; set
    ``CONVERTER_REQUEST_TIMEOUT`` to a positive integer to cap it.
    """
    read_raw = _int_env("CONVERTER_REQUEST_TIMEOUT", 0)
    read = None if read_raw <= 0 else float(read_raw)
    return httpx.Timeout(
        read,
        connect=float(_int_env("CONVERTER_CONNECT_TIMEOUT", 10)),
        write=float(_int_env("CONVERTER_WRITE_TIMEOUT", 120)),
        pool=float(_int_env("CONVERTER_POOL_TIMEOUT", 10)),
    )


class _Settings:
    """Lazy env-backed settings; properties re-read so tests can monkeypatch."""

    @property
    def LLM_GATEWAY_URL(self) -> str:
        return _get_llm_gateway_url()

    @property
    def LITELLM_URL(self) -> str:
        return self.LLM_GATEWAY_URL

    @property
    def tls_verify(self) -> bool | str | ssl.SSLContext:
        return _get_tls_verify()

    @property
    def request_timeout(self) -> httpx.Timeout:
        return _get_timeout()

    @property
    def nonstream_timeout(self) -> float | None:
        """Total wall-clock deadline (seconds) for a non-streaming upstream
        request — sending the body, waiting for generation, and reading the full
        response. The httpx ``read`` timeout cannot bound this safely: a
        non-streaming completion's body arrives atomically only after generation
        finishes, so a per-chunk read timeout tight enough to catch a stall would
        also cut off legitimately slow completions. This is a generous *total*
        ceiling instead, so a gateway that accepts the connection and then stalls
        (or trickles) the body cannot pin a worker forever. Default 600s; set
        ``CONVERTER_NONSTREAM_TIMEOUT`` <= 0 to disable (restore unbounded)."""
        raw = _int_env("CONVERTER_NONSTREAM_TIMEOUT", 600)
        return None if raw <= 0 else float(raw)

    @property
    def response_store_ttl(self) -> float:
        """TTL (seconds) for the in-memory previous_response_id conversation store."""
        return float(_int_env("CONVERTER_RESPONSE_STORE_TTL", 3600))

    @property
    def response_store_max(self) -> int:
        """Max stored conversations before LRU eviction."""
        return _int_env("CONVERTER_RESPONSE_STORE_MAX", 10000)

    @property
    def response_store_max_bytes(self) -> int:
        """Total approx-serialized byte budget for stored transcripts before LRU
        eviction (0 disables). Safety net for image-heavy / many concurrent
        chains, which the entry-count cap alone does not bound. Default 256 MiB."""
        return _int_env("CONVERTER_RESPONSE_STORE_MAX_BYTES", 256 * 1024 * 1024)

    @property
    def emit_reasoning(self) -> bool:
        """Whether to surface upstream ``reasoning_content`` as Responses reasoning items."""
        return _bool_env("CONVERTER_EMIT_REASONING", True)

    @property
    def trace(self) -> bool:
        """Opt-in verbose tracing (``CONVERTER_TRACE``). When on, ``/v1/messages``
        logs the full incoming Anthropic request body (system/tools/messages) and
        every decisive upstream chunk (``finish_reason`` + ``tool_calls`` presence)
        at INFO. Built to diff two clients hitting the SAME model — e.g. why a
        request from client A yields parseable tool calls from vLLM while client
        B's gets a plain-text ``finish_reason: stop``. Off by default (noisy)."""
        return _bool_env("CONVERTER_TRACE", False)

    @property
    def sse_heartbeat_seconds(self) -> float:
        """Idle interval after which a streaming response emits an SSE comment
        (``: ping``) to keep the connection's byte flow alive. LLM streams can be
        silent past a proxy's read timeout (nginx/APISIX, LBs) during long TTFT or
        reasoning; the heartbeat stops those intermediaries from dropping the
        socket. <= 0 disables it. Default 15s."""
        return float(_int_env("CONVERTER_SSE_HEARTBEAT_SECONDS", 15))


settings = _Settings()
