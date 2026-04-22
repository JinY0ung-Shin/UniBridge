# 게이트웨이 라우트 비교 뷰 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gateway 모니터링 페이지의 Top Routes 섹션을 다중 지표 비교 테이블로 교체한다. 한 테이블에서 요청량, 점유율, 에러율, p50/p95 레이턴시를 정렬/시각 강조와 함께 비교할 수 있게 한다.

**Architecture:** 백엔드에 단일 집약 엔드포인트 `/admin/gateway/metrics/routes-comparison`를 추가해 네 개의 PromQL 쿼리를 `asyncio.gather`로 병렬 실행한 뒤 `route` 라벨 기준으로 조인해 반환한다. 프론트엔드는 기존 `topRoutesQuery`를 새 쿼리로 교체하고 동일한 `chart-panel` 자리에 정렬 가능한 테이블을 그린다. inline 막대바는 셀 내 `<span>` 배경, heatmap은 CSS 클래스 조건부 적용.

**Tech Stack:** FastAPI + Prometheus HTTP API (backend), React 18 + TanStack Query + recharts + i18next (frontend), pytest + vitest (tests).

**Spec:** `docs/superpowers/specs/2026-04-22-gateway-route-comparison-design.md`

---

## 파일 구조

**백엔드:**
- Modify: `unibridge-service/app/routers/gateway.py` — 새 엔드포인트 함수 `metrics_routes_comparison` 추가 (기존 `metrics_top_routes` 바로 아래)
- Modify: `unibridge-service/tests/test_gateway.py` — 새 테스트 클래스 `TestMetricsRoutesComparison` 추가

**프론트엔드:**
- Modify: `unibridge-ui/src/api/client.ts` — 타입 `RouteComparisonRow`, `RouteComparisonResponse`와 함수 `getMetricsRoutesComparison` 추가
- Modify: `unibridge-ui/src/pages/GatewayMonitoring.tsx` — 기존 Top Routes 섹션 교체, 정렬/inline bar/heatmap 로직 추가
- Modify: `unibridge-ui/src/pages/GatewayMonitoring.css` — `.comparison-table`, `.inline-bar`, `.heatmap-cell-*` 스타일 추가
- Modify: `unibridge-ui/src/locales/ko.json`, `unibridge-ui/src/locales/en.json` — `routeComparison`, `share`, `latencyP50`, `latencyP95` 키 추가
- Modify: `unibridge-ui/src/test/GatewayMonitoring.test.tsx` — 기존 `getMetricsTopRoutes` mock을 `getMetricsRoutesComparison`으로 교체, 새 테스트 케이스 추가

---

## Task 1: 백엔드 — 비교 엔드포인트 기본 구조 + 요청 수

**Files:**
- Modify: `unibridge-service/app/routers/gateway.py` (새 함수, `metrics_top_routes` 아래)
- Modify: `unibridge-service/tests/test_gateway.py` (새 테스트 클래스)

- [ ] **Step 1: 실패 테스트 작성 — 요청 수 조인과 응답 형태**

`unibridge-service/tests/test_gateway.py`의 `TestMetricsTopRoutes` 클래스 바로 아래에 다음 클래스 추가:

```python
class TestMetricsRoutesComparison:
    async def test_returns_joined_route_metrics(self, client, admin_token):
        requests_result = [
            {"metric": {"route": "route-a"}, "value": [0, "1000"]},
            {"metric": {"route": "route-b"}, "value": [0, "500"]},
        ]
        errors_result = [
            {"metric": {"route": "route-a"}, "value": [0, "10"]},
        ]
        p50_result = [
            {"metric": {"route": "route-a"}, "value": [0, "42.5"]},
            {"metric": {"route": "route-b"}, "value": [0, "30.0"]},
        ]
        p95_result = [
            {"metric": {"route": "route-a"}, "value": [0, "180.0"]},
            {"metric": {"route": "route-b"}, "value": [0, "60.0"]},
        ]

        mock = AsyncMock(side_effect=[requests_result, errors_result, p50_result, p95_result])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/routes-comparison?range=1h",
                headers=auth_header(admin_token),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["total_requests"] == 1500
        assert len(data["routes"]) == 2
        by_route = {r["route"]: r for r in data["routes"]}

        a = by_route["route-a"]
        assert a["requests"] == 1000
        assert a["share"] == pytest.approx(66.67, rel=0.01)
        assert a["error_rate"] == pytest.approx(1.0, rel=0.01)
        assert a["latency_p50_ms"] == pytest.approx(42.5)
        assert a["latency_p95_ms"] == pytest.approx(180.0)

        b = by_route["route-b"]
        assert b["requests"] == 500
        assert b["error_rate"] == 0.0  # no entry in errors_result → 0
        assert b["latency_p50_ms"] == pytest.approx(30.0)

    async def test_routes_sorted_by_requests_desc(self, client, admin_token):
        requests_result = [
            {"metric": {"route": "small"}, "value": [0, "100"]},
            {"metric": {"route": "big"}, "value": [0, "900"]},
        ]
        mock = AsyncMock(side_effect=[requests_result, [], [], []])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/routes-comparison?range=1h",
                headers=auth_header(admin_token),
            )
        data = resp.json()
        assert [r["route"] for r in data["routes"]] == ["big", "small"]
```

