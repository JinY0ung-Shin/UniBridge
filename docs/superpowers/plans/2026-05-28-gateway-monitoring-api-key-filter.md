# Gateway Monitoring — API Key Filter & llm-proxy Hiding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 게이트웨이 모니터링 페이지에 API key 드롭다운 필터를 추가하고 `llm-proxy` 라우트를 페이지 전체에서 숨긴다.

**Architecture:** APISIX prometheus 시리즈의 `consumer` 라벨을 이용해 페이지 전체 메트릭을 필터링한다. PromQL label selector 헬퍼(`_labels()`)에 `consumer` 인자와 `route!="llm-proxy"` default 셀렉터를 추가하면, 6개 메트릭 엔드포인트 모두에 한 번에 적용된다. 프론트는 page-header에 `<select>` 추가, 모든 `useQuery`에 `selectedConsumer`를 queryKey/queryFn으로 전파.

**Tech Stack:** FastAPI (Python), React + TypeScript, React Query, vitest, pytest, axios.

**Spec:** `docs/superpowers/specs/2026-05-28-gateway-monitoring-api-key-filter-design.md`

**Branch:** `feat/gateway-monitoring-api-key-filter` (already created)

---

## File Map

**Modify:**
- `unibridge-service/app/routers/gateway.py` — `_labels()`, `_validate_consumer()`, 6개 메트릭 엔드포인트 시그니처
- `unibridge-service/tests/test_gateway.py` — 새 테스트 + 기존 `test_no_route_filter_omits_label` 보강
- `unibridge-ui/src/api/client.ts` — 6개 메트릭 함수에 `consumer` 인자
- `unibridge-ui/src/pages/GatewayMonitoring.tsx` — state, dropdown UI, queryKey 확장
- `unibridge-ui/src/pages/GatewayMonitoring.css` — `.page-header__filters` flex 정렬
- `unibridge-ui/src/locales/ko.json` / `en.json` — 2개 i18n 키
- `unibridge-ui/src/test/GatewayMonitoring.test.tsx` — `getApiKeys` 모킹 + 새 테스트

---

## Task 1: `_validate_consumer()` + `_labels()` 시그니처 확장

**Files:**
- Modify: `unibridge-service/app/routers/gateway.py:710-729`
- Test: `unibridge-service/tests/test_gateway.py` (new test class near `_validate_route` tests if any, otherwise append)

- [ ] **Step 1: Write failing unit tests for `_labels` and `_validate_consumer`**

`unibridge-service/tests/test_gateway.py` 상단의 `from app.routers.gateway import (...)` 블록에 `_labels`, `_validate_consumer` 추가:

```python
from app.routers.gateway import (
    _extract_scalar,
    _extract_service_key,
    _extract_service_keys,
    _extract_timeseries,
    _get_step,
    _inject_plugins,
    _labels,
    _service_headers_for_route,
    _validate_consumer,
    # ... 기존 import 유지
)
```

파일 맨 아래(또는 헬퍼 함수 테스트가 모인 섹션)에 새 테스트 클래스 추가:

```python
class TestLabelsHelper:
    """_labels() builds PromQL label selectors with llm-proxy exclusion default."""

    def test_no_args_excludes_llm_proxy(self):
        assert _labels(None, None) == '{route!="llm-proxy"}'

    def test_route_replaces_llm_proxy_exclusion(self):
        # Explicit route filter should not include the llm-proxy exclusion
        assert _labels("query-api", None) == '{route="query-api"}'

    def test_consumer_adds_label(self):
        assert _labels(None, "alice") == '{route!="llm-proxy",consumer="alice"}'

    def test_route_and_consumer(self):
        assert _labels("query-api", "alice") == '{route="query-api",consumer="alice"}'

    def test_extra_labels_prepended(self):
        # Existing usage: _labels(route, None, 'code=~"5.."')
        assert _labels(None, None, 'code=~"5.."') == '{code=~"5..",route!="llm-proxy"}'
        assert _labels("query-api", "alice", 'code=~"5.."') == \
            '{code=~"5..",route="query-api",consumer="alice"}'


class TestValidateConsumer:
    def test_accepts_safe_names(self):
        for name in ("alice", "my-app", "user_1", "svc.prod", "ABC123"):
            _validate_consumer(name)  # no exception

    def test_none_is_allowed(self):
        _validate_consumer(None)

    def test_empty_string_is_allowed(self):
        # Empty string is falsy; treated like None
        _validate_consumer("")

    def test_rejects_unsafe_names(self):
        for bad in ('alice"; drop', "a b", "name/etc", "x\"y", "name;"):
            with pytest.raises(HTTPException) as ei:
                _validate_consumer(bad)
            assert ei.value.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd unibridge-service && pytest tests/test_gateway.py::TestLabelsHelper tests/test_gateway.py::TestValidateConsumer -v`
Expected: FAIL with `ImportError: cannot import name '_validate_consumer'` (and `_labels` signature mismatch).

- [ ] **Step 3: Implement `_labels()` extension and `_validate_consumer()`**

`unibridge-service/app/routers/gateway.py` 파일 안에서 `_SAFE_ROUTE_RE` 아래에 `_SAFE_CONSUMER_RE` 추가:

