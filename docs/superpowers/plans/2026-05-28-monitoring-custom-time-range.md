# Monitoring Custom Absolute Time Range — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users query LLM and gateway monitoring by a custom absolute `[start, end]` time range (KST-fixed), alongside the existing 7 presets.

**Architecture:** Backend gains a `resolve_time_window` FastAPI dependency that turns either a preset `range` or custom `start`/`end` epoch params into a `TimeWindow` (PromQL window, step, eval time, start/end). Instant queries evaluate at `eval_time=end`; range queries pass explicit `start`/`end`. Frontend extracts a shared `TimeRangeSelector` (presets + custom popover) driven by a `TimeSelection` union; all datetime interpretation and chart-axis labels are fixed to `Asia/Seoul`.

**Tech Stack:** FastAPI + httpx + Prometheus (backend, pytest/AsyncMock); React + TypeScript + @tanstack/react-query + recharts + react-i18next (frontend, vitest/RTL).

**Spec:** `docs/superpowers/specs/2026-05-28-monitoring-custom-time-range-design.md`

---

## File Structure

**Backend**
- `unibridge-service/app/services/prometheus_client.py` — add `eval_time` to `instant_query`, `start`/`end` to `range_query`.
- `unibridge-service/app/routers/gateway.py` — add `TimeWindow`, `_tier_for_span`, `_validate_custom_range`, `resolve_time_window`; rewire 13 metrics endpoints.
- `unibridge-service/tests/test_prometheus_client.py` — NEW unit tests for the two client functions.
- `unibridge-service/tests/test_gateway.py` — add resolver unit tests + custom-range endpoint tests.

**Frontend**
- `unibridge-ui/src/utils/time.ts` — add KST helpers (`kstLocalToEpoch`, `epochToKstLocal`, `formatChartTime`, `formatChartTimestamp`, `formatKstChip`).
- `unibridge-ui/src/utils/timeRange.ts` — NEW pure module: `TIME_RANGES`, `PRESET_SECONDS`, `TimeSelection`, `timeParams`, `selectionKey`, `selectionSpanSeconds`.
- `unibridge-ui/src/components/TimeRangeSelector.tsx` — NEW component (presets + custom popover).
- `unibridge-ui/src/components/TimeRangeSelector.css` — NEW styles for the popover/chip.
- `unibridge-ui/src/api/client.ts` — change `get*` metric signatures to accept `TimeSelection`.
- `unibridge-ui/src/pages/GatewayMonitoring.tsx` — use selector + selection state + KST labels.
- `unibridge-ui/src/pages/LlmMonitoring.tsx` — same.
- `unibridge-ui/src/locales/ko.json`, `en.json` — new i18n keys.
- `unibridge-ui/src/test/time.test.ts` — add KST helper tests.
- `unibridge-ui/src/test/client.api.test.ts` — update metric-endpoint tests to `TimeSelection`.
- `unibridge-ui/src/test/TimeRangeSelector.test.tsx` — NEW component test.

---

## Task 1: Prometheus client — `eval_time` + explicit `start`/`end`

**Files:**
- Modify: `unibridge-service/app/services/prometheus_client.py:20-56`
- Test: `unibridge-service/tests/test_prometheus_client.py` (create)

- [ ] **Step 1: Write failing tests**

Create `unibridge-service/tests/test_prometheus_client.py`:

```python
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services import prometheus_client


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _client_ctx(captured: dict):
    """Return an AsyncClient stub whose .get records params and returns success."""
    async def fake_get(url, params=None):
        captured["url"] = url
        captured["params"] = params
        return _Resp({"status": "success", "data": {"result": [{"value": [1, "5"]}]}})

    ctx = AsyncMock()
    ctx.__aenter__.return_value = AsyncMock(get=AsyncMock(side_effect=fake_get))
    ctx.__aexit__.return_value = False
    return ctx


@pytest.mark.asyncio
async def test_instant_query_omits_time_by_default():
    captured: dict = {}
    with patch("app.services.prometheus_client.httpx.AsyncClient", return_value=_client_ctx(captured)):
        await prometheus_client.instant_query("up")
    assert captured["params"] == {"query": "up"}


@pytest.mark.asyncio
async def test_instant_query_includes_eval_time():
    captured: dict = {}
    with patch("app.services.prometheus_client.httpx.AsyncClient", return_value=_client_ctx(captured)):
        await prometheus_client.instant_query("up", eval_time=1717000000.0)
    assert captured["params"] == {"query": "up", "time": "1717000000.0"}


@pytest.mark.asyncio
async def test_range_query_uses_explicit_start_end():
    captured: dict = {}
    with patch("app.services.prometheus_client.httpx.AsyncClient", return_value=_client_ctx(captured)):
        await prometheus_client.range_query("up", step="60s", start=100.0, end=700.0)
    assert captured["params"]["start"] == "100.0"
    assert captured["params"]["end"] == "700.0"
    assert captured["params"]["step"] == "60s"


@pytest.mark.asyncio
async def test_range_query_falls_back_to_duration_when_no_start_end():
    captured: dict = {}
    with patch("app.services.prometheus_client.httpx.AsyncClient", return_value=_client_ctx(captured)):
        await prometheus_client.range_query("up", duration="1h", step="60s")
    start = float(captured["params"]["start"])
    end = float(captured["params"]["end"])
    assert round(end - start) == 3600
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `cd unibridge-service && python -m pytest tests/test_prometheus_client.py -v`
Expected: `test_instant_query_includes_eval_time` and `test_range_query_uses_explicit_start_end` FAIL (unexpected `eval_time`/`start`/`end` kwargs → TypeError).

- [ ] **Step 3: Implement `instant_query` change**

In `prometheus_client.py`, replace the `instant_query` function (lines 20-30) with:

```python
async def instant_query(query: str, eval_time: float | None = None) -> list[dict[str, Any]]:
    """Execute a Prometheus instant query. Returns list of result items.

    eval_time (epoch seconds) sets the evaluation timestamp; when omitted
    Prometheus evaluates at server "now".
    """
    url = f"{_base_url()}/api/v1/query"
    params: dict[str, str] = {"query": query}
    if eval_time is not None:
        params["time"] = str(eval_time)
    async with httpx.AsyncClient(timeout=PROM_TIMEOUT) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
    if data.get("status") != "success":
        logger.warning("Prometheus query failed: %s", data)
        return []
    return data.get("data", {}).get("result", [])
```

- [ ] **Step 4: Implement `range_query` change**

Replace the `range_query` function (lines 33-56) with:

```python
async def range_query(
    query: str,
    duration: str = "1h",
    step: str = "60s",
    start: float | None = None,
    end: float | None = None,
) -> list[dict[str, Any]]:
    """Execute a Prometheus range query.

    When start and end (epoch seconds) are both given they are used directly;
    otherwise the window is duration ending at now.
    """
    url = f"{_base_url()}/api/v1/query_range"
    if start is not None and end is not None:
        start_ts, end_ts = float(start), float(end)
    else:
        end_ts = time.time()
        start_ts = end_ts - _parse_duration(duration)

    async with httpx.AsyncClient(timeout=PROM_TIMEOUT) as client:
        resp = await client.get(url, params={
            "query": query,
            "start": str(start_ts),
            "end": str(end_ts),
            "step": step,
        })
        resp.raise_for_status()
        data = resp.json()
    if data.get("status") != "success":
        logger.warning("Prometheus range query failed: %s", data)
        return []
    return data.get("data", {}).get("result", [])