- [ ] **Step 2: 테스트 실행해서 실패 확인**

Run: `cd unibridge-service && .venv/bin/pytest tests/test_gateway.py::TestMetricsRoutesComparison -v`
Expected: FAIL — 404 (엔드포인트 미구현)

- [ ] **Step 3: 최소 구현 — 엔드포인트 추가**

`unibridge-service/app/routers/gateway.py`의 `metrics_top_routes` 함수 끝(`return routes` 이후 빈 줄) 바로 다음에 아래 함수를 추가:

```python
@router.get("/metrics/routes-comparison")
async def metrics_routes_comparison(
    time_range: str = Query("1h", alias="range", description="Time range"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, Any]:
    """Per-route comparison: requests, share, error_rate, p50/p95 latency in one payload."""
    if time_range not in VALID_RANGES:
        time_range = "1h"
    try:
        requests_res, errors_res, p50_res, p95_res = await asyncio.gather(
            prometheus_client.instant_query(
                f"topk(10, sum by (route) (increase(apisix_http_status[{time_range}])))"
            ),
            prometheus_client.instant_query(
                f'sum by (route) (increase(apisix_http_status{{code=~"5.."}}[{time_range}]))'
            ),
            prometheus_client.instant_query(
                "histogram_quantile(0.5, sum by (route, le) (rate(apisix_http_latency_bucket[5m])))"
            ),
            prometheus_client.instant_query(
                "histogram_quantile(0.95, sum by (route, le) (rate(apisix_http_latency_bucket[5m])))"
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )

    def _map_route_value(res: list[dict[str, Any]]) -> dict[str, float]:
        out: dict[str, float] = {}
        for r in res or []:
            route = r.get("metric", {}).get("route")
            if not route:
                continue
            value = r.get("value", [0, "0"])
            try:
                out[route] = float(value[1])
            except (IndexError, ValueError, TypeError):
                continue
        return out

    requests_map = _map_route_value(requests_res)
    errors_map = _map_route_value(errors_res)
    p50_map = _map_route_value(p50_res)
    p95_map = _map_route_value(p95_res)

    total = sum(requests_map.values())
    routes: list[dict[str, Any]] = []
    for route, req in requests_map.items():
        req_rounded = round(req)
        if req_rounded <= 0:
            continue
        share = (req / total * 100) if total > 0 else 0.0
        err = errors_map.get(route, 0.0)
        error_rate = (err / req * 100) if req > 0 else 0.0
        p50 = p50_map.get(route)
        p95 = p95_map.get(route)
        routes.append({
            "route": route,
            "requests": req_rounded,
            "share": round(share, 2),
            "error_rate": round(error_rate, 2),
            "latency_p50_ms": round(p50, 2) if p50 is not None and not math.isnan(p50) else None,
            "latency_p95_ms": round(p95, 2) if p95 is not None and not math.isnan(p95) else None,
        })

    routes.sort(key=lambda r: r["requests"], reverse=True)
    return {"total_requests": round(total), "routes": routes}
```

파일 상단 import 블록에 `math`가 없으면 추가:

```python
import math
```

(이미 있는지 확인 후 필요 시에만 추가. `grep -n "^import math" unibridge-service/app/routers/gateway.py`)

- [ ] **Step 4: 테스트 통과 확인**

Run: `cd unibridge-service && .venv/bin/pytest tests/test_gateway.py::TestMetricsRoutesComparison -v`
Expected: PASS (2 tests)

- [ ] **Step 5: 커밋**

```bash
git add unibridge-service/app/routers/gateway.py unibridge-service/tests/test_gateway.py
git commit -m "feat(gateway): add routes-comparison metrics endpoint"
```

---

## Task 2: 백엔드 — Edge Case (누락 라우트, NaN 레이턴시, 0 total)

**Files:**
- Modify: `unibridge-service/tests/test_gateway.py`

- [ ] **Step 1: 추가 테스트 작성**

`TestMetricsRoutesComparison` 클래스 안에 아래 테스트를 이어서 추가:

```python
    async def test_missing_latency_returns_null(self, client, admin_token):
        requests_result = [
            {"metric": {"route": "only-a"}, "value": [0, "200"]},
        ]
        # p50/p95 결과에 해당 route 없음
        mock = AsyncMock(side_effect=[requests_result, [], [], []])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/routes-comparison?range=1h",
                headers=auth_header(admin_token),
            )
        data = resp.json()
        only = data["routes"][0]
        assert only["latency_p50_ms"] is None
        assert only["latency_p95_ms"] is None

    async def test_nan_latency_returns_null(self, client, admin_token):
        requests_result = [
            {"metric": {"route": "x"}, "value": [0, "100"]},
        ]
        p50_result = [
            {"metric": {"route": "x"}, "value": [0, "NaN"]},
        ]
        mock = AsyncMock(side_effect=[requests_result, [], p50_result, []])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/routes-comparison?range=1h",
                headers=auth_header(admin_token),
            )
        data = resp.json()
        assert data["routes"][0]["latency_p50_ms"] is None

    async def test_zero_requests_returns_empty(self, client, admin_token):
        # route-a의 누적 증가량이 0 → 결과에서 제외되어야 함
        requests_result = [
            {"metric": {"route": "route-a"}, "value": [0, "0"]},
        ]
        mock = AsyncMock(side_effect=[requests_result, [], [], []])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/routes-comparison?range=1h",
                headers=auth_header(admin_token),
            )
        data = resp.json()
        assert data["total_requests"] == 0
        assert data["routes"] == []

    async def test_invalid_range_falls_back_to_1h(self, client, admin_token):
        mock = AsyncMock(side_effect=[[], [], [], []])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/routes-comparison?range=bogus",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        # 첫 호출에 전달된 쿼리에 [1h]가 포함되어야 함
        first_call_query = mock.call_args_list[0].args[0]
        assert "[1h]" in first_call_query

    async def test_prometheus_error_returns_502(self, client, admin_token):
        mock = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/routes-comparison?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 502

    async def test_forbidden_without_permission(self, client):
        resp = await client.get("/admin/gateway/metrics/routes-comparison?range=1h")
        assert resp.status_code in (401, 403)
```

- [ ] **Step 2: 테스트 실행**

Run: `cd unibridge-service && .venv/bin/pytest tests/test_gateway.py::TestMetricsRoutesComparison -v`
Expected: 모두 PASS. Task 1의 구현만으로 이 케이스들이 다 통과해야 한다 (`_map_route_value`의 `ValueError` 처리로 NaN 차단, requests_map 기반 조인이라 누락 라우트 자연 처리).

만약 `test_nan_latency_returns_null`가 실패하면: `float("NaN")`은 파이썬에서 `ValueError`가 아닌 실제 nan 값이므로 `math.isnan` 체크가 Task 1 구현에 이미 있다. 통과해야 함.

- [ ] **Step 3: 커밋**

```bash
git add unibridge-service/tests/test_gateway.py
git commit -m "test(gateway): add edge cases for routes-comparison endpoint"
```

---

## Task 3: 프론트엔드 — API 클라이언트 타입과 함수

**Files:**
- Modify: `unibridge-ui/src/api/client.ts`

- [ ] **Step 1: 타입과 함수 추가**

`unibridge-ui/src/api/client.ts`의 `getMetricsTopRoutes` 함수 주변을 읽어 스타일을 맞추고, `getMetricsRequestsTotal` 바로 뒤(368번 줄 근처)에 다음을 추가:

```typescript
export type RouteComparisonRow = {
  route: string;
  requests: number;
  share: number;
  error_rate: number;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
};

export type RouteComparisonResponse = {
  total_requests: number;
  routes: RouteComparisonRow[];
};

export async function getMetricsRoutesComparison(range = '1h'): Promise<RouteComparisonResponse> {
  const { data } = await client.get('/admin/gateway/metrics/routes-comparison', { params: { range } });
  return data;
}
```

기존 `getMetricsTopRoutes`는 유지한다(LlmMonitoring 등 다른 곳에서 쓸 수 있음).

- [ ] **Step 2: 타입 체크**

Run: `cd unibridge-ui && npx tsc --noEmit`
Expected: 에러 없음.

- [ ] **Step 3: 커밋**

```bash
git add unibridge-ui/src/api/client.ts
git commit -m "feat(ui): add routes-comparison API client"
```

---

## Task 4: 프론트엔드 — i18n 키 추가

**Files:**
- Modify: `unibridge-ui/src/locales/ko.json`
- Modify: `unibridge-ui/src/locales/en.json`

- [ ] **Step 1: 한국어 키 추가**

`unibridge-ui/src/locales/ko.json`의 `gatewayMonitoring` 블록에서 기존 `"topRoutes"` 라인을 아래로 교체:

```json
    "topRoutes": "트래픽 상위 라우트",
    "routeComparison": "라우트 비교",
    "share": "점유율",
    "latencyP50": "p50 (ms)",
    "latencyP95": "p95 (ms)",
```

(`topRoutes`는 다른 곳에서 참조될 수 있어 삭제하지 않음.)

- [ ] **Step 2: 영어 키 추가**

`unibridge-ui/src/locales/en.json`에서 동일 블록 찾아 아래 키 추가:

```json
    "topRoutes": "Top Routes",
    "routeComparison": "Route Comparison",
    "share": "Share",
    "latencyP50": "p50 (ms)",
    "latencyP95": "p95 (ms)",
```

- [ ] **Step 3: 커밋**

```bash
git add unibridge-ui/src/locales/ko.json unibridge-ui/src/locales/en.json
git commit -m "i18n(ui): add route comparison labels"
```

---

## Task 5: 프론트엔드 — 기존 Top Routes 섹션을 비교 테이블로 교체 (정렬/시각효과 없는 기본형)

**Files:**
- Modify: `unibridge-ui/src/pages/GatewayMonitoring.tsx`

- [ ] **Step 1: import와 쿼리 교체**

`unibridge-ui/src/pages/GatewayMonitoring.tsx`의 import 블록에서 API 함수 import 라인을 찾아 `getMetricsTopRoutes`를 `getMetricsRoutesComparison`으로 교체하고 타입도 import:

```tsx
import {
  getMetricsSummary,
  getMetricsRequests,
  getMetricsRequestsTotal,
  getMetricsStatusCodes,
  getMetricsLatency,
  getMetricsRoutesComparison,
  type RouteComparisonRow,
} from '../api/client';
```

- [ ] **Step 2: topRoutesQuery → routesComparisonQuery로 교체**

기존 `topRoutesQuery` 블록(96~100번 줄 근처):

```tsx
  const topRoutesQuery = useQuery({
    queryKey: ['metrics-top-routes', range],
    queryFn: () => getMetricsTopRoutes(range),
    refetchInterval: 30_000,
  });
```

를 아래로 교체:

```tsx
  const routesComparisonQuery = useQuery({
    queryKey: ['metrics-routes-comparison', range],
    queryFn: () => getMetricsRoutesComparison(range),
    refetchInterval: 30_000,
  });
```

그리고 기존 `hasPartialError` 계산의 `topRoutesQuery.isError`를 `routesComparisonQuery.isError`로 교체.

- [ ] **Step 3: Top Routes JSX 블록을 비교 테이블로 교체**

기존 Top Routes `<div className="chart-panel">` 블록(306~336번 줄)을 전부 다음으로 교체:

```tsx
      {/* Route Comparison */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('gatewayMonitoring.routeComparison')}</div>
        {(routesComparisonQuery.data?.routes ?? []).length > 0 ? (
          <div className="table-container" style={{ border: 'none' }}>
            <table className="data-table comparison-table">
              <thead>
                <tr>
                  <th>{t('gatewayMonitoring.route')}</th>
                  <th style={{ textAlign: 'right' }}>{t('gatewayMonitoring.requests')}</th>
                  <th style={{ textAlign: 'right' }}>{t('gatewayMonitoring.share')}</th>
                  <th style={{ textAlign: 'right' }}>{t('gatewayMonitoring.errorRate')}</th>
                  <th style={{ textAlign: 'right' }}>{t('gatewayMonitoring.latencyP50')}</th>
                  <th style={{ textAlign: 'right' }}>{t('gatewayMonitoring.latencyP95')}</th>
                </tr>
              </thead>
              <tbody>
                {(routesComparisonQuery.data?.routes ?? []).map((r) => (
                  <tr
                    key={r.route}
                    className={`route-row ${selectedRoute === r.route ? 'route-row--selected' : ''}`}
                    onClick={() => setSelectedRoute(selectedRoute === r.route ? null : r.route)}
                  >
                    <td className="cell-alias">{r.route}</td>
                    <td className="cell-metric">{r.requests.toLocaleString()}</td>
                    <td className="cell-metric">{r.share.toFixed(2)}%</td>
                    <td className="cell-metric">{r.error_rate.toFixed(2)}%</td>
                    <td className="cell-metric">{r.latency_p50_ms == null ? '—' : r.latency_p50_ms.toFixed(1)}</td>
                    <td className="cell-metric">{r.latency_p95_ms == null ? '—' : r.latency_p95_ms.toFixed(1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="no-data">{t('gatewayMonitoring.noRouteData')}</div>
        )}
      </div>
```

- [ ] **Step 4: 공통 CSS 클래스 추가**

`unibridge-ui/src/pages/GatewayMonitoring.css` 끝에 추가:

```css
.comparison-table .cell-metric {
  text-align: right;
  font-family: var(--font-mono);
  font-size: 12px;
  white-space: nowrap;
}
```

- [ ] **Step 5: 타입 체크 + 빌드 확인**

Run: `cd unibridge-ui && npx tsc --noEmit`
Expected: 에러 없음.

- [ ] **Step 6: 커밋**

```bash
git add unibridge-ui/src/pages/GatewayMonitoring.tsx unibridge-ui/src/pages/GatewayMonitoring.css
git commit -m "feat(ui): replace Top Routes with basic route comparison table"
```

---

## Task 6: 프론트엔드 — 컬럼 헤더 정렬 기능