```python
_SAFE_ROUTE_RE = re.compile(r"^[a-zA-Z0-9_\-\.]+$")
_SAFE_CONSUMER_RE = re.compile(r"^[a-zA-Z0-9_\-\.]+$")
```

`_validate_route` 아래에 새 함수 추가:

```python
def _validate_consumer(consumer: str | None) -> None:
    if consumer and not _SAFE_CONSUMER_RE.match(consumer):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid consumer name"
        )
```

`_labels` 함수 전체를 교체:

```python
def _labels(route: str | None, consumer: str | None, *extra: str) -> str:
    """Build PromQL label selector.

    Defaults exclude the ``llm-proxy`` route so the gateway monitoring page
    omits LLM traffic (shown separately on the LLM monitoring page). When
    ``route`` is explicitly set, that filter replaces the default exclusion.
    """
    parts = list(extra)
    if route:
        parts.append(f'route="{route}"')
    else:
        parts.append('route!="llm-proxy"')
    if consumer:
        parts.append(f'consumer="{consumer}"')
    return "{" + ",".join(parts) + "}" if parts else ""
```

- [ ] **Step 4: Run unit tests to verify they pass**

Run: `cd unibridge-service && pytest tests/test_gateway.py::TestLabelsHelper tests/test_gateway.py::TestValidateConsumer -v`
Expected: PASS (all 9 tests).

- [ ] **Step 5: Commit**

```bash
git add unibridge-service/app/routers/gateway.py unibridge-service/tests/test_gateway.py
git commit -m "feat(gateway): _labels() accepts consumer + excludes llm-proxy by default

Adds _validate_consumer() and extends _labels() to take an optional
consumer label and, when no explicit route is given, inject the
route!=\"llm-proxy\" selector so the gateway monitoring page omits LLM
traffic (which is shown on the LLM monitoring page).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Update all `_labels()` call-sites to pass `consumer=None` (compile-fix)

After Task 1, every existing caller of `_labels` is broken because the signature added a required positional `consumer`. Step 3 of Task 1 already changed the function, so the unit tests pass — but the integration tests (`pytest tests/test_gateway.py -k "Metrics"`) will fail until call-sites are updated. This task makes them pass *without* yet exposing `consumer` to the HTTP layer; we'll wire that up in Task 3.

**Files:**
- Modify: `unibridge-service/app/routers/gateway.py` — every `_labels(route)` and `_labels(route, '...')` call

- [ ] **Step 1: Run existing integration tests to confirm breakage**

Run: `cd unibridge-service && pytest tests/test_gateway.py -k "Metrics or RouteFilter" -v 2>&1 | tail -40`
Expected: FAIL — `TypeError: _labels() missing 1 required positional argument: 'consumer'`

- [ ] **Step 2: Update all `_labels` call-sites in `gateway.py` to insert `None` as 2nd arg**

There are exactly 5 endpoints using `_labels` today: `metrics_summary`, `metrics_requests`, `metrics_status_codes`, `metrics_latency`, `metrics_requests_total`. Edit each call:

In `metrics_summary` (around line 779):
```python
    hs = _labels(route, None)
    hs5 = _labels(route, None, 'code=~"5.."')
```

In `metrics_requests` (around line 815):
```python
    hs = _labels(route, None)
```

In `metrics_status_codes` (around line 838):
```python
    hs = _labels(route, None)
```

In `metrics_latency` (around line 870):
```python
    hs = _labels(route, None)
```

In `metrics_requests_total` (around line 1023):
```python
    hs = _labels(route, None)
```

- [ ] **Step 3: Run integration tests to verify they pass**

Run: `cd unibridge-service && pytest tests/test_gateway.py -k "Metrics or RouteFilter" -v`

Expected: PASS for most. **One test will start failing**: `TestRouteFilter::test_no_route_filter_omits_label`.

Read the failure carefully. Look at the assertion:
```python
for call in mock.call_args_list:
    assert "route=" not in call.args[0]
```

The PromQL now contains `route!="llm-proxy"`. Note that `route!=` does NOT contain the substring `route=` (the `=` follows `!`, not `route`), so this assertion still passes — verify in the test output. If it does pass, no change needed for this test in this task. We'll add an explicit positive assertion in Task 4.

If the test fails: investigate before continuing. (The substring `route=` is 6 chars: r-o-u-t-e-=. In `route!="llm-proxy"` the 6 chars starting at index 0 are r-o-u-t-e-!. No match.)

- [ ] **Step 4: Commit**

```bash
git add unibridge-service/app/routers/gateway.py
git commit -m "refactor(gateway): pass consumer=None to _labels() at all call sites

Mechanical fix-up after _labels() signature change in previous commit.
Behaviour is unchanged for callers — consumer label is not yet exposed
to the HTTP layer (that comes next).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Expose `consumer` query parameter on 5 simple metrics endpoints

5 endpoints: `metrics_summary`, `metrics_requests`, `metrics_status_codes`, `metrics_latency`, `metrics_requests_total`.

**Files:**
- Modify: `unibridge-service/app/routers/gateway.py:772-1037`
- Test: `unibridge-service/tests/test_gateway.py` (new `TestConsumerFilter` class)

