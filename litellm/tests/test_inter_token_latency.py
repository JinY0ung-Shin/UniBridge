"""Tests for the inter-token latency histogram in ``litellm/custom_callbacks.py``.

``conftest.py`` loads the real module by path, so these observe the actual
process-global histogram. Every test therefore uses its own ``model`` label
value: label children are independent, so one test's samples can never be read
as another's, and the tests stay order-independent.

Run from the repo root: ``python -m pytest litellm/tests/``.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

import custom_callbacks_under_test as cc

# The handle is None when prometheus_client is missing (or, in a stranger
# environment, when the metric name was already registered by something else).
# The recorder is then a documented no-op, so only the observation cases skip.
needs_histogram = pytest.mark.skipif(
    cc._ITL_HISTOGRAM is None,
    reason="prometheus_client unavailable or metric already registered",
)

START = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


# --- Helpers -----------------------------------------------------------------
def call_args(
    *,
    model="test-model",
    stream=True,
    first_token_after=0.2,
    duration=1.0,
    completion_tokens=5,
    completion_start_time="kwargs",
):
    """One (kwargs, response_obj, start_time, end_time) tuple for the hook.

    ``completion_start_time`` picks where the first-token timestamp lives:
    ``"kwargs"`` (a datetime, how LiteLLM calls the callback), ``"slo"`` (an
    epoch float, how the StandardLoggingPayload stores it), or None for neither.
    """
    end_time = START + timedelta(seconds=duration)
    first_token_at = (
        None if first_token_after is None else START + timedelta(seconds=first_token_after)
    )
    response = {
        "choices": [{"message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": completion_tokens},
    }
    slo = {
        "messages": [{"role": "user", "content": "hello"}],
        "model": f"slo/{model}",
        "stream": stream,
        "response": response,
        "endTime": end_time.timestamp(),
        "completion_tokens": completion_tokens,
    }
    kwargs = {
        "model": model,
        "stream": stream,
        "messages": [{"role": "user", "content": "hello"}],
        "standard_logging_object": slo,
    }
    if first_token_at is not None:
        if completion_start_time == "kwargs":
            kwargs["completion_start_time"] = first_token_at
        elif completion_start_time == "slo":
            slo["completionStartTime"] = first_token_at.timestamp()
    return kwargs, response, START, end_time


def observe(logger, args):
    asyncio.run(logger.async_log_success_event(*args))


def itl_for(model: str) -> tuple[float, float]:
    """(count, sum) of observations carrying this ``model`` label."""
    count = total = 0.0
    for metric in cc._ITL_HISTOGRAM.collect():
        for sample in metric.samples:
            if sample.labels.get("model") != model:
                continue
            if sample.name.endswith("_count"):
                count = sample.value
            elif sample.name.endswith("_sum"):
                total = sample.value
    return count, total


def make_logger(tmp_path, monkeypatch):
    monkeypatch.setenv("LITELLM_DATASET_DIR", str(tmp_path))
    monkeypatch.setenv("LITELLM_DATASET_RETENTION_DAYS", "0")
    monkeypatch.setenv("LITELLM_DATASET_MAX_TOTAL_BYTES", "0")
    return cc.JsonlDatasetLogger()


def dataset_lines(tmp_path) -> list[dict]:
    return [
        json.loads(line)
        for path in sorted(pathlib.Path(tmp_path).glob("dataset-*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


# --- Registration ------------------------------------------------------------
def test_metric_shape_is_stable():
    """The name and buckets are the scrape contract; changing them silently
    breaks every dashboard and recording rule built on them."""
    assert cc.ITL_METRIC_NAME == "litellm_inter_token_latency_seconds"
    assert cc.ITL_BUCKETS[0] == 0.001
    assert cc.ITL_BUCKETS[-1] == 2.0
    assert list(cc.ITL_BUCKETS) == sorted(cc.ITL_BUCKETS)


def test_building_the_histogram_twice_yields_none_not_an_exception():
    """Second load of the module in one process must not raise — a duplicate
    registration disables the metric instead."""
    if cc._ITL_HISTOGRAM is None:
        pytest.skip("prometheus_client unavailable")
    assert cc._build_itl_histogram() is None


# --- Observation -------------------------------------------------------------
@needs_histogram
def test_streamed_call_observes_mean_inter_token_latency(tmp_path, monkeypatch):
    logger = make_logger(tmp_path, monkeypatch)
    model = "itl-happy-path"
    # First token at 0.2s, done at 1.0s, 5 completion tokens: 0.8s over 4 gaps.
    observe(
        logger,
        call_args(model=model, first_token_after=0.2, duration=1.0, completion_tokens=5),
    )

    count, total = itl_for(model)
    assert count == 1.0
    assert total == pytest.approx(0.2)
    # The metric must not cost the dataset line it rides along with.
    assert len(dataset_lines(tmp_path)) == 1


@needs_histogram
def test_observation_reaches_the_default_registry_exposition(tmp_path, monkeypatch):
    """The whole delivery mechanism: the metric must be in the registry
    prometheus_client exposes, because that is the one LiteLLM's own /metrics
    endpoint serves — a private registry would scrape as nothing."""
    generate_latest = pytest.importorskip("prometheus_client").generate_latest

    logger = make_logger(tmp_path, monkeypatch)
    model = "itl-exposition"
    observe(logger, call_args(model=model, first_token_after=0.2, duration=1.0,
                              completion_tokens=5))

    text = generate_latest().decode()
    assert f'{cc.ITL_METRIC_NAME}_count{{model="{model}"}} 1.0' in text
    assert f'{cc.ITL_METRIC_NAME}_sum{{model="{model}"}} 0.2' in text


@needs_histogram
def test_first_token_time_from_standard_logging_object(tmp_path, monkeypatch):
    """LiteLLM stores the timestamp as an epoch float in the payload; the
    callback kwargs carry a datetime. Both must work."""
    logger = make_logger(tmp_path, monkeypatch)
    model = "itl-slo-timestamp"
    observe(
        logger,
        call_args(
            model=model,
            first_token_after=0.5,
            duration=1.5,
            completion_tokens=3,
            completion_start_time="slo",
        ),
    )

    count, total = itl_for(model)
    assert count == 1.0
    assert total == pytest.approx(0.5)  # 1.0s over 2 gaps


@needs_histogram
def test_non_streaming_call_records_nothing(tmp_path, monkeypatch):
    logger = make_logger(tmp_path, monkeypatch)
    model = "itl-non-streaming"
    observe(logger, call_args(model=model, stream=False))

    assert itl_for(model) == (0.0, 0.0)
    assert len(dataset_lines(tmp_path)) == 1  # still captured for the dataset


@needs_histogram
@pytest.mark.parametrize("completion_tokens", [None, 0, 1])
def test_fewer_than_two_tokens_records_nothing(
    tmp_path, monkeypatch, completion_tokens
):
    """One token has no gap to measure, and no token count means no divisor."""
    logger = make_logger(tmp_path, monkeypatch)
    model = f"itl-tokens-{completion_tokens}"
    observe(logger, call_args(model=model, completion_tokens=completion_tokens))

    assert itl_for(model) == (0.0, 0.0)


@needs_histogram
def test_missing_first_token_time_records_nothing(tmp_path, monkeypatch):
    logger = make_logger(tmp_path, monkeypatch)
    model = "itl-no-first-token"
    observe(logger, call_args(model=model, first_token_after=None))

    assert itl_for(model) == (0.0, 0.0)


@needs_histogram
def test_unparseable_first_token_time_records_nothing(tmp_path, monkeypatch):
    logger = make_logger(tmp_path, monkeypatch)
    model = "itl-bad-first-token"
    kwargs, response, start, end = call_args(model=model)
    kwargs["completion_start_time"] = "not a timestamp"
    kwargs["standard_logging_object"].pop("completionStartTime", None)
    observe(logger, (kwargs, response, start, end))

    assert itl_for(model) == (0.0, 0.0)


@needs_histogram
def test_negative_delta_records_nothing(tmp_path, monkeypatch):
    """Clock skew put the first token after the completion — not a measurement."""
    logger = make_logger(tmp_path, monkeypatch)
    model = "itl-clock-skew"
    observe(logger, call_args(model=model, first_token_after=2.0, duration=1.0))

    assert itl_for(model) == (0.0, 0.0)


@needs_histogram
def test_model_label_prefers_kwargs(tmp_path, monkeypatch):
    """kwargs-first, so the label matches LiteLLM's own token metrics."""
    logger = make_logger(tmp_path, monkeypatch)
    model = "itl-label-source"
    kwargs, response, start, end = call_args(model=model)
    observe(logger, (kwargs, response, start, end))

    assert itl_for(model)[0] == 1.0
    assert itl_for(f"slo/{model}") == (0.0, 0.0)


