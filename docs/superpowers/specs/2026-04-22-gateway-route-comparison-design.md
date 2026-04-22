# 게이트웨이 라우트 비교 뷰

## 배경

게이트웨이 모니터링 페이지(`GatewayMonitoring`)는 전체 요약 카드와 개별 라우트 드릴다운을 제공하지만, **여러 라우트를 동시에 비교**할 수단이 부족하다. 현재 "Top Routes" 섹션은 라우트명과 요청 수(숫자) 2컬럼만 보여주며:

- 어느 라우트가 에러율/레이턴시가 높은지 한눈에 파악할 수 없다.
- 요청 수 정렬은 고정이고, 다른 지표(에러율·p95 등) 기준으로 재정렬할 수 없다.
- 숫자만으로는 라우트 간 상대적 크기가 직관적으로 안 보인다.

## 목표

- Top Routes 섹션을 **라우트 비교 테이블**로 확장한다.
- 한 테이블에서 요청량, 점유율, 에러율, p50/p95 레이턴시를 동시에 비교할 수 있게 한다.
- 시각적 보조(막대바, heatmap 색상)로 상대적 크기와 이상치를 즉시 인지하게 한다.
- 모든 컬럼에서 정렬 가능하게 한다.
- 행 클릭 시 기존 드릴다운 패널 동작은 유지한다.

## 비목표

- 라우트별 추이(sparkline) 표시는 본 스펙 범위 외. 네트워크 비용 부담과 설계 범위 축소를 위해 추후 확장으로 남긴다.
- 비교 대상 라우트를 사용자가 수동으로 선택하는 UI는 범위 외. 현재 라우트 수가 10개 내외라 전체를 한 테이블에 담는 것으로 충분하다.
- LLM 메트릭 비교 뷰(`LlmMonitoring`)는 본 스펙 범위 외. 동일 패턴을 후속 과제로 적용 가능.
- 새로운 Prometheus 메트릭 수집은 범위 외. 기존 `apisix_http_status`, `apisix_http_latency_*` 메트릭만 활용한다.

## 설계

### 백엔드

**새 엔드포인트:** `GET /admin/gateway/metrics/routes-comparison?range=1h`

**권한:** `gateway.monitoring.read` (기존 메트릭과 동일)

**쿼리 파라미터:**
- `range`: `VALID_RANGES` 중 하나 (`15m`, `1h`, `6h`, `24h`, `7d`, `30d`, `60d`). 유효하지 않으면 `1h`로 fallback.

**Prometheus 쿼리 (병렬 실행):**

1. **요청 수 Top 10** (라우트별 누적, 상위 10개만)
   ```
   topk(10, sum by (route) (increase(apisix_http_status[{range}])))
   ```
2. **에러 수** (5xx만 집계, 전체 라우트)
   ```
   sum by (route) (increase(apisix_http_status{code=~"5.."}[{range}]))
   ```
3. **p50 레이턴시** (histogram_quantile, 전체 라우트)
   ```
   histogram_quantile(0.5, sum by (route, le) (rate(apisix_http_latency_bucket[5m])))
   ```
4. **p95 레이턴시** (전체 라우트)
   ```
   histogram_quantile(0.95, sum by (route, le) (rate(apisix_http_latency_bucket[5m])))
   ```

네 쿼리를 `asyncio.gather`로 병렬 실행한 뒤, **쿼리 1(요청 수 Top 10)의 라우트 집합을 기준으로** 나머지 세 쿼리 결과를 `route` 라벨로 좌측 조인한다. 쿼리 1에 없는 라우트는 응답에서 제외. 쿼리 1에 있으나 쿼리 2에 없는 라우트는 에러 수 0으로 처리, 쿼리 3/4에 없으면 레이턴시는 `null`.

**응답 형식:**
```json
{
  "total_requests": 12345,
  "routes": [
    {
      "route": "<route-id>",
      "requests": 4200,
      "share": 34.02,
      "error_rate": 1.19,
      "latency_p50_ms": 42.5,
      "latency_p95_ms": 180.0
    }
  ]
}
```

- `share`는 백엔드에서 계산 (`requests / total_requests * 100`, 소수 둘째자리 반올림).
- `total_requests`는 분모로 쓰기 위해 함께 반환.
- `error_rate`는 `errors / requests * 100`; `requests == 0`이면 `0`.
- 레이턴시 값이 NaN/missing이면 `null`.
- `routes`는 `requests` 내림차순 정렬.
- `requests == 0`인 라우트는 응답에서 제외.