- [ ] **Step 1: Write failing integration tests**

Append a new test class to `tests/test_gateway.py` after `TestRouteFilter`:

```python
class TestConsumerFilter:
    """Verify the optional consumer query parameter filters PromQL correctly."""

    async def test_summary_with_consumer(self, client, admin_token):
        total = [{"value": [0, "10"]}]
        err = [{"value": [0, "0"]}]
        lat = [{"value": [0, "5"]}]
        mock = AsyncMock(side_effect=[total, err, lat])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/summary?range=1h&consumer=alice",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        for call in mock.call_args_list:
            assert 'consumer="alice"' in call.args[0]

    async def test_requests_with_consumer(self, client, admin_token):
        ts = [{"values": [[1000, "5"]]}]
        mock = AsyncMock(return_value=ts)
        with patch("app.routers.gateway.prometheus_client.range_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/requests?range=1h&consumer=alice",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert 'consumer="alice"' in mock.call_args.args[0]

    async def test_status_codes_with_consumer(self, client, admin_token):
        results = [{"metric": {"code": "200"}, "value": [0, "5"]}]
        mock = AsyncMock(return_value=results)
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/status-codes?range=1h&consumer=alice",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert 'consumer="alice"' in mock.call_args.args[0]

    async def test_latency_with_consumer(self, client, admin_token):
        p = [{"values": [[1000, "10"]]}]
        mock = AsyncMock(side_effect=[p, p, p])
        with patch("app.routers.gateway.prometheus_client.range_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/latency?range=1h&consumer=alice",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        for call in mock.call_args_list:
            assert 'consumer="alice"' in call.args[0]

    async def test_requests_total_with_consumer(self, client, admin_token):
        ts = [{"values": [[1000, "5"]]}]
        mock = AsyncMock(return_value=ts)
        with patch("app.routers.gateway.prometheus_client.range_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/requests-total?range=1h&consumer=alice",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert 'consumer="alice"' in mock.call_args.args[0]

    async def test_consumer_and_route_together(self, client, admin_token):
        total = [{"value": [0, "10"]}]
        err = [{"value": [0, "0"]}]
        lat = [{"value": [0, "5"]}]
        mock = AsyncMock(side_effect=[total, err, lat])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/summary?range=1h&route=query-api&consumer=alice",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        for call in mock.call_args_list:
            q = call.args[0]
            assert 'route="query-api"' in q
            assert 'consumer="alice"' in q
            # llm-proxy exclusion is replaced by explicit route filter
            assert 'route!="llm-proxy"' not in q

    async def test_invalid_consumer_returns_400(self, client, admin_token):
        resp = await client.get(
            '/admin/gateway/metrics/summary?range=1h&consumer="; drop',
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400

    async def test_no_consumer_excludes_llm_proxy(self, client, admin_token):
        total = [{"value": [0, "10"]}]
        err = [{"value": [0, "0"]}]
        lat = [{"value": [0, "5"]}]
        mock = AsyncMock(side_effect=[total, err, lat])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/summary?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        for call in mock.call_args_list:
            assert 'route!="llm-proxy"' in call.args[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd unibridge-service && pytest tests/test_gateway.py::TestConsumerFilter -v`
Expected: FAIL — most assertions fail because `consumer` query parameter isn't wired up yet. `test_no_consumer_excludes_llm_proxy` should already PASS thanks to Task 1; that's fine.

- [ ] **Step 3: Wire `consumer` query parameter into 5 endpoints**

Edit each of the 5 endpoints in `gateway.py`. Pattern is identical: add `consumer` `Query(...)` parameter, call `_validate_consumer`, replace `_labels(route, None, ...)` with `_labels(route, consumer, ...)`.

`metrics_summary` (around line 772):

```python
@router.get("/metrics/summary")
async def metrics_summary(
    tw: TimeWindow = Depends(resolve_time_window),
    route: str | None = Query(None, description="Filter by route ID"),
    consumer: str | None = Query(None, description="Filter by APISIX consumer name (API key)"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, Any]:
    _validate_route(route)
    _validate_consumer(consumer)
    hs = _labels(route, consumer)
    hs5 = _labels(route, consumer, 'code=~"5.."')
    # ... rest unchanged
```

`metrics_requests` (around line 808):

```python
@router.get("/metrics/requests")
async def metrics_requests(
    tw: TimeWindow = Depends(resolve_time_window),
    route: str | None = Query(None, description="Filter by route ID"),
    consumer: str | None = Query(None, description="Filter by APISIX consumer name (API key)"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    _validate_route(route)
    _validate_consumer(consumer)
    hs = _labels(route, consumer)
    # ... rest unchanged
```

`metrics_status_codes` (around line 831):

```python
@router.get("/metrics/status-codes")
async def metrics_status_codes(
    tw: TimeWindow = Depends(resolve_time_window),
    route: str | None = Query(None, description="Filter by route ID"),
    consumer: str | None = Query(None, description="Filter by APISIX consumer name (API key)"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    _validate_route(route)
    _validate_consumer(consumer)
    hs = _labels(route, consumer)
    # ... rest unchanged
```