**Files:**
- Modify: `unibridge-ui/src/pages/GatewayMonitoring.tsx`
- Modify: `unibridge-ui/src/pages/GatewayMonitoring.css`

- [ ] **Step 1: 정렬 상태와 헬퍼 추가**

`GatewayMonitoring` 함수 본문 상단(기존 `useState` 들 옆)에 추가:

```tsx
  type SortColumn = 'route' | 'requests' | 'share' | 'error_rate' | 'latency_p50_ms' | 'latency_p95_ms';
  type SortDir = 'asc' | 'desc';
  const [sort, setSort] = useState<{ column: SortColumn; dir: SortDir }>({ column: 'requests', dir: 'desc' });

  const toggleSort = (column: SortColumn) => {
    setSort((prev) =>
      prev.column === column
        ? { column, dir: prev.dir === 'asc' ? 'desc' : 'asc' }
        : { column, dir: 'desc' }
    );
  };

  const sortedRoutes = useMemo(() => {
    const rows = routesComparisonQuery.data?.routes ?? [];
    const multiplier = sort.dir === 'asc' ? 1 : -1;
    return [...rows].sort((a, b) => {
      const av = a[sort.column];
      const bv = b[sort.column];
      // null은 항상 맨 아래
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      if (typeof av === 'string' && typeof bv === 'string') {
        return av.localeCompare(bv) * multiplier;
      }
      return ((av as number) - (bv as number)) * multiplier;
    });
  }, [routesComparisonQuery.data, sort]);
```

주의: `useMemo`는 이미 import되어 있다(파일 1번 줄). 필요시 확인.

- [ ] **Step 2: 테이블 JSX에서 정렬 가능 헤더로 전환**

Task 5에서 추가한 `<thead>` 블록을 다음으로 교체:

```tsx
              <thead>
                <tr>
                  <th className="sortable-header" onClick={() => toggleSort('route')}>
                    {t('gatewayMonitoring.route')}
                    {sort.column === 'route' && <span className="sort-indicator">{sort.dir === 'asc' ? '▲' : '▼'}</span>}
                  </th>
                  <th className="sortable-header sortable-header--right" onClick={() => toggleSort('requests')}>
                    {t('gatewayMonitoring.requests')}
                    {sort.column === 'requests' && <span className="sort-indicator">{sort.dir === 'asc' ? '▲' : '▼'}</span>}
                  </th>
                  <th className="sortable-header sortable-header--right" onClick={() => toggleSort('share')}>
                    {t('gatewayMonitoring.share')}
                    {sort.column === 'share' && <span className="sort-indicator">{sort.dir === 'asc' ? '▲' : '▼'}</span>}
                  </th>
                  <th className="sortable-header sortable-header--right" onClick={() => toggleSort('error_rate')}>
                    {t('gatewayMonitoring.errorRate')}
                    {sort.column === 'error_rate' && <span className="sort-indicator">{sort.dir === 'asc' ? '▲' : '▼'}</span>}
                  </th>
                  <th className="sortable-header sortable-header--right" onClick={() => toggleSort('latency_p50_ms')}>
                    {t('gatewayMonitoring.latencyP50')}
                    {sort.column === 'latency_p50_ms' && <span className="sort-indicator">{sort.dir === 'asc' ? '▲' : '▼'}</span>}
                  </th>
                  <th className="sortable-header sortable-header--right" onClick={() => toggleSort('latency_p95_ms')}>
                    {t('gatewayMonitoring.latencyP95')}
                    {sort.column === 'latency_p95_ms' && <span className="sort-indicator">{sort.dir === 'asc' ? '▲' : '▼'}</span>}
                  </th>
                </tr>
              </thead>
```

- [ ] **Step 3: `<tbody>`에서 원본 대신 `sortedRoutes` 사용**

Task 5의 `<tbody>` 안 `(routesComparisonQuery.data?.routes ?? []).map(...)`를 `sortedRoutes.map(...)`로 교체.

- [ ] **Step 4: 헤더 스타일 추가**

`GatewayMonitoring.css` 끝에 추가:

```css
.comparison-table th.sortable-header {
  cursor: pointer;
  user-select: none;
}
.comparison-table th.sortable-header:hover {
  background: var(--bg-hover, rgba(128, 128, 128, 0.08));
}
.comparison-table th.sortable-header--right {
  text-align: right;
}
.comparison-table .sort-indicator {
  margin-left: 4px;
  font-size: 10px;
  color: var(--text-secondary);
}
```

- [ ] **Step 5: 타입 체크**

Run: `cd unibridge-ui && npx tsc --noEmit`
Expected: 에러 없음.

- [ ] **Step 6: 커밋**

```bash
git add unibridge-ui/src/pages/GatewayMonitoring.tsx unibridge-ui/src/pages/GatewayMonitoring.css
git commit -m "feat(ui): add column sorting to route comparison table"
```