**라우트 개수 상한:** 기본 10개. 요청 수 내림차순으로 `topk(10, ...)` 적용하여 기존 `top-routes`와 동일한 기준을 유지한다. 상한을 늘리고 싶어질 경우 `limit` 쿼리 파라미터를 후속으로 도입할 수 있다(본 스펙에서는 고정).

**에러 처리:** 기존 메트릭 엔드포인트들과 동일하게 Prometheus 실패 시 `502 Bad Gateway`.

**기존 `/metrics/top-routes` 유지:** 현재 엔드포인트는 제거하지 않는다 — 다른 곳에서 참조될 수 있고, 단순 "요청 수 topN"만 필요한 경우도 있을 수 있다. 다만 UI의 `GatewayMonitoring.tsx`에서는 더 이상 사용하지 않는다.

### 프론트엔드

**영향 범위:** `unibridge-ui/src/pages/GatewayMonitoring.tsx`, `GatewayMonitoring.css`, `unibridge-ui/src/api/client.ts`, `unibridge-ui/src/locales/*`.

**API 클라이언트 추가:**
- `getMetricsRoutesComparison(range: string): Promise<RouteComparisonResponse>` 추가. 기존 `getMetricsTopRoutes`는 유지.
- 타입:
  ```ts
  type RouteComparisonRow = {
    route: string;
    requests: number;
    share: number;
    error_rate: number;
    latency_p50_ms: number | null;
    latency_p95_ms: number | null;
  };
  type RouteComparisonResponse = {
    total_requests: number;
    routes: RouteComparisonRow[];
  };
  ```

**컴포넌트:** `GatewayMonitoring.tsx` 내 기존 "Top Routes" `<div className="chart-panel">` 블록을 **Route Comparison 테이블**로 교체한다. 별도 컴포넌트 파일로 분리할 수도 있지만, 다른 곳에서 재사용할 계획이 없고 로컬 상태(정렬 기준)가 이 페이지에 묶여 있으므로 현 단계에서는 같은 파일 내에 테이블 렌더 함수로 유지한다. 파일 크기가 신경 쓰일 정도로 커지면 별도 파일로 분리.

**쿼리 변경:**
- 기존 `topRoutesQuery`를 `routesComparisonQuery`로 대체 (동일한 `refetchInterval: 30_000`).

**테이블 스펙:**

| 컬럼 | 정렬 | 시각 보조 | 포맷 |
|---|---|---|---|
| Route | 알파벳 | 없음 | 라우트 ID/이름 그대로 |
| Requests | 내림(기본) | inline 막대바 (최대값 대비 %) | `toLocaleString()` |
| Share % | 내림 | inline 막대바 (0~100% 스케일) | `n.nn%` |
| Error Rate % | 내림 | heatmap 배경: `< 1%` 무색, `1~5%` 노랑 tint, `> 5%` 빨강 tint | `n.nn%` |
| p50 ms | 내림 | heatmap 배경: 상대 스케일 (해당 컬럼 최대값의 50% 초과 노랑, 80% 초과 빨강) | `n.n` |
| p95 ms | 내림 | heatmap 배경: 동일 방식, 상대 스케일 | `n.n` |

- 정렬 상태는 `useState<{column: string; dir: 'asc'|'desc'}>`로 관리. 기본값은 `{column: 'requests', dir: 'desc'}`.
- 헤더 클릭으로 정렬 컬럼/방향 토글. 같은 컬럼 재클릭 시 `asc`/`desc` 교체, 다른 컬럼 클릭 시 해당 컬럼 내림차순으로 초기화.
- 레이턴시 `null` 값은 정렬 시 항상 맨 아래로 밀고, 셀에는 `—`로 표시.

**heatmap 색상 규칙:**
- **Error Rate**: 절대 임계값 (`1%`, `5%`). 서비스 전반의 품질 기준이라 절대값이 의미 있음.
- **Latency p50/p95**: 상대 스케일 (해당 컬럼 내 최대값 기준 50%/80%). 라우트 성격에 따라 정상 레이턴시 범위가 다르므로 절대값보다 **상대적으로 튀는 라우트**를 강조하는 편이 유용하다.
- 색상은 기존 CSS 변수(`--accent-red`, `--accent-yellow`) 위에 낮은 opacity의 `rgba()` 배경 오버레이로 구현. 다크/라이트 테마 모두에서 대비가 유지되도록 opacity는 0.1~0.2 범위로 조정.