`metrics_latency` (around line 863):

```python
@router.get("/metrics/latency")
async def metrics_latency(
    tw: TimeWindow = Depends(resolve_time_window),
    route: str | None = Query(None, description="Filter by route ID"),
    consumer: str | None = Query(None, description="Filter by APISIX consumer name (API key)"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, list[dict[str, Any]]]:
    _validate_route(route)
    _validate_consumer(consumer)
    hs = _labels(route, consumer)
    # ... rest unchanged
```

`metrics_requests_total` (around line 1015):

```python
@router.get("/metrics/requests-total")
async def metrics_requests_total(
    tw: TimeWindow = Depends(resolve_time_window),
    route: str | None = Query(None, description="Filter by route ID"),
    consumer: str | None = Query(None, description="Filter by APISIX consumer name (API key)"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    """Request volume per time bucket (total count, not rate)."""
    _validate_route(route)
    _validate_consumer(consumer)
    hs = _labels(route, consumer)
    # ... rest unchanged
```

- [ ] **Step 4: Run new + existing tests to verify all pass**

Run: `cd unibridge-service && pytest tests/test_gateway.py::TestConsumerFilter tests/test_gateway.py::TestRouteFilter -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add unibridge-service/app/routers/gateway.py unibridge-service/tests/test_gateway.py
git commit -m "feat(gateway): consumer query param on summary/requests/status/latency/requests-total

Exposes a 'consumer' query parameter on five gateway metric endpoints so
the UI can filter by APISIX consumer (i.e. API key name). When omitted,
the existing default selector route!=\"llm-proxy\" is applied to hide
LLM traffic from the gateway monitoring page.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Apply `_labels()` + consumer filter to `routes-comparison`

`routes-comparison` currently builds PromQL inline without `_labels` and without any label selector on `apisix_http_status`. We rewrite it so the same `route!="llm-proxy"` default + optional consumer filter apply, and PromQL uses the `hs` helper.

**Files:**
- Modify: `unibridge-service/app/routers/gateway.py:927-1012`
- Test: `unibridge-service/tests/test_gateway.py` (extend `TestConsumerFilter`)

- [ ] **Step 1: Write failing tests for routes-comparison**

Append to `TestConsumerFilter` class in `tests/test_gateway.py`:

```python
    async def test_routes_comparison_excludes_llm_proxy_by_default(self, client, admin_token):
        requests_result = [{"metric": {"route": "x"}, "value": [0, "10"]}]
        mock = AsyncMock(side_effect=[requests_result, [], [], []])
        list_mock = AsyncMock(return_value={"items": [], "total": 0})
        with patch("app.routers.gateway.prometheus_client.instant_query", mock), \
             patch("app.routers.gateway.apisix_client.list_resources", list_mock):
            resp = await client.get(
                "/admin/gateway/metrics/routes-comparison?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        # All 4 PromQL queries should include the llm-proxy exclusion
        for call in mock.call_args_list:
            assert 'route!="llm-proxy"' in call.args[0]

    async def test_routes_comparison_with_consumer(self, client, admin_token):
        requests_result = [{"metric": {"route": "x"}, "value": [0, "10"]}]
        mock = AsyncMock(side_effect=[requests_result, [], [], []])
        list_mock = AsyncMock(return_value={"items": [], "total": 0})
        with patch("app.routers.gateway.prometheus_client.instant_query", mock), \
             patch("app.routers.gateway.apisix_client.list_resources", list_mock):
            resp = await client.get(
                "/admin/gateway/metrics/routes-comparison?range=1h&consumer=alice",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        for call in mock.call_args_list:
            q = call.args[0]
            assert 'consumer="alice"' in q
            assert 'route!="llm-proxy"' in q

    async def test_routes_comparison_invalid_consumer_returns_400(self, client, admin_token):
        resp = await client.get(
            '/admin/gateway/metrics/routes-comparison?range=1h&consumer=bad name',
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd unibridge-service && pytest tests/test_gateway.py::TestConsumerFilter::test_routes_comparison_excludes_llm_proxy_by_default tests/test_gateway.py::TestConsumerFilter::test_routes_comparison_with_consumer tests/test_gateway.py::TestConsumerFilter::test_routes_comparison_invalid_consumer_returns_400 -v`
Expected: FAIL — current PromQL has no label selector and endpoint doesn't accept `consumer`.

- [ ] **Step 3: Refactor `routes-comparison` to use `_labels()`**

In `gateway.py`, replace the `metrics_routes_comparison` function's signature and the 4 PromQL strings. Before (around line 927):

```python
@router.get("/metrics/routes-comparison")
async def metrics_routes_comparison(
    tw: TimeWindow = Depends(resolve_time_window),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, Any]:
    """Per-route comparison: requests, share, error_rate, p50/p95 latency in one payload."""
    try:
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

After:

```python
@router.get("/metrics/routes-comparison")
async def metrics_routes_comparison(
    tw: TimeWindow = Depends(resolve_time_window),
    consumer: str | None = Query(None, description="Filter by APISIX consumer name (API key)"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, Any]:
    """Per-route comparison: requests, share, error_rate, p50/p95 latency in one payload."""
    _validate_consumer(consumer)
    # Routes-comparison never targets a single route — it groups by route.
    # Default selector hides llm-proxy (LLM monitoring page covers that).
    hs = _labels(None, consumer)
    hs5 = _labels(None, consumer, 'code=~"5.."')
    try:
        requests_res, errors_res, p50_res, p95_res = await asyncio.gather(
            prometheus_client.instant_query(
                f"topk(10, sum by (route) (increase(apisix_http_status{hs}[{tw.promql_window}])))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (route) (increase(apisix_http_status{hs5}[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"histogram_quantile(0.5, sum by (route, le) (rate(apisix_http_latency_bucket{hs}[5m])))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"histogram_quantile(0.95, sum by (route, le) (rate(apisix_http_latency_bucket{hs}[5m])))",
                eval_time=tw.eval_time,
            ),
        )
```

The rest of the function body (response building, `_map_route_value`, name_map, etc.) is unchanged.

- [ ] **Step 4: Run tests to verify all pass**

Run: `cd unibridge-service && pytest tests/test_gateway.py -k "Metrics or RouteFilter or ConsumerFilter or LabelsHelper or ValidateConsumer" -v`
Expected: ALL PASS. Watch for any pre-existing test of `routes-comparison` that asserted the literal string `apisix_http_status[` (without `{`) — none should exist, but inspect the failure output if anything regresses.

- [ ] **Step 5: Commit**

```bash
git add unibridge-service/app/routers/gateway.py unibridge-service/tests/test_gateway.py
git commit -m "feat(gateway): routes-comparison honors consumer filter & hides llm-proxy

Rewires the four PromQL queries in routes-comparison through _labels()
so the same consumer filter and route!=\"llm-proxy\" default applied
elsewhere also apply here.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Frontend API client — add `consumer` parameter

**Files:**
- Modify: `unibridge-ui/src/api/client.ts:481-529`

- [ ] **Step 1: Add `consumer` parameter to 6 metrics functions**

Edit `unibridge-ui/src/api/client.ts`. Replace the 6 metric-function signatures with:

```ts
export async function getMetricsSummary(
  sel: TimeSelection = DEFAULT_SELECTION,
  route?: string,
  consumer?: string,
): Promise<MetricsSummary> {
  const { data } = await client.get('/admin/gateway/metrics/summary', {
    params: { ...timeParams(sel), route, consumer },
  });
  return data;
}

export async function getMetricsRequests(
  sel: TimeSelection = DEFAULT_SELECTION,
  route?: string,
  consumer?: string,
): Promise<TimeSeriesPoint[]> {
  const { data } = await client.get('/admin/gateway/metrics/requests', {
    params: { ...timeParams(sel), route, consumer },
  });
  return data;
}

export async function getMetricsStatusCodes(
  sel: TimeSelection = DEFAULT_SELECTION,
  route?: string,
  consumer?: string,
): Promise<StatusCodeData[]> {
  const { data } = await client.get('/admin/gateway/metrics/status-codes', {
    params: { ...timeParams(sel), route, consumer },
  });
  return data;
}

export async function getMetricsLatency(
  sel: TimeSelection = DEFAULT_SELECTION,
  route?: string,
  consumer?: string,
): Promise<LatencyData> {
  const { data } = await client.get('/admin/gateway/metrics/latency', {
    params: { ...timeParams(sel), route, consumer },
  });
  return data;
}

export async function getMetricsRequestsTotal(
  sel: TimeSelection = DEFAULT_SELECTION,
  route?: string,
  consumer?: string,
): Promise<TimeSeriesPoint[]> {
  const { data } = await client.get('/admin/gateway/metrics/requests-total', {
    params: { ...timeParams(sel), route, consumer },
  });
  return data;
}

export async function getMetricsRoutesComparison(
  sel: TimeSelection = DEFAULT_SELECTION,
  consumer?: string,
): Promise<RouteComparisonResponse> {
  const { data } = await client.get('/admin/gateway/metrics/routes-comparison', {
    params: { ...timeParams(sel), consumer },
  });
  return data;
}
```

Note: axios omits `params` keys whose value is `undefined`, so existing callers without `consumer` keep working.

- [ ] **Step 2: Type-check**

Run: `cd unibridge-ui && npx tsc --noEmit`
Expected: no errors. (If any TS errors appear in `pages/GatewayMonitoring.tsx`, that's expected and will be fixed in Task 7.)

- [ ] **Step 3: Commit**

```bash
git add unibridge-ui/src/api/client.ts
git commit -m "feat(ui): add optional consumer param to gateway metric clients

axios omits undefined params, so existing call sites are unaffected.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: i18n keys

**Files:**
- Modify: `unibridge-ui/src/locales/ko.json:302-327`
- Modify: `unibridge-ui/src/locales/en.json:302-327`

- [ ] **Step 1: Add two keys to `ko.json`**

In `unibridge-ui/src/locales/ko.json`, inside the `gatewayMonitoring` object, add (place them right after `"customRange": "커스텀",`):

```json
    "apiKeyFilter": "API 키",
    "allApiKeys": "전체",
```

- [ ] **Step 2: Add two keys to `en.json`**

In `unibridge-ui/src/locales/en.json`, inside the `gatewayMonitoring` object:

```json
    "apiKeyFilter": "API Key",
    "allApiKeys": "All",
```

- [ ] **Step 3: Verify JSON parses**

Run: `cd unibridge-ui && node -e "JSON.parse(require('fs').readFileSync('src/locales/ko.json')); JSON.parse(require('fs').readFileSync('src/locales/en.json')); console.log('ok')"`
Expected: prints `ok`.

- [ ] **Step 4: Commit**

```bash
git add unibridge-ui/src/locales/ko.json unibridge-ui/src/locales/en.json
git commit -m "i18n(gateway-monitoring): apiKeyFilter and allApiKeys labels

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: GatewayMonitoring page — dropdown UI + consumer state

**Files:**
- Modify: `unibridge-ui/src/pages/GatewayMonitoring.tsx`
- Modify: `unibridge-ui/src/pages/GatewayMonitoring.css`
- Modify: `unibridge-ui/src/test/GatewayMonitoring.test.tsx`

This is the largest single task. We update tests first (TDD), update the test mocks for the existing imports, then implement.

- [ ] **Step 1: Update test mock to include `getApiKeys`**

In `unibridge-ui/src/test/GatewayMonitoring.test.tsx`, edit the `vi.mock` block at the top (around line 15):

```ts
vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getMetricsSummary: vi.fn(),
  getMetricsRequests: vi.fn(),
  getMetricsStatusCodes: vi.fn(),
  getMetricsLatency: vi.fn(),
  getMetricsRoutesComparison: vi.fn(),
  getMetricsRequestsTotal: vi.fn(),
  getApiKeys: vi.fn(),
}));
```

In the imports block (around line 27) add `getApiKeys`:

```ts
import {
  getMetricsSummary,
  getMetricsRequests,
  getMetricsStatusCodes,
  getMetricsLatency,
  getMetricsRoutesComparison,
  getMetricsRequestsTotal,
  getApiKeys,
} from '../api/client';
```

After the existing `mockedGetMetricsRequestsTotal`:

```ts
const mockedGetApiKeys = vi.mocked(getApiKeys);
```

In the `beforeEach` block, add a default mock:

```ts
    mockedGetApiKeys.mockResolvedValue([]);