---

## Task 7: 프론트엔드 — Requests/Share inline 막대바

**Files:**
- Modify: `unibridge-ui/src/pages/GatewayMonitoring.tsx`
- Modify: `unibridge-ui/src/pages/GatewayMonitoring.css`

- [ ] **Step 1: Requests 최대값 계산**

`sortedRoutes` 선언 다음 줄에 추가:

```tsx
  const maxRequests = useMemo(() => {
    const rows = routesComparisonQuery.data?.routes ?? [];
    return rows.reduce((m, r) => (r.requests > m ? r.requests : m), 0);
  }, [routesComparisonQuery.data]);
```

- [ ] **Step 2: 셀 렌더러 함수 추가**

`formatTimestamp` 함수 아래(파일 상단) 추가:

```tsx
function BarCell({ value, max, suffix = '' }: { value: number; max: number; suffix?: string }) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <span className="bar-cell">
      <span className="bar-cell__fill" style={{ width: `${pct}%` }} />
      <span className="bar-cell__text">{value.toLocaleString(undefined, { maximumFractionDigits: 2 })}{suffix}</span>
    </span>
  );
}
```

- [ ] **Step 3: Requests와 Share 셀 렌더링 교체**

Task 5/6의 `<tbody>` 안 Requests/Share `<td>`를 다음으로 교체:

```tsx
                    <td className="cell-metric"><BarCell value={r.requests} max={maxRequests} /></td>
                    <td className="cell-metric"><BarCell value={r.share} max={100} suffix="%" /></td>
```

- [ ] **Step 4: CSS에 bar-cell 스타일 추가**

`GatewayMonitoring.css` 끝에 추가:

```css
.bar-cell {
  position: relative;
  display: inline-block;
  width: 100%;
  min-width: 80px;
  padding: 2px 6px;
  box-sizing: border-box;
}
.bar-cell__fill {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  background: rgba(59, 130, 246, 0.15); /* accent-blue tint */
  border-radius: 2px;
  pointer-events: none;
}
.bar-cell__text {
  position: relative;
  z-index: 1;
  font-family: var(--font-mono);
  font-size: 12px;
}
```

- [ ] **Step 5: 타입 체크**

Run: `cd unibridge-ui && npx tsc --noEmit`
Expected: 에러 없음.

- [ ] **Step 6: 커밋**

```bash
git add unibridge-ui/src/pages/GatewayMonitoring.tsx unibridge-ui/src/pages/GatewayMonitoring.css
git commit -m "feat(ui): add inline bars for requests/share columns"
```

---

## Task 8: 프론트엔드 — Error Rate/Latency heatmap 색상

**Files:**
- Modify: `unibridge-ui/src/pages/GatewayMonitoring.tsx`
- Modify: `unibridge-ui/src/pages/GatewayMonitoring.css`

- [ ] **Step 1: heatmap 클래스 결정 헬퍼 추가**

`BarCell` 함수 아래에 추가:

```tsx
function errorRateClass(v: number): string {
  if (v >= 5) return 'heatmap-cell heatmap-cell--red';
  if (v >= 1) return 'heatmap-cell heatmap-cell--yellow';
  return 'heatmap-cell';
}

function latencyClass(v: number | null, max: number): string {
  if (v == null || max <= 0) return 'heatmap-cell';
  const ratio = v / max;
  if (ratio >= 0.8) return 'heatmap-cell heatmap-cell--red';
  if (ratio >= 0.5) return 'heatmap-cell heatmap-cell--yellow';
  return 'heatmap-cell';
}
```

- [ ] **Step 2: 레이턴시 컬럼별 최대값 계산**

`maxRequests` 아래에 추가:

```tsx
  const { maxP50, maxP95 } = useMemo(() => {
    const rows = routesComparisonQuery.data?.routes ?? [];
    return {
      maxP50: rows.reduce((m, r) => (r.latency_p50_ms != null && r.latency_p50_ms > m ? r.latency_p50_ms : m), 0),
      maxP95: rows.reduce((m, r) => (r.latency_p95_ms != null && r.latency_p95_ms > m ? r.latency_p95_ms : m), 0),
    };
  }, [routesComparisonQuery.data]);
```

- [ ] **Step 3: Error Rate/Latency 셀에 heatmap 클래스 적용**

Task 5/6의 `<tbody>`에서 해당 셀들을 다음으로 교체:

```tsx
                    <td className={`cell-metric ${errorRateClass(r.error_rate)}`}>{r.error_rate.toFixed(2)}%</td>
                    <td className={`cell-metric ${latencyClass(r.latency_p50_ms, maxP50)}`}>{r.latency_p50_ms == null ? '—' : r.latency_p50_ms.toFixed(1)}</td>
                    <td className={`cell-metric ${latencyClass(r.latency_p95_ms, maxP95)}`}>{r.latency_p95_ms == null ? '—' : r.latency_p95_ms.toFixed(1)}</td>
```

