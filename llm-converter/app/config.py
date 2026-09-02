"""Runtime configuration for the LLM endpoint converter.

The converter sits behind APISIX (which already performed key-auth and injected
the LiteLLM master key + ``x-litellm-end-user-id`` header) and forwards the
translated request to the upstream LiteLLM proxy's ``/v1/chat/completions``
route. All settings are read from the environment so they can be overridden in
``docker-compose.yml`` without rebuilding the image.
"""

from __future__ import annotations

import os
import re
import ssl

import httpx


def _get_litellm_url() -> str:
    """Base URL of the upstream LiteLLM proxy (no trailing slash).

    Required. The converter targets ``{LITELLM_URL}/v1/chat/completions``.
    """
    raw = os.getenv("LITELLM_URL", "").strip()
    if not raw:
        raise RuntimeError("LITELLM_URL is required")
    return raw.rstrip("/")


def _get_tls_verify() -> bool | str | ssl.SSLContext:
    """TLS verification setting for the upstream httpx client.

    Resolution order:
    1. ``CONVERTER_TLS_CA`` (a CA bundle file path) — verify the chain against
       that CA with hostname checking DISABLED. This is the recommended setting:
       LiteLLM serves a self-signed cert bound to ``HOST_IP`` but the converter
       dials it by the internal Docker name ``litellm``, so hostname verification
       would fail deterministically; pinning the CA still rejects any other cert
       a co-tenant container might present.
    2. ``CONVERTER_TLS_VERIFY`` — ``true``/``1``/``yes``/``on`` → verify with
       system CAs; any other non-empty non-boolean value → treat as a CA bundle
       path (with hostname checking ON).
    3. Default (both unset) → ``False`` (no verification). Trusted private
       network only.
    """
    ca = os.getenv("CONVERTER_TLS_CA", "").strip()
    if ca:
        ctx = ssl.create_default_context(cafile=ca)
        # The LiteLLM cert's SAN is HOST_IP, not the Docker service name we dial.
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


_MID_SYSTEM_POLICIES = frozenset({"user", "hoist", "asis"})

# Which models get their system messages normalized at all. Only the dotted
# Qwen3.x generations (3.5 / 3.6 / 3.8) hard-error on a misplaced system turn;
# the hyphenated 2025 line (``qwen3-8b``) tolerates it, so ``qwen3`` alone would
# over-match and reshape requests that never needed it.
_DEFAULT_MID_SYSTEM_MODEL_PATTERN = r"qwen3\.\d"

# Effort levels a vLLM/SGLang backend actually accepts. The OpenAI ladder the
# clients send is wider (see ``reasoning_effort.EFFORT_LADDER``), and an
# out-of-vocabulary value is a hard 400, not a silent downgrade.
_DEFAULT_REASONING_EFFORT_LEVELS = frozenset({"low", "medium", "high"})

_TRUTHY = frozenset({"true", "1", "yes", "on"})
_FALSY = frozenset({"false", "0", "no", "off"})

# Vendor prefix the model listing advertises aliases under. Claude Code discovers
# models by asking for the list and keeping the ones that look like Claude models,
# so every model needs a ``claude/``-prefixed twin to be selectable there.
_DEFAULT_MODEL_ALIAS_PREFIX = "claude/"