```

- [ ] **Step 2: Add failing tests for dropdown behavior**

Append to the `describe('GatewayMonitoring', ...)` block in `unibridge-ui/src/test/GatewayMonitoring.test.tsx`:

```tsx
  it('renders the API key filter dropdown with "All" default', async () => {
    mockedGetApiKeys.mockResolvedValue([
      { name: 'alice', description: '', api_key: null, key_created: true, allowed_databases: [], allowed_routes: [], created_at: null },
      { name: 'bob',   description: '', api_key: null, key_created: true, allowed_databases: [], allowed_routes: [], created_at: null },
    ]);

    renderWithProviders(<GatewayMonitoring />);

    const select = await screen.findByLabelText(/API Key/i) as HTMLSelectElement;
    expect(select.value).toBe('');                       // 기본 = 전체 (빈 문자열)
    expect(screen.getByRole('option', { name: 'All' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'alice' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'bob' })).toBeInTheDocument();
  });

  it('passes selected consumer to metric calls when changed', async () => {
    const userEvent = (await import('@testing-library/user-event')).default;
    const user = userEvent.setup();

    mockedGetApiKeys.mockResolvedValue([
      { name: 'alice', description: '', api_key: null, key_created: true, allowed_databases: [], allowed_routes: [], created_at: null },
    ]);

    renderWithProviders(<GatewayMonitoring />);

    const select = await screen.findByLabelText(/API Key/i) as HTMLSelectElement;
    await user.selectOptions(select, 'alice');

    await waitFor(() => {
      // Find the most recent call with consumer == 'alice'
      const calls = mockedGetMetricsSummary.mock.calls;
      const hasConsumer = calls.some((args) => args[2] === 'alice');
      expect(hasConsumer).toBe(true);
    });

    // Routes-comparison has consumer as 2nd arg (no route arg)
    await waitFor(() => {
      const calls = mockedGetMetricsRoutesComparison.mock.calls;
      const hasConsumer = calls.some((args) => args[1] === 'alice');
      expect(hasConsumer).toBe(true);
    });
  });

  it('omits consumer parameter when "All" is selected (default)', async () => {
    renderWithProviders(<GatewayMonitoring />);

    await waitFor(() => {
      expect(mockedGetMetricsSummary).toHaveBeenCalled();
    });

    // Every call so far should have undefined consumer (3rd arg)
    for (const args of mockedGetMetricsSummary.mock.calls) {
      expect(args[2]).toBeUndefined();
    }
  });
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd unibridge-ui && npx vitest run src/test/GatewayMonitoring.test.tsx`
Expected: New 3 tests FAIL (no dropdown rendered; consumer not propagated). Existing tests should still PASS.

- [ ] **Step 4: Edit `GatewayMonitoring.tsx` — add state, query, dropdown, propagate consumer**

In `unibridge-ui/src/pages/GatewayMonitoring.tsx`, update the imports block at the top:

```ts
import {
  getMetricsSummary,
  getMetricsRequests,
  getMetricsRequestsTotal,
  getMetricsStatusCodes,
  getMetricsLatency,
  getMetricsRoutesComparison,
  getApiKeys,
  type RouteComparisonRow,
} from '../api/client';
```

Inside `function GatewayMonitoring()`, after the existing `useState<{ column: SortColumn; dir: SortDir }>` declaration, add:

```ts
  const [selectedConsumer, setSelectedConsumer] = useState<string>('');