- [ ] **Step 4: heatmap CSS 추가**

`GatewayMonitoring.css` 끝에 추가:

```css
.heatmap-cell--yellow {
  background: rgba(234, 179, 8, 0.15);
}
.heatmap-cell--red {
  background: rgba(239, 68, 68, 0.18);
}
```

- [ ] **Step 5: 타입 체크**

Run: `cd unibridge-ui && npx tsc --noEmit`
Expected: 에러 없음.

- [ ] **Step 6: 커밋**

```bash
git add unibridge-ui/src/pages/GatewayMonitoring.tsx unibridge-ui/src/pages/GatewayMonitoring.css
git commit -m "feat(ui): add heatmap shading to error rate and latency columns"
```

---

## Task 9: 프론트엔드 — 기존 테스트 마이그레이션 + 새 테스트

**Files:**
- Modify: `unibridge-ui/src/test/GatewayMonitoring.test.tsx`

- [ ] **Step 1: mock과 import 교체**

`unibridge-ui/src/test/GatewayMonitoring.test.tsx` 상단의 `vi.mock('../api/client', ...)` 블록에서 `getMetricsTopRoutes`를 `getMetricsRoutesComparison`으로 교체:

```tsx
vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getMetricsSummary: vi.fn(),
  getMetricsRequests: vi.fn(),
  getMetricsRequestsTotal: vi.fn(),
  getMetricsStatusCodes: vi.fn(),
  getMetricsLatency: vi.fn(),
  getMetricsRoutesComparison: vi.fn(),
}));
```

그리고 import 구문에서도:

```tsx
import {
  getMetricsSummary,
  getMetricsRequests,
  getMetricsRequestsTotal,
  getMetricsStatusCodes,
  getMetricsLatency,
  getMetricsRoutesComparison,
} from '../api/client';

const mockedGetMetricsSummary = vi.mocked(getMetricsSummary);
const mockedGetMetricsRequests = vi.mocked(getMetricsRequests);
const mockedGetMetricsRequestsTotal = vi.mocked(getMetricsRequestsTotal);
const mockedGetMetricsStatusCodes = vi.mocked(getMetricsStatusCodes);
const mockedGetMetricsLatency = vi.mocked(getMetricsLatency);
const mockedGetMetricsRoutesComparison = vi.mocked(getMetricsRoutesComparison);
```

(파일에 `getMetricsRequestsTotal`이 없으면 함께 추가; 있으면 그대로 유지.)

`beforeEach`의 기본값:

```tsx
    mockedGetMetricsRoutesComparison.mockResolvedValue({ total_requests: 0, routes: [] });
```

를 `mockedGetMetricsTopRoutes.mockResolvedValue([])` 자리에 교체. 기존 `getMetricsTopRoutes` 참조를 모두 제거.

- [ ] **Step 2: 새 테스트 케이스 추가**

`describe('GatewayMonitoring', () => { ... })` 블록 끝에 추가:

```tsx
  it('renders route comparison table with all columns', async () => {
    mockedGetMetricsRoutesComparison.mockResolvedValue({
      total_requests: 1500,
      routes: [
        { route: 'route-a', requests: 1000, share: 66.67, error_rate: 1.0, latency_p50_ms: 42.5, latency_p95_ms: 180.0 },
        { route: 'route-b', requests: 500, share: 33.33, error_rate: 0.0, latency_p50_ms: 30.0, latency_p95_ms: 60.0 },
      ],
    });

    renderWithProviders(<GatewayMonitoring />);

    await waitFor(() => {
      expect(screen.getByText('route-a')).toBeInTheDocument();
    });
    expect(screen.getByText('route-b')).toBeInTheDocument();
    // Share, error rate, latencies render
    expect(screen.getByText('66.67%')).toBeInTheDocument();
    expect(screen.getByText('42.5')).toBeInTheDocument();
    expect(screen.getByText('180.0')).toBeInTheDocument();
  });

  it('renders em-dash for null latency', async () => {
    mockedGetMetricsRoutesComparison.mockResolvedValue({
      total_requests: 100,
      routes: [
        { route: 'x', requests: 100, share: 100, error_rate: 0, latency_p50_ms: null, latency_p95_ms: null },
      ],
    });

    renderWithProviders(<GatewayMonitoring />);

    await waitFor(() => {
      expect(screen.getByText('x')).toBeInTheDocument();
    });
    // p50/p95 cells are em-dash
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2);
  });

  it('applies red heatmap class when error rate >= 5%', async () => {
    mockedGetMetricsRoutesComparison.mockResolvedValue({
      total_requests: 100,
      routes: [
        { route: 'hot', requests: 100, share: 100, error_rate: 7.5, latency_p50_ms: 10, latency_p95_ms: 20 },
      ],
    });

    const { container } = renderWithProviders(<GatewayMonitoring />);

    await waitFor(() => {
      expect(screen.getByText('7.50%')).toBeInTheDocument();
    });
    const redCells = container.querySelectorAll('.heatmap-cell--red');
    expect(redCells.length).toBeGreaterThan(0);
  });

  it('sorts by requests descending by default and toggles on header click', async () => {
    const user = (await import('@testing-library/user-event')).default.setup();
    mockedGetMetricsRoutesComparison.mockResolvedValue({
      total_requests: 1500,
      routes: [
        { route: 'small', requests: 500, share: 33.33, error_rate: 0, latency_p50_ms: 10, latency_p95_ms: 20 },
        { route: 'big', requests: 1000, share: 66.67, error_rate: 0, latency_p50_ms: 5, latency_p95_ms: 10 },
      ],
    });

    const { container } = renderWithProviders(<GatewayMonitoring />);

    await waitFor(() => {
      expect(screen.getByText('big')).toBeInTheDocument();
    });

    const rows = container.querySelectorAll('.comparison-table tbody tr');
    expect(rows[0].textContent).toContain('big');

    // 헤더 재클릭으로 오름차순 전환
    const requestsHeader = screen.getByText((_, el) =>
      el?.classList.contains('sortable-header') === true && el.textContent?.includes('Requests') === true
    );
    await user.click(requestsHeader);
    const rowsAsc = container.querySelectorAll('.comparison-table tbody tr');
    expect(rowsAsc[0].textContent).toContain('small');
  });
```