```

- [ ] **Step 5: Run tests, verify pass**

Run: `cd unibridge-service && python -m pytest tests/test_prometheus_client.py -v`
Expected: 4 PASS.

- [ ] **Step 6: Commit**

```bash
git add unibridge-service/app/services/prometheus_client.py unibridge-service/tests/test_prometheus_client.py
git commit -m "feat(monitoring): prometheus_client supports eval_time and explicit start/end"
```

---

## Task 2: Backend `TimeWindow` resolver + helpers

**Files:**
- Modify: `unibridge-service/app/routers/gateway.py` (imports + after `RANGE_VOLUME` block at line 614)
- Test: `unibridge-service/tests/test_gateway.py` (append a `TestResolveTimeWindow` class)

- [ ] **Step 1: Write failing tests**

Append to `unibridge-service/tests/test_gateway.py` (end of file). It already imports `pytest` and uses `HTTPException`; add the import if missing at top: `from fastapi import HTTPException`.

```python
class TestResolveTimeWindow:
    def test_preset_window(self):
        from app.routers.gateway import resolve_time_window

        tw = resolve_time_window(time_range="6h", start=None, end=None)
        assert tw.is_custom is False
        assert tw.promql_window == "6h"
        assert tw.step == "300s"          # RANGE_STEPS["6h"]
        assert tw.volume_window == "30m"  # RANGE_VOLUME["6h"][1]
        assert tw.volume_step == "1800s"  # RANGE_VOLUME["6h"][0]
        assert tw.eval_time is None and tw.start is None and tw.end is None

    def test_invalid_preset_defaults_to_1h(self):
        from app.routers.gateway import resolve_time_window

        tw = resolve_time_window(time_range="nope", start=None, end=None)
        assert tw.promql_window == "1h"
        assert tw.step == "60s"

    def test_custom_window_maps_to_tier(self):
        from app.routers.gateway import resolve_time_window

        # 2 day span → tier "7d"
        start, end = 1_000_000, 1_000_000 + 2 * 86400
        tw = resolve_time_window(time_range="1h", start=start, end=end)
        assert tw.is_custom is True
        assert tw.promql_window == f"{2 * 86400}s"
        assert tw.step == "3600s"          # RANGE_STEPS["7d"]
        assert tw.volume_window == "1h"    # RANGE_VOLUME["7d"][1]
        assert tw.eval_time == float(end)
        assert tw.start == float(start) and tw.end == float(end)

    def test_custom_requires_both_bounds(self):
        from fastapi import HTTPException
        from app.routers.gateway import resolve_time_window

        with pytest.raises(HTTPException) as exc:
            resolve_time_window(time_range="1h", start=100, end=None)
        assert exc.value.status_code == 400

    def test_custom_rejects_reversed_range(self):
        from fastapi import HTTPException
        from app.routers.gateway import resolve_time_window

        with pytest.raises(HTTPException) as exc:
            resolve_time_window(time_range="1h", start=500, end=100)
        assert exc.value.status_code == 400

    def test_custom_rejects_future_end(self):
        import time as _t
        from fastapi import HTTPException
        from app.routers.gateway import resolve_time_window

        future = int(_t.time()) + 10_000
        with pytest.raises(HTTPException) as exc:
            resolve_time_window(time_range="1h", start=future - 3600, end=future)
        assert exc.value.status_code == 400

    def test_custom_rejects_tiny_span(self):
        from fastapi import HTTPException
        from app.routers.gateway import resolve_time_window

        with pytest.raises(HTTPException) as exc:
            resolve_time_window(time_range="1h", start=1000, end=1030)  # 30s
        assert exc.value.status_code == 400
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `cd unibridge-service && python -m pytest tests/test_gateway.py::TestResolveTimeWindow -v`
Expected: FAIL with `ImportError: cannot import name 'resolve_time_window'`.

- [ ] **Step 3: Add the `dataclass` import**

In `unibridge-service/app/routers/gateway.py`, `import time` (line 7) and `Depends`/`Query`/`status` (line 10) are already imported. Add a new import line after line 7:

```python
from dataclasses import dataclass
```

- [ ] **Step 4: Implement resolver**

In `gateway.py`, immediately after the `RANGE_VOLUME = {...}` dict (ends line 614) insert:

```python
_TIER_ORDER = ["15m", "1h", "6h", "24h", "7d", "30d", "60d"]
_TIER_SECONDS = {
    "15m": 900,
    "1h": 3600,
    "6h": 21600,
    "24h": 86400,
    "7d": 604800,
    "30d": 2592000,
    "60d": 5184000,
}


def _tier_for_span(span: int) -> str:
    """Smallest preset tier whose duration covers the given span (seconds)."""
    for key in _TIER_ORDER:
        if span <= _TIER_SECONDS[key]:
            return key
    return "60d"


@dataclass
class TimeWindow:
    promql_window: str        # increase() window for summary-type instant queries
    step: str                 # range_query step for non-volume series
    volume_step: str          # range_query step for volume series
    volume_window: str        # increase() window for volume series
    eval_time: float | None   # custom → end epoch; preset → None (= now)
    start: float | None       # custom → start epoch; preset → None
    end: float | None
    is_custom: bool


def _validate_custom_range(start: int, end: int) -> None:
    now = time.time()
    if end <= start:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start must be before end",
        )
    if end - start < 60:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="time range must span at least 60 seconds",
        )
    if end > now + 60:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="end must not be in the future",
        )


def resolve_time_window(
    time_range: str = Query(
        "1h", alias="range", description="Preset range: 15m, 1h, 6h, 24h, 7d, 30d, 60d"
    ),
    start: int | None = Query(None, description="Custom range start (epoch seconds)"),
    end: int | None = Query(None, description="Custom range end (epoch seconds)"),
) -> TimeWindow:
    if start is not None or end is not None:
        if start is None or end is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="both start and end are required for a custom range",
            )
        _validate_custom_range(start, end)
        span = end - start
        tier = _tier_for_span(span)
        vstep, vwindow = RANGE_VOLUME[tier]
        return TimeWindow(
            promql_window=f"{span}s",
            step=RANGE_STEPS[tier],
            volume_step=vstep,
            volume_window=vwindow,
            eval_time=float(end),
            start=float(start),
            end=float(end),
            is_custom=True,
        )
    if time_range not in VALID_RANGES:
        time_range = "1h"
    vstep, vwindow = RANGE_VOLUME[time_range]
    return TimeWindow(
        promql_window=time_range,
        step=RANGE_STEPS[time_range],
        volume_step=vstep,
        volume_window=vwindow,
        eval_time=None,
        start=None,
        end=None,
        is_custom=False,
    )
```

- [ ] **Step 5: Run tests, verify pass**

Run: `cd unibridge-service && python -m pytest tests/test_gateway.py::TestResolveTimeWindow -v`
Expected: 7 PASS.

- [ ] **Step 6: Commit**

```bash
git add unibridge-service/app/routers/gateway.py unibridge-service/tests/test_gateway.py
git commit -m "feat(monitoring): add TimeWindow resolver for preset and custom ranges"
```

---

## Task 3: Rewire route (gateway) metrics endpoints

Each endpoint changes the **same way**: replace the `time_range: str = Query(...)` parameter with `tw: TimeWindow = Depends(resolve_time_window)`, delete the `if time_range not in VALID_RANGES: time_range = "1h"` line, and update query calls:
- instant: `increase(metric[{time_range}])` → `increase(metric[{tw.promql_window}])`, and add `eval_time=tw.eval_time`. Plain `rate(...[5m])` snapshots gain `eval_time=tw.eval_time`.
- range (non-volume): add `start=tw.start, end=tw.end`; `duration=tw.promql_window`, `step=tw.step`.
- range (volume): window → `tw.volume_window`, `step=tw.volume_step`, `duration=tw.promql_window`, plus `start=tw.start, end=tw.end`.

`route` parameters and `_validate_route(route)` stay unchanged.