def _get_timeout() -> httpx.Timeout:
    """httpx timeout for the upstream client.

    ``connect``/``write``/``pool`` are always bounded so an unreachable or
    wedged LiteLLM fails fast instead of pinning a worker forever. ``read`` is
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
    def LITELLM_URL(self) -> str:
        return _get_litellm_url()

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
        ceiling instead, so a LiteLLM that accepts the connection and then stalls
        (or trickles) the body cannot pin a worker forever. Default 600s; set
        ``CONVERTER_NONSTREAM_TIMEOUT`` <= 0 to disable (restore unbounded)."""
        raw = _int_env("CONVERTER_NONSTREAM_TIMEOUT", 600)
        return None if raw <= 0 else float(raw)

    @property
    def response_store_ttl(self) -> float:
        """TTL (seconds) for the previous_response_id conversation store."""
        return float(_int_env("CONVERTER_RESPONSE_STORE_TTL", 3600))

    @property
    def response_store_max(self) -> int:
        """Max stored conversations before LRU eviction."""
        return _int_env("CONVERTER_RESPONSE_STORE_MAX", 10000)

    @property
    def response_store_max_bytes(self) -> int:
        """Total approx-serialized byte budget for stored transcripts before LRU
        eviction (0 disables). Safety net for image-heavy / many concurrent
        chains, which the entry-count cap alone does not bound. Default 64 MiB."""
        return _int_env("CONVERTER_RESPONSE_STORE_MAX_BYTES", 64 * 1024 * 1024)

    @property
    def response_store_max_entry_bytes(self) -> int:
        """Per-response transcript byte cap (0 disables). Oversized transcripts
        are not persisted, so they cannot evict every other active chain.
        Default 16 MiB."""
        return _int_env("CONVERTER_RESPONSE_STORE_MAX_ENTRY_BYTES", 16 * 1024 * 1024)

    @property
    def response_store_path(self) -> str:
        """Optional SQLite file path for a restart-safe previous_response_id store."""
        return os.getenv("CONVERTER_RESPONSE_STORE_PATH", "").strip()

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
    def mid_system_policy(self) -> str:
        """Placement policy for system messages that would land after index 0
        (``CONVERTER_MID_SYSTEM_POLICY``). Strict chat templates — newer Qwen
        among them — raise "System message must be at the beginning." and the
        backend answers 400, so a request carrying a second or mid-history
        system turn never reaches the model:

        * ``user`` — merge the leading system run into one head message and
          role-swap later system messages to ``user`` where they stand.
        * ``hoist`` — merge every system message into one leading system message.
        * ``asis`` — forward untouched (tolerant templates / debugging).

        Defaults to ``user`` because Claude Code sends mid-history
        ``role: "system"`` reminders on nearly every non-trivial turn: the
        alternative default (``asis``) breaks that client outright, while
        ``hoist`` silently relocates a reminder away from the turn it belongs to.
        Missing, empty, or unrecognized values fall back silently, matching
        ``_int_env``/``_bool_env``."""
        raw = os.getenv("CONVERTER_MID_SYSTEM_POLICY", "").strip().lower()
        return raw if raw in _MID_SYSTEM_POLICIES else "user"

    @property
    def mid_system_model_regex(self) -> re.Pattern[str]:
        """Which models :attr:`mid_system_policy` applies to
        (``CONVERTER_MID_SYSTEM_MODEL_PATTERN``). The pattern is *searched*
        (:meth:`re.Pattern.search`, case-insensitive) against the OUTBOUND
        chat/completions ``model`` value rather than matched whole, so a
        provider-prefixed deployment name — ``hosted_vllm/qwen3.5-32b`` — hits on
        the substring and needs no wildcards.

        The gate exists because normalization is not free of consequence: it
        role-swaps or relocates a system turn, so a backend whose template
        tolerates mid-history system messages should keep receiving the client's
        original shape. The default ``qwen3\\.\\d`` covers the strict dotted
        Qwen3.5 / 3.6 / 3.8 generations while deliberately NOT matching the
        tolerant 2025-line ``qwen3-8b`` naming. Set ``.*`` to restore
        normalize-for-every-model.

        An unparseable regex falls back to the default silently, matching
        ``_int_env``/:attr:`mid_system_policy` — a typo in compose must not fail
        every request. Re-compiling per access is cheap (``re.compile`` hits its
        own module-level cache), so this stays a plain re-reading property like
        the rest."""
        raw = os.getenv("CONVERTER_MID_SYSTEM_MODEL_PATTERN", "").strip()
        if raw:
            try:
                return re.compile(raw, re.IGNORECASE)
            except re.error:
                pass
        return re.compile(_DEFAULT_MID_SYSTEM_MODEL_PATTERN, re.IGNORECASE)

    @property
    def model_alias_prefix(self) -> str:
        """Prefix for the aliased model ids ``GET /v1/models`` advertises
        (``CONVERTER_MODEL_ALIAS_PREFIX``, default ``claude/``).

        Claude Code auto-detects models from the listing by vendor, so each real
        model is advertised twice: under its own id and under
        ``{prefix}{id}``. ``/v1/messages`` and ``/v1/responses`` strip the prefix
        back off on the way in, so an aliased id is callable.

        Set to the empty string to disable both halves — no aliases in the
        listing, no stripping on inbound requests. Unset (not empty) keeps the
        default; that distinction is why this reads ``os.getenv`` directly
        instead of going through a helper."""
        raw = os.getenv("CONVERTER_MODEL_ALIAS_PREFIX")
        if raw is None:
            return _DEFAULT_MODEL_ALIAS_PREFIX
        return raw.strip()

    @property
    def sse_heartbeat_seconds(self) -> float:
        """Idle interval after which a streaming response emits an SSE comment
        (``: ping``) to keep the connection's byte flow alive. LLM streams can be
        silent past a proxy's read timeout (nginx/APISIX, LBs) during long TTFT or
        reasoning; the heartbeat stops those intermediaries from dropping the
        socket. <= 0 disables it. Default 15s."""
        return float(_int_env("CONVERTER_SSE_HEARTBEAT_SECONDS", 15))

    @property
    def reasoning_effort_levels(self) -> frozenset[str] | None:
        """Effort vocabulary the backend accepts
        (``CONVERTER_REASONING_EFFORT_LEVELS``, default ``low,medium,high``).

        Both bridges forward the client's effort as chat/completions
        ``reasoning_effort`` alongside ``allowed_openai_params``, so LiteLLM
        hands it to the backend verbatim — and vLLM/SGLang answer 400 on
        anything outside ``low|medium|high``, while Codex's ladder reaches
        ``xhigh``/``max``/``ultra`` and Claude Code's ``output_config.effort``
        reaches ``max``. Values outside this set are clamped to the nearest
        listed level (unknown names are dropped) so a too-ambitious effort
        degrades instead of failing the request.

        Comma-separated, case-insensitive; unset or blank keeps the default. The
        literal ``*`` returns ``None``, which restores verbatim forwarding for a
        backend that understands the full ladder."""
        raw = os.getenv("CONVERTER_REASONING_EFFORT_LEVELS", "").strip()
        if not raw:
            return _DEFAULT_REASONING_EFFORT_LEVELS
        if raw == "*":
            return None
        levels = frozenset(part.strip().lower() for part in raw.split(",") if part.strip())
        return levels or _DEFAULT_REASONING_EFFORT_LEVELS

    @property
    def length_as_completed(self) -> str:
        """Whether a ``finish_reason=length`` truncation is reported as a
        terminal ``response.completed`` instead of the spec-correct
        ``response.incomplete`` (``CONVERTER_LENGTH_AS_COMPLETED``).

        Codex CLI reads ``response.incomplete`` as a failed stream and re-sends
        the entire turn up to ``stream_max_retries`` (default 5) times, so one
        truncated generation costs six — and every retry truncates again.

        * ``auto`` (default) — completed for Codex clients only, detected from
          the ``originator`` / ``user-agent`` request headers; every other
          client keeps the spec behaviour.
        * ``true`` — completed for every client.
        * ``false`` — spec behaviour always.

        Unrecognized values fall back to ``auto``, matching
        ``_int_env``/:attr:`mid_system_policy`."""
        raw = os.getenv("CONVERTER_LENGTH_AS_COMPLETED", "").strip().lower()
        if raw in _TRUTHY:
            return "true"
        if raw in _FALSY:
            return "false"
        return "auto"

    @property
    def flatten_namespace_tools(self) -> bool:
        """Whether to flatten a Responses ``namespace`` tool's inner functions
        into top-level chat/completions functions
        (``CONVERTER_FLATTEN_NAMESPACE_TOOLS``, default true).

        Codex CLI bundles its client-side tools — the ``multi_agent_v1``
        sub-agent controls (spawn_agent / send_input / wait_agent / close_agent /
        resume_agent) and ``image_gen`` — inside a Responses ``namespace`` tool, a
        container shape chat/completions has no way to represent. Left
        unflattened the whole namespace is dropped, its functions never reach the
        backend, and the model reports the tools missing.

        When true the converter flattens each namespace's inner functions to
        top-level chat functions on the request and re-stamps the originating
        ``namespace`` onto the ``function_call`` items it emits on the response —
        Codex routes a returned call by ``{namespace, name}`` (a chat/completions
        tool call carries only ``function.name``), so without the re-stamp it
        cannot dispatch the call back to the client-side tool. When false the old
        drop behaviour is restored.

        Missing, empty, or unrecognized values fall back to the default, matching
        ``_bool_env``."""
        return _bool_env("CONVERTER_FLATTEN_NAMESPACE_TOOLS", True)


settings = _Settings()