```

Right above the existing `summaryQuery`, add the api-keys query:

```ts
  const { permissions, loaded: permissionsLoaded } = usePermissions();
  const canReadApiKeys = permissionsLoaded && permissions.includes('apikeys.read');

  const apiKeysQuery = useQuery({
    queryKey: ['api-keys', 'gateway-monitoring-filter'],
    queryFn: getApiKeys,
    staleTime: 5 * 60 * 1000,
    refetchInterval: false,
    enabled: canReadApiKeys,
  });
  const apiKeyOptions = useMemo(() => {
    const items = apiKeysQuery.data ?? [];
    return [...items].sort((a, b) => a.name.localeCompare(b.name));
  }, [apiKeysQuery.data]);
```

Render the API key dropdown only when `canReadApiKeys` is true. Monitoring-only users keep the default “All” behavior and do not call `/admin/api-keys`.

Update every metrics `useQuery` to include `selectedConsumer` in `queryKey` and pass it as the appropriate argument. The full set of updates:

```ts
  const summaryQuery = useQuery({
    queryKey: ['metrics-summary', selKey, selectedConsumer],
    queryFn: () => getMetricsSummary(selection, undefined, selectedConsumer || undefined),
    refetchInterval,
  });

  const requestsQuery = useQuery({
    queryKey: ['metrics-requests', selKey, selectedConsumer],
    queryFn: () => getMetricsRequests(selection, undefined, selectedConsumer || undefined),
    refetchInterval,
  });

  const statusQuery = useQuery({
    queryKey: ['metrics-status-codes', selKey, selectedConsumer],
    queryFn: () => getMetricsStatusCodes(selection, undefined, selectedConsumer || undefined),
    refetchInterval,
  });

  const latencyQuery = useQuery({
    queryKey: ['metrics-latency', selKey, selectedConsumer],
    queryFn: () => getMetricsLatency(selection, undefined, selectedConsumer || undefined),
    refetchInterval,
  });

  const routesComparisonQuery = useQuery({
    queryKey: ['metrics-routes-comparison', selKey, selectedConsumer],
    queryFn: () => getMetricsRoutesComparison(selection, selectedConsumer || undefined),
    refetchInterval,
  });

  const requestsTotalQuery = useQuery({
    queryKey: ['metrics-requests-total', selKey, selectedConsumer],
    queryFn: () => getMetricsRequestsTotal(selection, undefined, selectedConsumer || undefined),
    refetchInterval,
  });

  // Route drill-down queries
  const routeSummaryQuery = useQuery({
    queryKey: ['metrics-summary', selKey, selectedRoute, selectedConsumer],
    queryFn: () => getMetricsSummary(selection, selectedRoute!, selectedConsumer || undefined),
    refetchInterval,
    enabled: !!selectedRoute,
  });

  const routeRequestsQuery = useQuery({
    queryKey: ['metrics-requests', selKey, selectedRoute, selectedConsumer],
    queryFn: () => getMetricsRequests(selection, selectedRoute!, selectedConsumer || undefined),
    refetchInterval,
    enabled: !!selectedRoute,
  });

  const routeStatusQuery = useQuery({
    queryKey: ['metrics-status-codes', selKey, selectedRoute, selectedConsumer],
    queryFn: () => getMetricsStatusCodes(selection, selectedRoute!, selectedConsumer || undefined),
    refetchInterval,
    enabled: !!selectedRoute,
  });

  const routeVolumQuery = useQuery({
    queryKey: ['metrics-requests-total', selKey, selectedRoute, selectedConsumer],
    queryFn: () => getMetricsRequestsTotal(selection, selectedRoute!, selectedConsumer || undefined),
    refetchInterval,
    enabled: !!selectedRoute,
  });