주의: 기존 테스트 중 `getMetricsTopRoutes` 또는 top routes 관련 기대를 참조하는 것이 있으면 함께 삭제/교체해야 한다. 파일 전체를 한 번 훑어 잔류 참조가 없는지 확인한다.

- [ ] **Step 3: 테스트 실행**

Run: `cd unibridge-ui && npx vitest run src/test/GatewayMonitoring.test.tsx`
Expected: 모든 기존 테스트 + 새 4개 테스트 PASS.

- [ ] **Step 4: 커밋**

```bash
git add unibridge-ui/src/test/GatewayMonitoring.test.tsx
git commit -m "test(ui): migrate GatewayMonitoring tests to comparison endpoint"
```

---

## Task 10: 수동 검증

**Files:** 없음 (실행 및 UI 확인만)

- [ ] **Step 1: 백엔드 로컬 실행 (이미 실행 중이면 재시작)**

Run: `cd unibridge-service && .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload` (로컬 Python 환경 기준. Docker 미사용 원칙.)
Expected: 서버가 8080에서 LISTEN.

주의: 로컬에서 Prometheus가 없으면 엔드포인트가 502를 반환할 수 있음. Prometheus가 있는 환경에서 검증하거나, 테스트 통과만으로 로직을 보증하고 UI는 mock 데이터로 확인한다.

- [ ] **Step 2: 프론트엔드 dev 서버 실행**

Run: `cd unibridge-ui && npm run dev`
Expected: Vite가 localhost:5173에서 뜸.

- [ ] **Step 3: 브라우저에서 Gateway Monitoring 페이지 확인**

- `/monitoring/gateway` 또는 해당 라우트로 이동
- Route Comparison 테이블 렌더링 여부 확인
- 각 컬럼 헤더 클릭 시 정렬 방향 표시와 순서 변경
- Requests/Share 셀에 inline 막대바 표시
- Error Rate/p50/p95 셀의 heatmap 색상 (값에 따라)
- 행 클릭 시 기존 드릴다운 패널 정상 동작
- 다크/라이트 테마 토글 시 색상 대비 유지

- [ ] **Step 4: 전체 테스트 재실행**

Run:
```bash
cd unibridge-service && .venv/bin/pytest tests/test_gateway.py -v
cd unibridge-ui && npx vitest run
```
Expected: 모두 PASS.

- [ ] **Step 5: 최종 상태 확인**

Run: `git log --oneline -15`
Expected: Task 1~9의 커밋들이 순서대로 기록됨.

---

## 구현 완료 체크리스트

- [ ] 새 엔드포인트 `/admin/gateway/metrics/routes-comparison` 동작
- [ ] 6개 edge case 테스트 (join, null latency, NaN, 0 total, invalid range, Prometheus error, forbidden) PASS
- [ ] UI에 6컬럼 테이블 표시, 기본 `requests desc` 정렬
- [ ] 헤더 클릭 정렬 토글 동작
- [ ] Requests/Share inline 막대바 시각화
- [ ] Error Rate 절대 임계값 heatmap (1%/5%)
- [ ] p50/p95 상대 스케일 heatmap (컬럼 최대값의 50%/80%)
- [ ] 행 클릭 드릴다운 기존 동작 유지
- [ ] null latency가 `—`로 표시되고 정렬 시 맨 아래로 밀림
- [ ] i18n (ko/en) 키 추가
- [ ] 프론트/백 테스트 전부 PASS
