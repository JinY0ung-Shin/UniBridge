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

Inter-token latency metric
--------------------------
The same hook also records a true inter-token latency (ITL) histogram,
``litellm_inter_token_latency_seconds``. LiteLLM publishes no real ITL: its
``litellm_deployment_latency_per_output_token`` divides the *whole* request
duration by the output token count, so every sample carries TTFT in the
numerator and reads high for short answers. This one measures only the interval
after the first token (completion time minus first-token time).

The unit of observation is one *token gap*, not one request: a call that
streamed N completion tokens contributes N-1 samples, each at that request's
mean gap. So percentiles are token-weighted (a 2000-token generation counts
2000× a 1-token one, which is what a decode-latency SLO means),
``rate(_sum)/rate(_count)`` is the true token-weighted mean ITL, and
``rate(_count)`` is streamed output-token throughput. The individual chunk
timestamps are not visible at this hook, so within-request variance is smoothed
to the mean — the aggregate is exact, one request's spread is not.

Streamed requests only: without a first-token timestamp there is no gap to
measure, so non-streamed calls record nothing.

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
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

try:
    from prometheus_client import Histogram
except Exception:  # prometheus_client absent (e.g. a bare test venv)
    Histogram = None  # type: ignore[assignment]

# Bumped only on a breaking change to the record shape; the build script
# refuses records it does not understand rather than guessing.
SCHEMA_VERSION = 1

DEFAULT_DATASET_DIR = "/var/lib/litellm-dataset"

# Retention (#44) runs opportunistically from the write path, so it must stay
# cheap. A full sweep fires at most once per process per day at the daily
# rotation boundary; when a byte cap is set, a byte-only sweep is additionally
# allowed no more often than this so a single high-traffic day cannot outrun
# LITELLM_DATASET_MAX_TOTAL_BYTES — while never costing more than one os.scandir
# per interval.
SIZE_CHECK_INTERVAL_SECONDS = 300

_DATASET_PREFIX = "dataset-"
_DATASET_SUFFIX = ".jsonl"


