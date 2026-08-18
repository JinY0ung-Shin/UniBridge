"""LiteLLM custom callback: capture every successful LLM call as JSONL.

Why this exists
---------------
All client traffic reaches LiteLLM authenticated as the master key (APISIX
injects it), so the proxy's Postgres tables and the Prometheus metrics record
*how much* was spent and *by whom* (the ``end_user`` field), but they never
keep the conversation itself. Fine-tuning needs the conversation. This logger
appends one JSON line per successful call to a Docker volume
(``litellm-dataset``), which ``scripts/build_finetune_dataset.py`` later turns
into a training-ready chat dataset offline.

Streaming is handled for free: LiteLLM hands success callbacks the *assembled*
final response (``complete_streaming_response``), not individual chunks, so a
streamed call produces exactly one line just like a non-streamed one.

Record schema (``schema: 1``)
-----------------------------
One JSON object per line in ``dataset-YYYYMMDD.jsonl`` (UTC date, daily
rotation)::

    {
      "schema": 1,
      "id":               LiteLLM call id,
      "trace_id":         LiteLLM trace id,
      "ts":               ISO8601 UTC timestamp of call completion,
      "model":            resolved model name,
      "end_user":         x-litellm-end-user-id (per-user attribution),
      "call_type":        e.g. "acompletion",
      "stream":           bool,
      "messages":         the request messages, verbatim,
      "response":         the raw response dict, verbatim,
      "prompt_tokens":    int | null,
      "completion_tokens":int | null,
      "cost":             float | null
    }

``response`` is kept RAW on purpose: reprocessability beats prettiness. The
build script extracts ``response["choices"][0]["message"]``, but keeping the
whole object means a future schema can mine finish reasons, logprobs or
reasoning content without re-capturing anything.

The never-raise constraint
--------------------------
This runs inside the live request path's logging hook. An exception escaping
from here surfaces on a user's request, so *every* path catches broadly and
prints instead. Assume every field of ``kwargs`` may be missing or a different
type than expected (provider quirks, LiteLLM version drift): losing one dataset
line is always preferable to breaking one request. ``print`` rather than
``logging`` because the proxy runs with ``LITELLM_LOG=ERROR``, which would
swallow anything below error level.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

# Bumped only on a breaking change to the record shape; the build script
# refuses records it does not understand rather than guessing.
SCHEMA_VERSION = 1

DEFAULT_DATASET_DIR = "/var/lib/litellm-dataset"


def _as_utc_datetime(value: Any) -> datetime | None:
    """Best-effort coercion of a LiteLLM timestamp to a tz-aware UTC datetime.

    LiteLLM passes ``datetime`` objects to the callback but stores epoch floats
    in ``standard_logging_object``, and older builds used ISO strings. Naive
    datetimes are assumed UTC (TZ=UTC everywhere in this project).
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return _as_utc_datetime(parsed)
    return None


def _as_dict(obj: Any) -> dict[str, Any] | None:
    """Return ``obj`` as a plain dict, or None if it cannot be one.

    Responses arrive either already serialized (inside
    ``standard_logging_object``) or as a pydantic ``ModelResponse`` (the
    ``response_obj`` argument), so try the pydantic accessors too.
    """
    if isinstance(obj, dict):
        return obj
    for attr in ("model_dump", "dict"):
        method = getattr(obj, attr, None)
        if callable(method):
            try:
                dumped = method()
            except Exception:
                continue
            if isinstance(dumped, dict):
                return dumped
    return None


def _nested_get(container: Any, *keys: str) -> Any:
    """Walk ``keys`` through nested mappings, returning None on any miss."""
    current = container
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