```

Replace the existing page-header `<div>`:

```tsx
      <div className="page-header">
        <div>
          <h1>{t('gatewayMonitoring.title')}</h1>
          <p className="page-subtitle">{t('gatewayMonitoring.subtitle')}</p>
        </div>
        <TimeRangeSelector value={selection} onChange={setSelection} />
      </div>
```

with:

```tsx
      <div className="page-header">
        <div>
          <h1>{t('gatewayMonitoring.title')}</h1>
          <p className="page-subtitle">{t('gatewayMonitoring.subtitle')}</p>
        </div>
        <div className="page-header__filters">
          <label className="api-key-filter">
            <span className="api-key-filter__label">{t('gatewayMonitoring.apiKeyFilter')}</span>
            <select
              className="api-key-filter__select"
              value={selectedConsumer}
              onChange={(e) => setSelectedConsumer(e.target.value)}
            >
              <option value="">{t('gatewayMonitoring.allApiKeys')}</option>
              {apiKeyOptions.map((k) => (
                <option key={k.name} value={k.name}>{k.name}</option>
              ))}
            </select>
          </label>
          <TimeRangeSelector value={selection} onChange={setSelection} />
        </div>
      </div>
```

- [ ] **Step 5: Add CSS for the filter container and select**

Append to `unibridge-ui/src/pages/GatewayMonitoring.css`:

```css
.page-header__filters {
  display: flex;
  gap: 16px;
  align-items: center;
}