**inline 막대바 구현:**
- 셀 내부에 `<span class="inline-bar">` 배경을 두고 `width: {percent}%`로 채움. 숫자 텍스트는 그 위에 오버레이.
- `Requests`는 테이블 내 최대값 대비 퍼센트; `Share %`는 항상 0~100% 스케일.

**행 상호작용:**
- 행 클릭 시 기존 `setSelectedRoute` 로직 그대로 유지. 드릴다운 패널 동작은 변경 없음.
- 선택된 행은 기존 `.route-row--selected` 스타일 유지.
- 헤더 클릭은 정렬만 수행하고 행 선택을 트리거하지 않도록 이벤트 버블링 분리.

**로딩/에러 상태:**
- 쿼리 loading 중: 기존처럼 `<div className="no-data">` 또는 skeleton.
- 쿼리 error: 기존 `hasPartialError` 배너 조건에 `routesComparisonQuery.isError` 추가.
- 라우트 0개: "no route data" 문구 표시 (기존 i18n 키 재사용).

**i18n:**
- `gatewayMonitoring.topRoutes` → `gatewayMonitoring.routeComparison` 로 라벨 변경 (ko/en).
- 새 컬럼 헤더 키 추가: `gatewayMonitoring.share`, `gatewayMonitoring.latencyP50`, `gatewayMonitoring.latencyP95`. 기존 `gatewayMonitoring.errorRate`, `gatewayMonitoring.requests`는 재사용.

### 데이터 흐름

```
User → GatewayMonitoring page
  → routesComparisonQuery (30s refetch)
  → GET /admin/gateway/metrics/routes-comparison?range={range}
  → gateway.py metrics_routes_comparison()
    ├─ PromQL: sum by (route) (increase(apisix_http_status[range]))
    ├─ PromQL: sum by (route) (increase(apisix_http_status{code=~"5.."}[range]))
    ├─ PromQL: histogram_quantile(0.5, ...)
    └─ PromQL: histogram_quantile(0.95, ...)
  → join on `route` label → compute share/error_rate
  → return {total_requests, routes[]}
  → Table renders rows with sort state, inline bars, heatmap cells
  → Row click → setSelectedRoute → existing drill-down panel unchanged
```

## 테스트

### 백엔드

`unibridge-service/tests/test_gateway.py`에 케이스 추가:

- `test_metrics_routes_comparison_ok`: Prometheus mock이 네 쿼리에 각각 응답할 때, 응답이 라우트별로 올바르게 조인되고 `share`/`error_rate`가 계산되는지.
- `test_metrics_routes_comparison_missing_latency`: 한 라우트에 레이턴시 메트릭이 없을 때 `null`로 반환되는지.
- `test_metrics_routes_comparison_zero_total`: 전체 요청이 0일 때(응답 `routes: []`) 안전하게 빈 배열 반환.
- `test_metrics_routes_comparison_invalid_range`: 잘못된 range 값이 `1h`로 fallback 되는지.
- `test_metrics_routes_comparison_prometheus_error`: Prometheus 예외 시 502.
- `test_metrics_routes_comparison_forbidden`: `gateway.monitoring.read` 권한 없을 때 403.

### 프론트엔드

`unibridge-ui/src/test/` 하위에 비교 테이블 렌더링/정렬 테스트 추가:

- 모의 응답으로 렌더링 시 모든 컬럼이 표시되고, 기본 정렬이 `requests desc`인지.
- 헤더 클릭 시 정렬 컬럼/방향 토글이 작동하는지.
- 레이턴시 `null` 행이 `—`로 표시되고 정렬 시 항상 맨 아래에 위치하는지.
- 행 클릭이 기존 드릴다운을 여는지 (selectedRoute 상태 변화).
- 에러율 5% 초과 행에 빨강 heatmap 클래스가 붙는지 (DOM 속성 확인).

## 마이그레이션/호환성

- 기존 `/metrics/top-routes` 엔드포인트는 유지. 삭제하지 않음.
- UI는 `getMetricsTopRoutes` 사용을 중단하고 `getMetricsRoutesComparison`으로 교체한다. 클라이언트 export는 유지.
- DB 스키마 변경 없음. 새 환경변수 없음. 배포 시 추가 작업 불필요.

## 오픈 이슈 / 후속 과제

- 본 스펙에서 제외한 sparkline(라우트별 추이 미니 차트)은 백엔드에 `/metrics/requests-by-route?range=...`처럼 라우트별 시계열을 한 번에 반환하는 엔드포인트를 추가하는 방식으로 후속 과제화 가능.
- LLM 모니터링 페이지에도 동일한 "모델 비교 테이블" 패턴을 적용할 여지 있음.