class JsonlDatasetLogger(CustomLogger):
    """Append successful LLM calls to a daily-rotated JSONL file.

    Registered from ``litellm/config.yaml`` as
    ``callbacks: custom_callbacks.proxy_handler_instance``. Only successes are
    captured — a failed call has no assistant turn to learn from.
    """

    def __init__(self) -> None:
        super().__init__()
        self.dataset_dir = os.environ.get("LITELLM_DATASET_DIR", DEFAULT_DATASET_DIR)
        # A single lock for all writes. Appends are tiny and the contention
        # cost is noise next to an LLM round trip, but it guarantees whole
        # lines even if LiteLLM ever calls us from more than one event loop or
        # worker thread (sync fallbacks, retries) — interleaved partial writes
        # would corrupt the file for every later reader.
        self._lock = threading.Lock()
        try:
            os.makedirs(self.dataset_dir, exist_ok=True)
        except Exception as e:
            # Never raise: a broken capture dir must not stop the proxy from
            # booting. Writes will retry the makedirs and keep reporting.
            print(f"JsonlDatasetLogger error: {e}")

    async def async_log_success_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        """Write one JSONL record for a completed call. Never raises."""
        try:
            built = self._build_record(kwargs, response_obj, end_time)
            if built is None:
                return
            record, completed_at = built
            # default=str so an exotic object (Decimal, enum, pydantic leaf)
            # degrades to its string form instead of killing the dump.
            line = json.dumps(record, ensure_ascii=False, default=str)
            path = os.path.join(
                self.dataset_dir, f"dataset-{completed_at.strftime('%Y%m%d')}.jsonl"
            )
            with self._lock:
                # Re-assert the directory on every write: the volume may be
                # (re)mounted under a running proxy, and exist_ok makedirs is
                # a rounding error next to the request it just served.
                os.makedirs(self.dataset_dir, exist_ok=True)
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except Exception as e:
            print(f"JsonlDatasetLogger error: {e}")

    def _build_record(
        self, kwargs: dict[str, Any], response_obj: Any, end_time: Any
    ) -> tuple[dict[str, Any], datetime] | None:
        """Assemble the record, or None when the call is not worth capturing.

        Returns the record plus its completion time, so the record's ``ts`` and
        the file it lands in can never disagree.
        """
        slo = kwargs.get("standard_logging_object")
        if not isinstance(slo, dict):
            slo = {}
        metadata = slo.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}

        # Prefer the StandardLoggingPayload (already normalized across
        # providers), fall back to the raw kwargs LiteLLM was called with.
        messages = slo.get("messages")
        if not isinstance(messages, list) or not messages:
            messages = kwargs.get("messages")
        if not isinstance(messages, list) or not messages:
            return None  # embeddings, moderation, health checks — nothing to train on

        response = _as_dict(slo.get("response")) or _as_dict(response_obj)
        if not response:
            return None

        usage = response.get("usage")
        if not isinstance(usage, dict):
            usage = {}

        completed_at = (
            _as_utc_datetime(end_time)
            or _as_utc_datetime(_first_not_none(slo.get("endTime"), slo.get("end_time")))
            or datetime.now(timezone.utc)
        )

        record = {
            "schema": SCHEMA_VERSION,
            "id": _first_not_none(slo.get("id"), kwargs.get("litellm_call_id")),
            "trace_id": slo.get("trace_id"),
            "ts": completed_at.isoformat(),
            "model": _first_not_none(slo.get("model"), kwargs.get("model")),
            "end_user": self._resolve_end_user(kwargs, metadata),
            "call_type": _first_not_none(slo.get("call_type"), kwargs.get("call_type")),
            "stream": bool(_first_not_none(slo.get("stream"), kwargs.get("stream"), False)),
            "messages": messages,
            "response": response,
            "prompt_tokens": _first_not_none(
                slo.get("prompt_tokens"), usage.get("prompt_tokens")
            ),
            "completion_tokens": _first_not_none(
                slo.get("completion_tokens"), usage.get("completion_tokens")
            ),
            "cost": _first_not_none(slo.get("response_cost"), kwargs.get("response_cost")),
        }
        return record, completed_at

    @staticmethod
    def _resolve_end_user(kwargs: dict[str, Any], metadata: dict[str, Any]) -> Any:
        """Per-user attribution: the only identity we have.

        Every request authenticates as the master key, so ``end_user`` (set by
        the ``x-litellm-end-user-id`` header APISIX forwards) is what separates
        one person's conversations from another's — and it is the grouping key
        the build script dedups within.
        """
        return _first_not_none(
            metadata.get("user_api_key_end_user_id"),
            _nested_get(kwargs, "litellm_params", "metadata", "user_api_key_end_user_id"),
            _nested_get(kwargs, "litellm_params", "metadata", "end_user_id"),
            kwargs.get("user"),
        )


# LiteLLM instantiates nothing itself: `callbacks:` in config.yaml points at
# this already-constructed instance by dotted path.
proxy_handler_instance = JsonlDatasetLogger()