@needs_histogram
def test_unknown_model_label_when_none_is_reported(tmp_path, monkeypatch):
    logger = make_logger(tmp_path, monkeypatch)
    kwargs, response, start, end = call_args()
    kwargs.pop("model")
    kwargs["standard_logging_object"].pop("model")
    before = itl_for("unknown")[0]
    observe(logger, (kwargs, response, start, end))

    assert itl_for("unknown")[0] == before + 1.0


# --- Never-raise / no-op -----------------------------------------------------
def test_dataset_line_still_written_when_metric_is_disabled(tmp_path, monkeypatch):
    """prometheus_client absent (handle None): the recorder no-ops and the
    dataset write is untouched."""
    monkeypatch.setattr(cc, "_ITL_HISTOGRAM", None)
    logger = make_logger(tmp_path, monkeypatch)
    observe(logger, call_args(model="itl-disabled"))

    lines = dataset_lines(tmp_path)
    assert len(lines) == 1
    assert lines[0]["completion_tokens"] == 5


def test_recorder_never_raises_on_garbage_kwargs():
    """Every field may be missing or the wrong type (provider quirks, version
    drift); the recorder must swallow all of it."""
    for kwargs in (
        {},
        {"stream": True},
        {"stream": True, "standard_logging_object": "not a dict"},
        {"stream": True, "completion_start_time": object()},
        {"stream": True, "completion_start_time": START, "model": object()},
    ):
        cc.JsonlDatasetLogger._record_inter_token_latency(kwargs, None, None)


def test_hook_never_raises_when_the_histogram_itself_fails(tmp_path, monkeypatch):
    """A broken metric client must not cost the dataset line."""

    class _Exploding:
        def labels(self, **_kwargs):
            raise RuntimeError("registry exploded")

    monkeypatch.setattr(cc, "_ITL_HISTOGRAM", _Exploding())
    logger = make_logger(tmp_path, monkeypatch)
    observe(logger, call_args(model="itl-exploding"))

    assert len(dataset_lines(tmp_path)) == 1