**Files:**
- Modify: `unibridge-service/app/routers/gateway.py:678-949`
- Test: `unibridge-service/tests/test_gateway.py`

- [ ] **Step 1: Write failing custom-range tests**

Append to `unibridge-service/tests/test_gateway.py`:

```python
class TestMetricsCustomRange:
    async def test_summary_custom_passes_eval_time(self, client, admin_token):
        scalar = [{"value": [1000, "5"]}]
        mock = AsyncMock(side_effect=[scalar, scalar, scalar])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/summary?start=1000000&end=1003600",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        # every instant_query call evaluated at end=1003600
        for call in mock.call_args_list:
            assert call.kwargs.get("eval_time") == 1003600.0

    async def test_requests_custom_passes_start_end(self, client, admin_token):
        mock = AsyncMock(return_value=[{"values": [[1000000, "1"]]}])
        with patch("app.routers.gateway.prometheus_client.range_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/requests?start=1000000&end=1003600",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert mock.call_args.kwargs.get("start") == 1000000.0
        assert mock.call_args.kwargs.get("end") == 1003600.0

    async def test_summary_rejects_reversed_custom_range(self, client, admin_token):
        resp = await client.get(
            "/admin/gateway/metrics/summary?start=2000&end=1000",
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `cd unibridge-service && python -m pytest tests/test_gateway.py::TestMetricsCustomRange -v`
Expected: FAIL (endpoints still ignore `start`/`end`; `eval_time`/`start`/`end` kwargs absent).

- [ ] **Step 3: Rewrite `metrics_summary` (lines 678-712)**

```python
@router.get("/metrics/summary")
async def metrics_summary(
    tw: TimeWindow = Depends(resolve_time_window),
    route: str | None = Query(None, description="Filter by route ID"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, Any]:
    _validate_route(route)
    hs = _labels(route)
    hs5 = _labels(route, 'code=~"5.."')
    try:
        total_results, error_rate_results, latency_results = await asyncio.gather(
            prometheus_client.instant_query(
                f"sum(increase(apisix_http_status{hs}[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum(rate(apisix_http_status{hs5}[5m])) / sum(rate(apisix_http_status{hs}[5m])) * 100",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum(rate(apisix_http_latency_sum{hs}[5m])) / sum(rate(apisix_http_latency_count{hs}[5m]))",
                eval_time=tw.eval_time,
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )

    return {
        "total_requests": round(_extract_scalar(total_results)),
        "error_rate": round(_extract_scalar(error_rate_results), 2),
        "avg_latency_ms": round(_extract_scalar(latency_results), 2),
    }
```

- [ ] **Step 4: Rewrite `metrics_requests` (lines 715-735)**

```python
@router.get("/metrics/requests")
async def metrics_requests(
    tw: TimeWindow = Depends(resolve_time_window),
    route: str | None = Query(None, description="Filter by route ID"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    _validate_route(route)
    hs = _labels(route)
    try:
        results = await prometheus_client.range_query(
            f"sum(rate(apisix_http_status{hs}[5m]))",
            duration=tw.promql_window,
            step=tw.step,
            start=tw.start,
            end=tw.end,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )
    return _extract_timeseries(results)
```

- [ ] **Step 5: Rewrite `metrics_status_codes` (lines 738-768)**

Change signature to `tw: TimeWindow = Depends(resolve_time_window)` + keep `route`, delete the guard line, and change the instant query:

```python
        results = await prometheus_client.instant_query(
            f"sum by (code) (increase(apisix_http_status{hs}[{tw.promql_window}]))",
            eval_time=tw.eval_time,
        )
```

(The codes-building loop below it is unchanged.)

- [ ] **Step 6: Rewrite `metrics_latency` (lines 771-809)**

Change signature + drop guard. Replace `step = _get_step(time_range)` with `step = tw.step`, and add `start`/`end` to each `range_query`:

```python
@router.get("/metrics/latency")
async def metrics_latency(
    tw: TimeWindow = Depends(resolve_time_window),
    route: str | None = Query(None, description="Filter by route ID"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, list[dict[str, Any]]]:
    _validate_route(route)
    hs = _labels(route)
    step = tw.step
    try:
        p50, p95, p99 = await asyncio.gather(
            prometheus_client.range_query(
                f"histogram_quantile(0.5, sum(rate(apisix_http_latency_bucket{hs}[5m])) by (le))",
                duration=tw.promql_window, step=step, start=tw.start, end=tw.end,
            ),
            prometheus_client.range_query(
                f"histogram_quantile(0.95, sum(rate(apisix_http_latency_bucket{hs}[5m])) by (le))",
                duration=tw.promql_window, step=step, start=tw.start, end=tw.end,
            ),
            prometheus_client.range_query(
                f"histogram_quantile(0.99, sum(rate(apisix_http_latency_bucket{hs}[5m])) by (le))",
                duration=tw.promql_window, step=step, start=tw.start, end=tw.end,
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )

    return {
        "p50": _extract_timeseries(p50),
        "p95": _extract_timeseries(p95),
        "p99": _extract_timeseries(p99),
    }
```

- [ ] **Step 7: Rewrite `metrics_top_routes` (lines 812-838)**

Signature → `tw: TimeWindow = Depends(resolve_time_window)` (no `route` param here), drop guard, change query:

```python
        results = await prometheus_client.instant_query(
            f"topk(10, sum by (route) (increase(apisix_http_status[{tw.promql_window}])))",
            eval_time=tw.eval_time,
        )
```

- [ ] **Step 8: Rewrite `metrics_routes_comparison` (lines 841-867 query block)**

Signature → `tw: TimeWindow = Depends(resolve_time_window)`, drop guard, update the 4 instant queries:

```python
        requests_res, errors_res, p50_res, p95_res = await asyncio.gather(
            prometheus_client.instant_query(
                f"topk(10, sum by (route) (increase(apisix_http_status[{tw.promql_window}])))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f'sum by (route) (increase(apisix_http_status{{code=~"5.."}}[{tw.promql_window}]))',
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                "histogram_quantile(0.5, sum by (route, le) (rate(apisix_http_latency_bucket[5m])))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                "histogram_quantile(0.95, sum by (route, le) (rate(apisix_http_latency_bucket[5m])))",
                eval_time=tw.eval_time,
            ),
        )
```

(Everything below — `_map_route_value`, name map, totals — is unchanged.)

- [ ] **Step 9: Rewrite `metrics_requests_total` (lines 927-949)**

```python
@router.get("/metrics/requests-total")
async def metrics_requests_total(
    tw: TimeWindow = Depends(resolve_time_window),
    route: str | None = Query(None, description="Filter by route ID"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    """Request volume per time bucket (total count, not rate)."""
    _validate_route(route)
    hs = _labels(route)
    try:
        results = await prometheus_client.range_query(
            f"sum(increase(apisix_http_status{hs}[{tw.volume_window}]))",
            duration=tw.promql_window,
            step=tw.volume_step,
            start=tw.start,
            end=tw.end,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )
    return _extract_timeseries(results)
```

- [ ] **Step 10: Run route-metric tests, verify pass**

Run: `cd unibridge-service && python -m pytest tests/test_gateway.py -k "Metrics" -v`
Expected: existing `TestMetricsSummary/Requests/StatusCodes/Latency/TopRoutes` PASS (preset behavior preserved) and new `TestMetricsCustomRange` PASS.

- [ ] **Step 11: Commit**

```bash
git add unibridge-service/app/routers/gateway.py unibridge-service/tests/test_gateway.py
git commit -m "feat(monitoring): wire gateway metrics endpoints to TimeWindow resolver"
```

---

## Task 4: Rewire LLM metrics endpoints

Same transformation recipe as Task 3. None of these take a `route` param.

**Files:**
- Modify: `unibridge-service/app/routers/gateway.py:955-1298`
- Test: `unibridge-service/tests/test_gateway.py`

- [ ] **Step 1: Write failing tests**

Append to `unibridge-service/tests/test_gateway.py`:

```python
class TestLlmMetricsCustomRange:
    async def test_llm_summary_custom_eval_time(self, client, admin_token):
        scalar = [{"value": [1000, "3"]}]
        mock = AsyncMock(side_effect=[scalar] * 7)
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/llm/summary?start=1000000&end=1003600",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        for call in mock.call_args_list:
            assert call.kwargs.get("eval_time") == 1003600.0

    async def test_llm_tokens_custom_start_end(self, client, admin_token):
        mock = AsyncMock(return_value=[{"values": [[1000000, "2"]]}])
        with patch("app.routers.gateway.prometheus_client.range_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/llm/tokens?start=1000000&end=1003600",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert mock.call_args.kwargs.get("start") == 1000000.0
        assert mock.call_args.kwargs.get("end") == 1003600.0
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `cd unibridge-service && python -m pytest tests/test_gateway.py::TestLlmMetricsCustomRange -v`
Expected: FAIL.

- [ ] **Step 3: Rewrite `llm_metrics_summary` (lines 955-1011)**

Signature → `tw: TimeWindow = Depends(resolve_time_window)`, drop guard. Add `eval_time=tw.eval_time` to all 7 instant queries; the 5 `increase(...[{time_range}])` become `increase(...[{tw.promql_window}])`; the 2 `rate(...[5m])` keep `[5m]` but add `eval_time=tw.eval_time`. Example for the first and the two rate ones:

```python
            prometheus_client.instant_query(
                f"sum(increase(litellm_total_tokens_metric_total[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            # ... prompt/completion/spend/requests identical pattern with tw.promql_window ...
            prometheus_client.instant_query(
                "sum(rate(litellm_request_total_latency_metric_sum[5m]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                "sum(rate(litellm_request_total_latency_metric_count[5m]))",
                eval_time=tw.eval_time,
            ),
```

Apply `[{time_range}]` → `[{tw.promql_window}]` + `eval_time=tw.eval_time` to all five `increase(...)` queries (`litellm_total_tokens_metric_total`, `litellm_input_tokens_metric_total`, `litellm_output_tokens_metric_total`, `litellm_spend_metric_total`, `litellm_proxy_total_requests_metric_total`). The scalar math below is unchanged.

- [ ] **Step 4: Rewrite `llm_metrics_tokens` (lines 1014-1044)**

```python
@router.get("/metrics/llm/tokens")
async def llm_metrics_tokens(
    tw: TimeWindow = Depends(resolve_time_window),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, list[dict[str, Any]]]:
    """Token usage trend: prompt and completion tokens over time."""
    try:
        prompt_results, completion_results = await asyncio.gather(
            prometheus_client.range_query(
                f"sum(increase(litellm_input_tokens_metric_total[{tw.volume_window}]))",
                duration=tw.promql_window, step=tw.volume_step, start=tw.start, end=tw.end,
            ),
            prometheus_client.range_query(
                f"sum(increase(litellm_output_tokens_metric_total[{tw.volume_window}]))",
                duration=tw.promql_window, step=tw.volume_step, start=tw.start, end=tw.end,
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )

    return {
        "prompt": _extract_timeseries(prompt_results),
        "completion": _extract_timeseries(completion_results),
    }
```

- [ ] **Step 5: Rewrite `llm_metrics_by_model` (lines 1047-1078 query block)**

Signature → `tw: TimeWindow = Depends(resolve_time_window)`, drop guard. Each of the 5 instant queries: `[{time_range}]` → `[{tw.promql_window}]`, add `eval_time=tw.eval_time`:

```python
            prometheus_client.instant_query(
                f"sum by (requested_model, model) (increase(litellm_total_tokens_metric_total[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (requested_model, model) (increase(litellm_input_tokens_metric_total[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (requested_model, model) (increase(litellm_output_tokens_metric_total[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (requested_model, model) (increase(litellm_spend_metric_total[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (requested_model, model) (increase(litellm_proxy_total_requests_metric_total[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
```

(The map-building code below is unchanged.)

- [ ] **Step 6: Rewrite `llm_metrics_top_keys` (lines 1154-1181 query block)**

Signature → resolver, drop guard. The 4 instant queries: `[{time_range}]` → `[{tw.promql_window}]`, add `eval_time=tw.eval_time`:

```python
            prometheus_client.instant_query(
                f"topk(10, sum by (end_user) (increase(litellm_total_tokens_metric_total[{tw.promql_window}])))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (end_user) (increase(litellm_input_tokens_metric_total[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (end_user) (increase(litellm_output_tokens_metric_total[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (end_user) (increase(litellm_proxy_total_requests_metric_total[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
```

(The map/keys-building code below is unchanged.)

- [ ] **Step 7: Rewrite `llm_metrics_errors` (lines 1236-1276)**

Signature → resolver, drop guard + drop `step, window = RANGE_VOLUME.get(...)`. Update both range queries:

```python
        success_results, error_results = await asyncio.gather(
            prometheus_client.range_query(
                f"sum(increase(litellm_proxy_total_requests_metric_total[{tw.volume_window}])) - sum(increase(litellm_proxy_failed_requests_metric_total[{tw.volume_window}]))",
                duration=tw.promql_window, step=tw.volume_step, start=tw.start, end=tw.end,
            ),
            prometheus_client.range_query(
                f"sum(increase(litellm_proxy_failed_requests_metric_total[{tw.volume_window}]))",
                duration=tw.promql_window, step=tw.volume_step, start=tw.start, end=tw.end,
            ),
        )
```

(The success/error combine loop below is unchanged.)

- [ ] **Step 8: Rewrite `llm_metrics_requests_total` (lines 1279-1298)**

```python
@router.get("/metrics/llm/requests-total")
async def llm_metrics_requests_total(
    tw: TimeWindow = Depends(resolve_time_window),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    """LLM request volume per time bucket."""
    try:
        results = await prometheus_client.range_query(
            f"sum(increase(litellm_proxy_total_requests_metric_total[{tw.volume_window}]))",
            duration=tw.promql_window,
            step=tw.volume_step,
            start=tw.start,
            end=tw.end,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )
    return _extract_timeseries(results)
```

- [ ] **Step 9: Run full gateway tests, verify pass**

Run: `cd unibridge-service && python -m pytest tests/test_gateway.py tests/test_gateway_extra.py tests/test_prometheus_client.py -v`
Expected: all PASS (existing preset tests + new custom tests). If a preset test that asserted `_get_step`/`RANGE_VOLUME` indirectly breaks, fix by confirming the new `tw.step`/`tw.volume_step` equals the old value for that preset.

- [ ] **Step 10: Commit**

```bash
git add unibridge-service/app/routers/gateway.py unibridge-service/tests/test_gateway.py
git commit -m "feat(monitoring): wire LLM metrics endpoints to TimeWindow resolver"
```

---

## Task 5: Frontend KST time helpers

**Files:**
- Modify: `unibridge-ui/src/utils/time.ts`
- Test: `unibridge-ui/src/test/time.test.ts`

- [ ] **Step 1: Write failing tests**

Append to `unibridge-ui/src/test/time.test.ts`:

```typescript
import {
  kstLocalToEpoch,
  epochToKstLocal,
  formatChartTime,
  formatChartTimestamp,
  formatKstChip,
} from '../utils/time';

describe('KST monitoring helpers', () => {
  // 2026-05-20 09:00 KST == 2026-05-20 00:00 UTC
  const epoch = Date.UTC(2026, 4, 20, 0, 0, 0) / 1000;

  it('kstLocalToEpoch interprets input as KST (+09:00)', () => {
    expect(kstLocalToEpoch('2026-05-20T09:00')).toBe(epoch);
  });

  it('epochToKstLocal round-trips', () => {
    expect(epochToKstLocal(epoch)).toBe('2026-05-20T09:00');
  });

  it('formatChartTime renders KST HH:mm regardless of host TZ', () => {
    expect(formatChartTime(epoch)).toBe('09:00');
  });

  it('formatChartTimestamp picks granularity by span', () => {
    expect(formatChartTimestamp(epoch, 3600)).toBe('09:00');            // <=24h
    expect(formatChartTimestamp(epoch, 2 * 86400)).toBe('5/20 09h');    // >24h, <=7d
    expect(formatChartTimestamp(epoch, 30 * 86400)).toBe('5/20');       // >7d
  });

  it('formatKstChip renders start~end', () => {
    const end = epoch + 2 * 86400 + 9 * 3600; // 5/22 18:00 KST
    expect(formatKstChip(epoch, end)).toBe('5/20 09:00~5/22 18:00');
  });
});
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `cd unibridge-ui && npx vitest run src/test/time.test.ts`
Expected: FAIL (functions not exported).

- [ ] **Step 3: Implement helpers**

Append to `unibridge-ui/src/utils/time.ts`:

```typescript
const KST_TZ = 'Asia/Seoul';

function kstParts(epochSeconds: number): Record<string, string> {
  const fmt = new Intl.DateTimeFormat('en-CA', {
    timeZone: KST_TZ,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
  const parts: Record<string, string> = {};
  for (const p of fmt.formatToParts(new Date(epochSeconds * 1000))) {
    if (p.type !== 'literal') parts[p.type] = p.value;
  }
  if (parts.hour === '24') parts.hour = '00'; // some engines emit 24 at midnight
  return parts;
}

/** epoch seconds → "HH:mm" in KST. */
export function formatChartTime(epochSeconds: number): string {
  const p = kstParts(epochSeconds);
  return `${p.hour}:${p.minute}`;
}

/** epoch seconds → span-aware axis label in KST. */
export function formatChartTimestamp(epochSeconds: number, spanSeconds: number): string {
  const p = kstParts(epochSeconds);
  if (spanSeconds > 7 * 86400) return `${Number(p.month)}/${Number(p.day)}`;
  if (spanSeconds > 86400) return `${Number(p.month)}/${Number(p.day)} ${p.hour}h`;
  return `${p.hour}:${p.minute}`;
}

/** Two epochs → "M/D HH:mm~M/D HH:mm" chip text in KST. */
export function formatKstChip(startSeconds: number, endSeconds: number): string {
  const s = kstParts(startSeconds);
  const e = kstParts(endSeconds);
  return (
    `${Number(s.month)}/${Number(s.day)} ${s.hour}:${s.minute}` +
    `~${Number(e.month)}/${Number(e.day)} ${e.hour}:${e.minute}`
  );
}

/** "YYYY-MM-DDTHH:mm" (datetime-local, interpreted as KST) → epoch seconds. */
export function kstLocalToEpoch(local: string): number {
  return Math.floor(Date.parse(`${local}:00+09:00`) / 1000);
}

/** epoch seconds → "YYYY-MM-DDTHH:mm" wall-clock string in KST (for datetime-local value). */
export function epochToKstLocal(epochSeconds: number): string {
  const p = kstParts(epochSeconds);
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
}
```

- [ ] **Step 4: Run tests, verify pass**

Run: `cd unibridge-ui && npx vitest run src/test/time.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add unibridge-ui/src/utils/time.ts unibridge-ui/src/test/time.test.ts
git commit -m "feat(monitoring): KST chart/datetime helpers"
```

---

## Task 6: `TimeSelection` model + API client signatures

**Files:**
- Create: `unibridge-ui/src/utils/timeRange.ts`
- Modify: `unibridge-ui/src/api/client.ts:479-596`
- Test: `unibridge-ui/src/test/client.api.test.ts:240-281`

- [ ] **Step 1: Create the pure `timeRange.ts` module**

Create `unibridge-ui/src/utils/timeRange.ts`:

```typescript
export const TIME_RANGES = ['15m', '1h', '6h', '24h', '7d', '30d', '60d'] as const;

export const PRESET_SECONDS: Record<string, number> = {
  '15m': 900,
  '1h': 3600,
  '6h': 21600,
  '24h': 86400,
  '7d': 604800,
  '30d': 2592000,
  '60d': 5184000,
};

export type TimeSelection =
  | { kind: 'preset'; value: string }
  | { kind: 'custom'; start: number; end: number }; // epoch seconds

export const DEFAULT_SELECTION: TimeSelection = { kind: 'preset', value: '1h' };

/** Query params for the metrics API: preset → {range}, custom → {start,end}. */
export function timeParams(sel: TimeSelection): Record<string, string | number> {
  return sel.kind === 'preset'
    ? { range: sel.value }
    : { start: sel.start, end: sel.end };
}

/** Stable react-query key fragment. */
export function selectionKey(sel: TimeSelection): string {
  return sel.kind === 'preset' ? `preset:${sel.value}` : `custom:${sel.start}-${sel.end}`;
}

/** Span in seconds (for chart-axis label granularity). */
export function selectionSpanSeconds(sel: TimeSelection): number {
  return sel.kind === 'preset' ? PRESET_SECONDS[sel.value] ?? 3600 : sel.end - sel.start;
}
```

- [ ] **Step 2: Update the failing client tests**

In `unibridge-ui/src/test/client.api.test.ts`, replace the body of the `'gateway metrics endpoints'` test (lines 240-258) with:

```typescript
  it('gateway metrics endpoints', async () => {
    const mod = await importClient(keycloak);
    const calls: Array<{ url?: string; params?: Record<string, unknown> }> = [];
    mod.default.defaults.adapter = makeAdapter((c) => {
      calls.push({ url: c.url, params: c.params });
      return {};
    });
    await mod.getMetricsSummary({ kind: 'preset', value: '6h' }, 'r1');
    await mod.getMetricsRequests({ kind: 'custom', start: 1000, end: 2000 });
    await mod.getMetricsStatusCodes({ kind: 'preset', value: '1h' }, 'r1');

    expect(calls[0]).toEqual({ url: '/admin/gateway/metrics/summary', params: { range: '6h', route: 'r1' } });
    expect(calls[1]).toEqual({ url: '/admin/gateway/metrics/requests', params: { start: 1000, end: 2000, route: undefined } });
    expect(calls[2].params).toEqual({ range: '1h', route: 'r1' });
  });
```

Leave the `'llm metrics endpoints'` test as-is for now — it calls with no args and relies on the default selection (Step 4 keeps a default).

- [ ] **Step 3: Run tests, verify they fail**

Run: `cd unibridge-ui && npx vitest run src/test/client.api.test.ts`
Expected: FAIL (`getMetricsSummary` still expects a string range; passing an object yields wrong params).

- [ ] **Step 4: Update client.ts**

At the top of `unibridge-ui/src/api/client.ts`, add the import (place near other imports):

```typescript
import { type TimeSelection, DEFAULT_SELECTION, timeParams } from '../utils/timeRange';
export type { TimeSelection } from '../utils/timeRange';
```

Replace each metric getter (lines 479-596) so the first arg is a `TimeSelection` defaulting to `DEFAULT_SELECTION` and params spread `timeParams(sel)`. Full replacements:

```typescript
export async function getMetricsSummary(sel: TimeSelection = DEFAULT_SELECTION, route?: string): Promise<MetricsSummary> {
  const { data } = await client.get('/admin/gateway/metrics/summary', { params: { ...timeParams(sel), route } });
  return data;
}

export async function getMetricsRequests(sel: TimeSelection = DEFAULT_SELECTION, route?: string): Promise<TimeSeriesPoint[]> {
  const { data } = await client.get('/admin/gateway/metrics/requests', { params: { ...timeParams(sel), route } });
  return data;
}

export async function getMetricsStatusCodes(sel: TimeSelection = DEFAULT_SELECTION, route?: string): Promise<StatusCodeData[]> {
  const { data } = await client.get('/admin/gateway/metrics/status-codes', { params: { ...timeParams(sel), route } });
  return data;
}

export async function getMetricsLatency(sel: TimeSelection = DEFAULT_SELECTION, route?: string): Promise<LatencyData> {
  const { data } = await client.get('/admin/gateway/metrics/latency', { params: { ...timeParams(sel), route } });
  return data;
}

export async function getMetricsTopRoutes(sel: TimeSelection = DEFAULT_SELECTION): Promise<TopRoute[]> {
  const { data } = await client.get('/admin/gateway/metrics/top-routes', { params: { ...timeParams(sel) } });
  return data;
}

export async function getMetricsRequestsTotal(sel: TimeSelection = DEFAULT_SELECTION, route?: string): Promise<TimeSeriesPoint[]> {
  const { data } = await client.get('/admin/gateway/metrics/requests-total', { params: { ...timeParams(sel), route } });
  return data;
}

export async function getMetricsRoutesComparison(sel: TimeSelection = DEFAULT_SELECTION): Promise<RouteComparisonResponse> {
  const { data } = await client.get('/admin/gateway/metrics/routes-comparison', { params: { ...timeParams(sel) } });
  return data;
}

export async function getLlmSummary(sel: TimeSelection = DEFAULT_SELECTION): Promise<LlmSummary> {
  const { data } = await client.get('/admin/gateway/metrics/llm/summary', { params: { ...timeParams(sel) } });
  return data;
}

export async function getLlmTokens(sel: TimeSelection = DEFAULT_SELECTION): Promise<LlmTokenSeries> {
  const { data } = await client.get('/admin/gateway/metrics/llm/tokens', { params: { ...timeParams(sel) } });
  return data;
}

export async function getLlmByModel(sel: TimeSelection = DEFAULT_SELECTION): Promise<LlmModelUsage[]> {
  const { data } = await client.get('/admin/gateway/metrics/llm/by-model', { params: { ...timeParams(sel) } });
  return data;
}

export async function getLlmTopKeys(sel: TimeSelection = DEFAULT_SELECTION): Promise<LlmKeyUsage[]> {
  const { data } = await client.get('/admin/gateway/metrics/llm/top-keys', { params: { ...timeParams(sel) } });
  return data;
}

export async function getLlmErrors(sel: TimeSelection = DEFAULT_SELECTION): Promise<LlmErrorPoint[]> {
  const { data } = await client.get('/admin/gateway/metrics/llm/errors', { params: { ...timeParams(sel) } });
  return data;
}

export async function getLlmRequestsTotal(sel: TimeSelection = DEFAULT_SELECTION): Promise<TimeSeriesPoint[]> {
  const { data } = await client.get('/admin/gateway/metrics/llm/requests-total', { params: { ...timeParams(sel) } });
  return data;
}
```

- [ ] **Step 5: Run tests, verify pass**

Run: `cd unibridge-ui && npx vitest run src/test/client.api.test.ts`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add unibridge-ui/src/utils/timeRange.ts unibridge-ui/src/api/client.ts unibridge-ui/src/test/client.api.test.ts
git commit -m "feat(monitoring): TimeSelection model and metric client signatures"
```

---

## Task 7: `TimeRangeSelector` component

**Files:**
- Create: `unibridge-ui/src/components/TimeRangeSelector.tsx`
- Create: `unibridge-ui/src/components/TimeRangeSelector.css`
- Test: `unibridge-ui/src/test/TimeRangeSelector.test.tsx`

- [ ] **Step 1: Write failing component test**

Create `unibridge-ui/src/test/TimeRangeSelector.test.tsx`:

```typescript
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi } from 'vitest';
import TimeRangeSelector from '../components/TimeRangeSelector';
import type { TimeSelection } from '../utils/timeRange';
import { renderWithProviders } from './helpers';

describe('TimeRangeSelector', () => {
  it('renders preset buttons and highlights the active one', () => {
    renderWithProviders(
      <TimeRangeSelector value={{ kind: 'preset', value: '1h' }} onChange={vi.fn()} />,
    );
    expect(screen.getByRole('button', { name: '15m' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '1h' })).toHaveClass('time-range-btn--active');
  });

  it('fires onChange with a preset when a preset button is clicked', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderWithProviders(
      <TimeRangeSelector value={{ kind: 'preset', value: '1h' }} onChange={onChange} />,
    );
    await user.click(screen.getByRole('button', { name: '6h' }));
    expect(onChange).toHaveBeenCalledWith({ kind: 'preset', value: '6h' });
  });

  it('opens the custom popover and applies a valid range as epoch seconds', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderWithProviders(
      <TimeRangeSelector value={{ kind: 'preset', value: '1h' }} onChange={onChange} />,
    );
    await user.click(screen.getByTestId('custom-toggle'));

    const start = screen.getByTestId('custom-start') as HTMLInputElement;
    const end = screen.getByTestId('custom-end') as HTMLInputElement;
    await user.clear(start);
    await user.type(start, '2026-05-20T09:00');
    await user.clear(end);
    await user.type(end, '2026-05-20T10:00');
    await user.click(screen.getByTestId('custom-apply'));

    expect(onChange).toHaveBeenCalledWith({
      kind: 'custom',
      start: Date.UTC(2026, 4, 20, 0, 0, 0) / 1000,
      end: Date.UTC(2026, 4, 20, 1, 0, 0) / 1000,
    });
  });

  it('disables apply when start is not before end', async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <TimeRangeSelector value={{ kind: 'preset', value: '1h' }} onChange={vi.fn()} />,
    );
    await user.click(screen.getByTestId('custom-toggle'));
    const start = screen.getByTestId('custom-start') as HTMLInputElement;
    const end = screen.getByTestId('custom-end') as HTMLInputElement;
    await user.clear(start);
    await user.type(start, '2026-05-20T10:00');
    await user.clear(end);
    await user.type(end, '2026-05-20T09:00');
    expect(screen.getByTestId('custom-apply')).toBeDisabled();
  });

  it('shows a chip for an active custom selection and clears it back to 1h', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const value: TimeSelection = {
      kind: 'custom',
      start: Date.UTC(2026, 4, 20, 0, 0, 0) / 1000,
      end: Date.UTC(2026, 4, 22, 9, 0, 0) / 1000,
    };
    renderWithProviders(<TimeRangeSelector value={value} onChange={onChange} />);
    expect(screen.getByText('5/20 09:00~5/22 18:00')).toBeInTheDocument();
    await user.click(screen.getByTestId('custom-clear'));
    expect(onChange).toHaveBeenCalledWith({ kind: 'preset', value: '1h' });
  });
});
```

- [ ] **Step 2: Run test, verify it fails**

Run: `cd unibridge-ui && npx vitest run src/test/TimeRangeSelector.test.tsx`
Expected: FAIL (module does not exist).

- [ ] **Step 3: Implement the component**

Create `unibridge-ui/src/components/TimeRangeSelector.tsx`:

```typescript
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { TIME_RANGES, type TimeSelection } from '../utils/timeRange';
import { kstLocalToEpoch, epochToKstLocal, formatKstChip } from '../utils/time';
import './TimeRangeSelector.css';

interface TimeRangeSelectorProps {
  value: TimeSelection;
  onChange: (next: TimeSelection) => void;
}

function defaultLocalRange(): { start: string; end: string } {
  const nowSec = Math.floor(Date.now() / 1000);
  return {
    start: epochToKstLocal(nowSec - 3600),
    end: epochToKstLocal(nowSec),
  };
}

function TimeRangeSelector({ value, onChange }: TimeRangeSelectorProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const initial = defaultLocalRange();
  const [startLocal, setStartLocal] = useState(
    value.kind === 'custom' ? epochToKstLocal(value.start) : initial.start,
  );
  const [endLocal, setEndLocal] = useState(
    value.kind === 'custom' ? epochToKstLocal(value.end) : initial.end,
  );

  const startEpoch = startLocal ? kstLocalToEpoch(startLocal) : NaN;
  const endEpoch = endLocal ? kstLocalToEpoch(endLocal) : NaN;
  const nowSec = Math.floor(Date.now() / 1000);
  const valid =
    Number.isFinite(startEpoch) &&
    Number.isFinite(endEpoch) &&
    startEpoch < endEpoch &&
    endEpoch <= nowSec + 60;

  const apply = () => {
    if (!valid) return;
    onChange({ kind: 'custom', start: startEpoch, end: endEpoch });
    setOpen(false);
  };

  const clearCustom = () => onChange({ kind: 'preset', value: '1h' });

  return (
    <div className="time-range-selector">
      <div className="time-range-toggle">
        {TIME_RANGES.map((r) => (
          <button
            key={r}
            className={`time-range-btn ${value.kind === 'preset' && value.value === r ? 'time-range-btn--active' : ''}`}
            onClick={() => onChange({ kind: 'preset', value: r })}
          >
            {r}
          </button>
        ))}
        {value.kind === 'custom' ? (
          <span className="time-range-chip">
            {formatKstChip(value.start, value.end)}
            <button
              type="button"
              className="time-range-chip__clear"
              data-testid="custom-clear"
              aria-label={t('timeRange.clear')}
              onClick={clearCustom}
            >
              ✕
            </button>
          </span>
        ) : (
          <button
            type="button"
            className="time-range-btn time-range-btn--custom"
            data-testid="custom-toggle"
            onClick={() => setOpen((o) => !o)}
          >
            {t('timeRange.custom')} ▾
          </button>
        )}
      </div>

      {open && (
        <div className="time-range-popover">
          <label className="time-range-field">
            <span>{t('timeRange.start')}</span>
            <input
              type="datetime-local"
              data-testid="custom-start"
              value={startLocal}
              onChange={(e) => setStartLocal(e.target.value)}
            />
          </label>
          <label className="time-range-field">
            <span>{t('timeRange.end')}</span>
            <input
              type="datetime-local"
              data-testid="custom-end"
              value={endLocal}
              onChange={(e) => setEndLocal(e.target.value)}
            />
          </label>
          {!valid && <div className="time-range-error">{t('timeRange.invalid')}</div>}
          <div className="time-range-actions">
            <button type="button" onClick={() => setOpen(false)}>
              {t('timeRange.cancel')}
            </button>
            <button type="button" data-testid="custom-apply" disabled={!valid} onClick={apply}>
              {t('timeRange.apply')}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default TimeRangeSelector;
```

- [ ] **Step 4: Add styles**

Create `unibridge-ui/src/components/TimeRangeSelector.css`:

```css
.time-range-selector {
  position: relative;
}

.time-range-btn--custom {
  white-space: nowrap;
}

.time-range-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 6px;
  background: var(--bg-tertiary, #2a2a2a);
  color: var(--text-primary, #eee);
  font-size: 12px;
  white-space: nowrap;
}

.time-range-chip__clear {
  border: none;
  background: transparent;
  color: inherit;
  cursor: pointer;
  font-size: 12px;
  line-height: 1;
  padding: 0;
}

.time-range-popover {
  position: absolute;
  right: 0;
  top: calc(100% + 6px);
  z-index: 20;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  border-radius: 8px;
  background: var(--bg-secondary, #1e1e1e);
  border: 1px solid var(--border-color, #333);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
}

.time-range-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--text-secondary, #aaa);
}

.time-range-error {
  color: var(--accent-red, #e5484d);
  font-size: 12px;
}

.time-range-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
```

- [ ] **Step 5: Add i18n keys (so the component renders text in tests)**

In `unibridge-ui/src/locales/en.json`, add a top-level `"timeRange"` object:

```json
  "timeRange": {
    "custom": "Custom",
    "start": "Start",
    "end": "End",
    "apply": "Apply",
    "cancel": "Cancel",
    "clear": "Clear custom range",
    "invalid": "Start must be before end and end cannot be in the future."
  },
```

In `unibridge-ui/src/locales/ko.json`, add:

```json
  "timeRange": {
    "custom": "커스텀",
    "start": "시작",
    "end": "종료",
    "apply": "적용",
    "cancel": "취소",
    "clear": "커스텀 기간 지우기",
    "invalid": "시작은 종료보다 빨라야 하며 종료는 미래일 수 없습니다."
  },
```

- [ ] **Step 6: Run test, verify pass**

Run: `cd unibridge-ui && npx vitest run src/test/TimeRangeSelector.test.tsx`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add unibridge-ui/src/components/TimeRangeSelector.tsx unibridge-ui/src/components/TimeRangeSelector.css unibridge-ui/src/locales/en.json unibridge-ui/src/locales/ko.json unibridge-ui/src/test/TimeRangeSelector.test.tsx
git commit -m "feat(monitoring): TimeRangeSelector with custom KST popover"
```

---

## Task 8: Wire `GatewayMonitoring` page

**Files:**
- Modify: `unibridge-ui/src/pages/GatewayMonitoring.tsx`
- Test: `unibridge-ui/src/test/GatewayMonitoring.test.tsx`

- [ ] **Step 1: Add a failing test for the custom toggle**

In `unibridge-ui/src/test/GatewayMonitoring.test.tsx`, the existing `'renders time range toggle buttons'` test stays valid. Add after it:

```typescript
  it('renders the custom range toggle', () => {
    renderWithProviders(<GatewayMonitoring />);
    expect(screen.getByTestId('custom-toggle')).toBeInTheDocument();
  });
```

- [ ] **Step 2: Run test, verify it fails**

Run: `cd unibridge-ui && npx vitest run src/test/GatewayMonitoring.test.tsx`
Expected: the new test FAILS (no `custom-toggle` yet).

- [ ] **Step 3: Update imports + state**

In `GatewayMonitoring.tsx`:

Replace the recharts/util imports header region. Remove the local `TIME_RANGES` const (line 22) and the `formatTime`/`formatTimestamp` functions (lines 24-38). Add imports near the top (after the existing `./GatewayMonitoring.css` import):

```typescript
import TimeRangeSelector from '../components/TimeRangeSelector';
import { type TimeSelection, selectionKey, selectionSpanSeconds } from '../utils/timeRange';
import { formatChartTime, formatChartTimestamp } from '../utils/time';
```

Replace `const [range, setRange] = useState('1h');` (line 109) with:

```typescript
  const [selection, setSelection] = useState<TimeSelection>({ kind: 'preset', value: '1h' });
  const selKey = selectionKey(selection);
  const span = selectionSpanSeconds(selection);
  const refetchInterval = selection.kind === 'custom' ? false : 30_000;
  const rangeLabel = selection.kind === 'preset' ? selection.value : t('gatewayMonitoring.customRange');
```

- [ ] **Step 4: Update every query to use selection**

For each `useQuery` in the file, change the `queryKey` `range` entry to `selKey`, the `queryFn` argument from `range` to `selection`, and `refetchInterval: 30_000` to `refetchInterval`. Apply this to all 10 queries: `summaryQuery`, `requestsQuery`, `statusQuery`, `latencyQuery`, `routesComparisonQuery`, `requestsTotalQuery`, `routeSummaryQuery`, `routeRequestsQuery`, `routeStatusQuery`, `routeVolumQuery`. Example:

```typescript
  const summaryQuery = useQuery({
    queryKey: ['metrics-summary', selKey],
    queryFn: () => getMetricsSummary(selection),
    refetchInterval,
  });
```

For the route drill-down queries that pass `selectedRoute!`, keep the second arg:

```typescript
  const routeSummaryQuery = useQuery({
    queryKey: ['metrics-summary', selKey, selectedRoute],
    queryFn: () => getMetricsSummary(selection, selectedRoute!),
    refetchInterval,
    enabled: !!selectedRoute,
  });
```

- [ ] **Step 5: Replace the toggle markup + label calls**

Replace the `<div className="time-range-toggle">…</div>` block (lines 255-265) with:

```tsx
        <TimeRangeSelector value={selection} onChange={setSelection} />
```

Replace chart-label calls:
- `formatTime(p.timestamp)` → `formatChartTime(p.timestamp)` (request trend line chart at lines 229, 469).
- `formatTimestamp(p.timestamp, range)` → `formatChartTimestamp(p.timestamp, span)` (volume bar charts at lines 322, 493).
- latency chart `formatTime(p.timestamp)` → `formatChartTime(p.timestamp)` (line 234... the `latencyChartData` map).

Replace `{ range }` interpolation in `totalRequests` labels (lines 277, 449) with `{ range: rangeLabel }`.

- [ ] **Step 6: Add the `customRange` i18n key**

In `unibridge-ui/src/locales/en.json` under `gatewayMonitoring`, add `"customRange": "custom"`. In `ko.json` under `gatewayMonitoring`, add `"customRange": "커스텀"`.

- [ ] **Step 7: Run tests + typecheck, verify pass**

Run: `cd unibridge-ui && npx vitest run src/test/GatewayMonitoring.test.tsx && npx tsc --noEmit`
Expected: PASS, no type errors.

- [ ] **Step 8: Commit**

```bash
git add unibridge-ui/src/pages/GatewayMonitoring.tsx unibridge-ui/src/test/GatewayMonitoring.test.tsx unibridge-ui/src/locales/en.json unibridge-ui/src/locales/ko.json
git commit -m "feat(monitoring): GatewayMonitoring custom range + KST labels"
```

---

## Task 9: Wire `LlmMonitoring` page

**Files:**
- Modify: `unibridge-ui/src/pages/LlmMonitoring.tsx`
- Test: `unibridge-ui/src/test/LlmMonitoring.test.tsx`

- [ ] **Step 1: Add a failing test for the custom toggle**

In `unibridge-ui/src/test/LlmMonitoring.test.tsx`, add:

```typescript
  it('renders the custom range toggle', () => {
    renderWithProviders(<LlmMonitoring />);
    expect(screen.getByTestId('custom-toggle')).toBeInTheDocument();
  });
```

(If the existing test file does not import `screen`/`renderWithProviders`, mirror the imports already used by its other tests.)

- [ ] **Step 2: Run test, verify it fails**

Run: `cd unibridge-ui && npx vitest run src/test/LlmMonitoring.test.tsx`
Expected: new test FAILS.

- [ ] **Step 3: Update imports + state**

In `LlmMonitoring.tsx`: remove local `TIME_RANGES` (line 20) and `formatTime`/`formatTimestamp` (lines 23-37). Add imports after `./LlmMonitoring.css`:

```typescript
import TimeRangeSelector from '../components/TimeRangeSelector';
import { type TimeSelection, selectionKey, selectionSpanSeconds } from '../utils/timeRange';
import { formatChartTimestamp } from '../utils/time';
```

Replace `const [range, setRange] = useState('1h');` (line 51) with:

```typescript
  const [selection, setSelection] = useState<TimeSelection>({ kind: 'preset', value: '1h' });
  const selKey = selectionKey(selection);
  const span = selectionSpanSeconds(selection);
  const refetchInterval = selection.kind === 'custom' ? false : 30_000;
  const rangeLabel = selection.kind === 'preset' ? selection.value : t('llmMonitoring.customRange');
```

- [ ] **Step 4: Update queries**

For all 6 queries (summary, tokens, byModel, topKeys, errors, requestsTotal): `queryKey` `range` → `selKey`, `queryFn` arg `range` → `selection`, `refetchInterval: 30_000` → `refetchInterval`. Example:

```typescript
  const summaryQuery = useQuery({
    queryKey: ['llm-summary', selKey],
    queryFn: () => getLlmSummary(selection),
    refetchInterval,
  });
```

- [ ] **Step 5: Replace toggle markup + labels**

Replace the `<div className="time-range-toggle">…</div>` block (lines 131-141) with:

```tsx
          <TimeRangeSelector value={selection} onChange={setSelection} />
```

Replace `formatTimestamp(p.timestamp, range)` (lines 93, 99, 203) with `formatChartTimestamp(p.timestamp, span)`. Replace `{ range }` in `totalTokens`/`totalRequests` labels (lines 154, 162) with `{ range: rangeLabel }`.

- [ ] **Step 6: Add `customRange` i18n key**

In `en.json` under `llmMonitoring` add `"customRange": "custom"`; in `ko.json` under `llmMonitoring` add `"customRange": "커스텀"`.

- [ ] **Step 7: Run tests + typecheck, verify pass**

Run: `cd unibridge-ui && npx vitest run src/test/LlmMonitoring.test.tsx && npx tsc --noEmit`
Expected: PASS, no type errors.

- [ ] **Step 8: Commit**

```bash
git add unibridge-ui/src/pages/LlmMonitoring.tsx unibridge-ui/src/test/LlmMonitoring.test.tsx unibridge-ui/src/locales/en.json unibridge-ui/src/locales/ko.json
git commit -m "feat(monitoring): LlmMonitoring custom range + KST labels"
```

---

## Task 10: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Backend suite**

Run: `cd unibridge-service && python -m pytest -q`
Expected: all pass. Investigate and fix any regression before proceeding.

- [ ] **Step 2: Frontend suite + typecheck + lint**

Run: `cd unibridge-ui && npx vitest run && npx tsc --noEmit && npm run lint`
Expected: all pass.

- [ ] **Step 3: Manual smoke (optional but recommended)**

With the stack running, open Gateway Monitoring, click `커스텀 ▾`, pick a start/end, Apply. Confirm the chip shows the KST range, charts reload, and the auto-refresh stops (network tab shows no 30s polling). Repeat on LLM Monitoring.

- [ ] **Step 4: Final commit (if Step 1/2 required fixes)**

```bash
git add -A
git commit -m "test(monitoring): fix regressions from custom time range"
```

---

## Notes for the implementer

- **Preset behavior must not change.** Every preset query string and step is identical to before; the resolver returns the same `RANGE_STEPS`/`RANGE_VOLUME` values for the 7 presets. If an existing test breaks, that's a signal the wiring diverged — fix the wiring, not the test (unless the test asserted an internal that legitimately moved).
- **`route` params stay.** Only the time parameters move into `resolve_time_window`.
- **KST is fixed +09:00, no DST** — `kstLocalToEpoch` relies on the literal `+09:00` offset; do not switch to `getHours()`/local-time math.
- **Epoch is the wire format.** The backend never sees a timezone; it only receives `start`/`end` epoch seconds. All KST logic lives in the frontend.