def _env_int(name: str, default: int) -> int:
    """Read a non-negative int env var; ``default`` on anything unparseable.

    Retention config is operator-supplied, so a typo must never crash the proxy:
    negatives collapse to 0 (the "disabled/unlimited" sentinel) and a
    non-numeric value falls back to the default with a one-line notice.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        print(f"JsonlDatasetLogger: ignoring invalid {name}={raw!r}")
        return default
    return value if value > 0 else 0


def _dataset_file_date(name: str) -> str | None:
    """Return the ``YYYYMMDD`` key of a ``dataset-YYYYMMDD.jsonl`` file, else None.

    The date is parsed from the filename this logger controls, so cleanup only
    ever considers files it wrote and never trusts mtime (which a volume remount
    or restore can reset). The key is a zero-padded date string, so plain string
    ordering is chronological.
    """
    if not (name.startswith(_DATASET_PREFIX) and name.endswith(_DATASET_SUFFIX)):
        return None
    key = name[len(_DATASET_PREFIX) : -len(_DATASET_SUFFIX)]
    # Exactly the 8-digit form this logger writes: strptime alone would accept
    # short forms like "2026082" as a valid date.
    if len(key) != 8 or not key.isdigit():
        return None
    try:
        datetime.strptime(key, "%Y%m%d")
    except ValueError:
        return None
    return key


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


# --- Inter-token latency metric ---------------------------------------------
# Registered in the default prometheus_client registry, which is the one
# LiteLLM's own /metrics endpoint serves — so this rides along on the endpoint
# the stack Prometheus already scrapes and the llm-metrics gateway route
# exposes, with no scrape target of its own.
ITL_METRIC_NAME = "litellm_inter_token_latency_seconds"
# SGLang's sglang:inter_token_latency_seconds buckets verbatim
# (python/sglang/srt/observability/metrics_collector.py, sgl-project/sglang main
# as of 2026-09-01). Same semantic family — per-token-gap samples, token-weighted
# — so histogram_quantile() over this metric is directly comparable with the
# same query against an SGLang backend, no bucket-boundary caveat. vLLM's
# vllm:inter_token_latency_seconds uses a coarser 0.01–80s ladder, so
# comparisons against vLLM stay approximate.
ITL_BUCKETS = (
    0.002, 0.004, 0.006, 0.008, 0.010, 0.015, 0.020, 0.025, 0.030,
    0.035, 0.040, 0.060, 0.080, 0.100, 0.200, 0.400, 0.600, 0.800,
    1.0, 2.0, 4.0, 6.0, 8.0,
)

# Set once if the weighted fast path ever falls back, so the notice is a
# one-liner per process instead of one per request.
_ITL_FALLBACK_REPORTED = False


def _build_itl_histogram() -> Any:
    """The ITL histogram, or None when it cannot be registered.

    Registration is an import-time side effect, so it has to tolerate both a
    missing prometheus_client and this module being loaded twice in one process
    (re-registering a metric name raises). Either way the recorder below no-ops
    rather than letting an observability extra break the proxy.
    """
    if Histogram is None:
        return None
    try:
        return Histogram(
            ITL_METRIC_NAME,
            "Inter-token latency in seconds, one sample per output-token gap, each "
            "recorded at its request's mean gap (chunk timestamps are not visible at "
            "this hook). Streamed requests only; recorded by UniBridge's custom "
            "callback, not an upstream LiteLLM metric",
            ["model"],
            buckets=ITL_BUCKETS,
        )
    except Exception as e:
        print(f"JsonlDatasetLogger: inter-token latency metric disabled: {e}")
        return None


_ITL_HISTOGRAM = _build_itl_histogram()


def _observe_weighted(child: Any, value: float, count: int, total: float) -> None:
    """Record ``count`` observations of ``value``, summing to ``total``, at once.

    prometheus_client exposes no weighted observe, and a 2000-token answer would
    otherwise mean 1999 ``observe()`` calls inside the request path's logging
    hook. Buckets are stored non-cumulative and cumulated at collect time, so a
    single ``inc(count)`` on the matching bucket is exactly equivalent to that
    loop — the increment is what ``_count`` is derived from.

    Boundary semantics mirror ``Histogram.observe``: first bound where
    ``value <= bound`` wins, and prometheus_client always appends +Inf, so a
    match always exists.

    ``_upper_bounds`` / ``_buckets`` / ``_sum`` are private. They have been
    stable for years, but every one of them is read *before* anything is
    mutated: a fallback after a partial write would double-count, since
    ``observe()`` increments the same sum again.
    """
    global _ITL_FALLBACK_REPORTED
    try:
        bounds = child._upper_bounds
        buckets = child._buckets
        sum_value = child._sum
        if len(bounds) != len(buckets):
            raise AttributeError("bucket layout does not match the bound list")
    except Exception as e:
        if not _ITL_FALLBACK_REPORTED:
            _ITL_FALLBACK_REPORTED = True
            print(
                "JsonlDatasetLogger: inter-token latency weighted fast path "
                f"unavailable (prometheus_client internals changed: {e}); "
                "falling back to loop"
            )
        for _ in range(count):
            child.observe(value)
        return

    # Past this point nothing may fall back to observe() — a retry would add the
    # sum twice. A failure here propagates to the caller's never-raise wrapper.
    for index, bound in enumerate(bounds):
        if value <= bound:
            buckets[index].inc(count)
            break
    sum_value.inc(total)


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
        # Retention (#44). Both default to 0 = unlimited, preserving the
        # unbounded full-history capture anyone may already rely on. Enforced
        # opportunistically from the write path — see _maybe_cleanup.
        self.retention_days = _env_int("LITELLM_DATASET_RETENTION_DAYS", 0)
        self.max_total_bytes = _env_int("LITELLM_DATASET_MAX_TOTAL_BYTES", 0)
        # Throttle state for _maybe_cleanup. The non-blocking _cleanup_lock stops
        # concurrent callers from each launching a sweep; the date/time markers
        # gate how often one runs at all.
        self._cleanup_lock = threading.Lock()
        self._last_cleanup_date = ""
        self._last_cleanup_at = 0.0
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
        # Self-contained and first: the helper swallows its own errors, so a
        # metric problem can never cost the dataset line, nor the reverse.
        self._record_inter_token_latency(kwargs, response_obj, end_time)
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
            # Opportunistic retention, after the write so a sweep can never delay
            # or displace the capture it just made. Never raises.
            self._maybe_cleanup(completed_at)
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
    def _record_inter_token_latency(
        kwargs: dict[str, Any], response_obj: Any, end_time: Any
    ) -> None:
        """Observe one streamed call's token gaps. Never raises.

        ITL is the time from the first token to the last, spread over the gaps
        between tokens — so it needs a first-token timestamp and at least two
        completion tokens (one token has no gap). The request contributes one
        sample per gap, so percentiles weight a long generation proportionally.
        Anything else, including every non-streamed call, is skipped silently:
        this is an extra signal, not a contract, and a missing sample must never
        turn into a log line per request.
        """
        if _ITL_HISTOGRAM is None:
            return
        try:
            slo = kwargs.get("standard_logging_object")
            if not isinstance(slo, dict):
                slo = {}

            if not _first_not_none(slo.get("stream"), kwargs.get("stream"), False):
                return

            first_token_at = _as_utc_datetime(
                kwargs.get("completion_start_time")
            ) or _as_utc_datetime(
                _first_not_none(
                    slo.get("completionStartTime"), slo.get("completion_start_time")
                )
            )
            completed_at = _as_utc_datetime(end_time) or _as_utc_datetime(
                _first_not_none(slo.get("endTime"), slo.get("end_time"))
            )
            if first_token_at is None or completed_at is None:
                return

            response = _as_dict(slo.get("response")) or _as_dict(response_obj) or {}
            usage = response.get("usage")
            if not isinstance(usage, dict):
                usage = {}
            try:
                completion_tokens = int(
                    _first_not_none(
                        slo.get("completion_tokens"), usage.get("completion_tokens")
                    )
                )
            except (TypeError, ValueError):
                return
            if completion_tokens < 2:
                return

            elapsed = (completed_at - first_token_at).total_seconds()
            if elapsed < 0:
                return  # clock skew between the two timestamps; not a measurement

            # kwargs first, so the label matches the value LiteLLM's own token
            # metrics carry — the dashboards group the two together on `model`.
            model = kwargs.get("model") or slo.get("model") or "unknown"
            gaps = completion_tokens - 1
            _observe_weighted(
                _ITL_HISTOGRAM.labels(model=str(model)),
                elapsed / gaps,
                gaps,
                elapsed,
            )
        except Exception as e:
            print(f"JsonlDatasetLogger metrics error: {e}")

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

    def _maybe_cleanup(self, completed_at: datetime) -> None:
        """Delete aged / over-budget dataset files. Best-effort, never raises.

        A full sweep runs at the daily-rotation boundary (once per process per
        day); when a byte cap is set, a byte sweep may also run between
        boundaries, no more often than SIZE_CHECK_INTERVAL_SECONDS. When neither
        cap is configured this returns before touching the filesystem at all.
        """
        if self.retention_days <= 0 and self.max_total_bytes <= 0:
            return
        try:
            today = completed_at.strftime("%Y%m%d")
            now = time.monotonic()
            due = today != self._last_cleanup_date
            if not due and self.max_total_bytes > 0:
                due = (now - self._last_cleanup_at) >= SIZE_CHECK_INTERVAL_SECONDS
            if not due:
                return
            # One sweep at a time: if another caller is already cleaning, its
            # sweep will subsume this call's growth too, so skip rather than
            # queue behind it (and never block the request path on a delete).
            if not self._cleanup_lock.acquire(blocking=False):
                return
            try:
                self._last_cleanup_date = today
                self._last_cleanup_at = now
                self._run_cleanup(today)
            finally:
                self._cleanup_lock.release()
        except Exception as e:
            print(f"JsonlDatasetLogger cleanup error: {e}")

    def _run_cleanup(self, today: str) -> None:
        """One os.scandir, then unlink aged files, then oldest-first over budget.

        The day still being written (``today``) is never deleted, so the byte cap
        may be briefly exceeded by at most the current day's file — preferable to
        deleting data the proxy just captured and will keep appending to.
        """
        files: list[tuple[str, int, str]] = []
        total = 0
        with os.scandir(self.dataset_dir) as entries:
            for entry in entries:
                date_key = _dataset_file_date(entry.name)
                if date_key is None:
                    continue
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                files.append((date_key, size, entry.path))
                total += size
        if not files:
            return
        files.sort()  # string date keys sort oldest-first

        removed = 0
        freed = 0
        survivors: list[tuple[str, int, str]] = []

        if self.retention_days > 0:
            cutoff = (
                datetime.strptime(today, "%Y%m%d")
                - timedelta(days=self.retention_days - 1)
            ).strftime("%Y%m%d")
            for date_key, size, path in files:
                if date_key < cutoff and self._unlink(path):
                    removed += 1
                    freed += size
                    total -= size
                else:
                    survivors.append((date_key, size, path))
        else:
            survivors = files

        if self.max_total_bytes > 0:
            for date_key, size, path in survivors:
                if total <= self.max_total_bytes:
                    break
                if date_key >= today:
                    continue  # never delete the day still being written
                if self._unlink(path):
                    removed += 1
                    freed += size
                    total -= size

        if removed:
            print(
                f"JsonlDatasetLogger cleanup: removed {removed} file(s), "
                f"freed {freed} bytes, {total} bytes remain"
            )

    @staticmethod
    def _unlink(path: str) -> bool:
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return False  # already gone (raced with another sweep) — fine
        except OSError as e:
            print(f"JsonlDatasetLogger cleanup: could not remove {path}: {e}")
            return False


# LiteLLM instantiates nothing itself: `callbacks:` in config.yaml points at
# this already-constructed instance by dotted path.
proxy_handler_instance = JsonlDatasetLogger()