.api-key-filter {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text-secondary);
}

.api-key-filter__select {
  background: var(--bg-secondary);
  color: var(--text-primary);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 13px;
  cursor: pointer;
}

.api-key-filter__select:focus {
  outline: none;
  border-color: var(--accent-blue);
}
```

- [ ] **Step 6: Run frontend tests to verify they pass**

Run: `cd unibridge-ui && npx vitest run src/test/GatewayMonitoring.test.tsx`
Expected: ALL PASS (existing + 3 new).

- [ ] **Step 7: Type-check**

Run: `cd unibridge-ui && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add unibridge-ui/src/pages/GatewayMonitoring.tsx unibridge-ui/src/pages/GatewayMonitoring.css unibridge-ui/src/test/GatewayMonitoring.test.tsx
git commit -m "feat(ui): API key filter dropdown on gateway monitoring page

Adds a page-wide consumer filter in the header next to the time range
selector. Default 'All' omits the consumer query parameter so the
backend serves unfiltered (but still llm-proxy-excluded) data.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Verification

- [ ] **Step 1: Run the full backend test suite**

Run: `cd unibridge-service && pytest tests/test_gateway.py -v 2>&1 | tail -30`
Expected: ALL PASS. If anything regresses outside the gateway file, investigate.

- [ ] **Step 2: Run the full frontend test suite**

Run: `cd unibridge-ui && npx vitest run`
Expected: ALL PASS.

- [ ] **Step 3: Type-check the UI**

Run: `cd unibridge-ui && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Lint (if configured)**

Run: `cd unibridge-ui && npx eslint src/pages/GatewayMonitoring.tsx src/api/client.ts`
Expected: no errors. If ESLint isn't configured the way I expect, skip with a note.

- [ ] **Step 5: Manual smoke test (if stack is running)**

If `docker-compose up` is feasible: open the gateway monitoring page in the browser, verify:
- API key dropdown appears in the page header
- Default is "All" / "전체"; llm-proxy row is NOT in the comparison table
- Selecting an API key causes all charts and the comparison table to re-render
- Switching back to "All" restores the full view (sans llm-proxy)
- Direct API call `curl '...metrics/summary?range=1h&route=llm-proxy'` still returns data (backdoor preserved)

If the stack isn't running, document this in the PR description as a manual-verification step for the reviewer.

- [ ] **Step 6: Push and open PR**

```bash
git push -u origin feat/gateway-monitoring-api-key-filter
gh pr create --base main --title "feat(gateway-monitoring): API key filter & hide llm-proxy" \
  --body "$(cat <<'EOF'
## Summary
- Adds an API key dropdown filter to the gateway monitoring page header. Default 전체.
- Filter applies page-wide (summary cards, all charts, route comparison table, route drill-down).
- Hides the \`llm-proxy\` route from the gateway monitoring page (it's shown on the LLM monitoring page).

## How
- Backend: \`_labels()\` helper extended to accept a \`consumer\` argument and to inject \`route!=\"llm-proxy\"\` by default when no explicit route is requested. \`_validate_consumer()\` mirrors \`_validate_route()\`. Six metric endpoints gain an optional \`consumer\` query parameter.
- Frontend: page-header gains a \`<select>\` populated from \`/admin/api-keys\`; selection is propagated to every \`useQuery\` via \`queryKey\` and \`queryFn\`.

## Tests
- New backend tests: \`TestLabelsHelper\`, \`TestValidateConsumer\`, \`TestConsumerFilter\` (incl. routes-comparison cases).
- New frontend tests: dropdown rendering, consumer propagation, default \"전체\" behavior.

## Notes
- Direct API call \`?route=llm-proxy\` still works (intentional backdoor for power users / scripts).
- \`llm-admin\` is NOT hidden — only \`llm-proxy\`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

- **Spec coverage**: Every spec section mapped to tasks (helpers → Task 1, 5 endpoints → Task 3, routes-comparison → Task 4, API client → Task 5, page UI → Task 7, i18n → Task 6, tests → Tasks 1/3/4/7, edge cases verified in Task 8).
- **Placeholder scan**: No TBDs, no "implement later". Every code step contains actual code.
- **Type consistency**: `_labels(route, consumer, *extra)` consistent everywhere. `selectedConsumer || undefined` pattern consistent across all 10 useQuery sites. i18n keys (`apiKeyFilter`, `allApiKeys`) used consistently in Tasks 6/7.
- **Argument-position care**: `getMetricsSummary(sel, route, consumer)` vs `getMetricsRoutesComparison(sel, consumer)` — routes-comparison has no route arg, so consumer is 2nd. Tests in Task 7 step 2 reflect this (`args[1]` vs `args[2]`).
