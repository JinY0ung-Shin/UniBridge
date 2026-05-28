# Gateway Monitoring — API Key Filter & llm-proxy Hiding

**Date**: 2026-05-28
**Status**: Approved (design)
**Scope**: Gateway monitoring page UX + backend metrics endpoints

## Goal

게이트웨이 모니터링 페이지에서:
1. 페이지 상단 드롭다운으로 **API key(consumer)** 별 트래픽을 필터링할 수 있어야 한다. 기본값은 "전체".
2. `llm-proxy` 라우트는 별도 LLM 모니터링 페이지에서 보여주므로 게이트웨이 모니터링 페이지의 모든 메트릭에서 **숨긴다** (하드코딩, 토글 없음).

두 변경은 같은 페이지에 영향을 주므로 한 PR/플랜으로 묶어서 진행.

## Background

- 게이트웨이 메트릭은 APISIX prometheus 플러그인이 노출하는 `apisix_http_status`, `apisix_http_latency_{sum,count,bucket}` 시리즈로부터 산출된다.
- APISIX prometheus는 위 시리즈에 `route`, `code`, `service`, `consumer`, `node` 등 라벨을 포함한다. `consumer` 값은 APISIX consumer username이며, 이 시스템에서는 `ApiKey.name`과 동일하다 (`unibridge-service/app/routers/api_keys.py`에서 consumer를 그 이름으로 등록).
- 현재 `/admin/gateway/metrics/*` 엔드포인트는 optional `route` 파라미터만 지원하고 consumer 필터는 없다 (`unibridge-service/app/routers/gateway.py`).
- `llm-proxy`는 보호된 시스템 라우트 ID (`PROTECTED_ROUTE_IDS`에 등록되어 있음, `gateway.py:30`). LLM 모니터링 페이지는 litellm 시리즈를 별도로 보여준다.

## Non-goals

- "익명 트래픽" (consumer 라벨 없는 호출)을 위한 별도 드롭다운 옵션 — 첫 버전에서는 "전체" 선택 시에만 포함됨.
- llm-proxy를 다시 보여주기 위한 사용자 토글 — 사용자가 "일단 완전 숨기자"라고 명시함.
- `llm-admin` 라우트 숨김 — 요청 범위에 없음.
- LLM 모니터링 페이지 변경.

## Architecture

### Backend (`unibridge-service/app/routers/gateway.py`)

#### 1. `_labels()` 헬퍼 확장

```python
def _labels(route: str | None, consumer: str | None, *extra: str) -> str:
    parts = list(extra)
    if route:
        parts.append(f'route="{route}"')
    else:
        # Default: hide llm-proxy from gateway monitoring page.
        # Only applied when no specific route is requested — if a caller
        # explicitly asks for route=llm-proxy (e.g. via direct API), they get it.
        parts.append('route!="llm-proxy"')
    if consumer:
        parts.append(f'consumer="{consumer}"')
    return "{" + ",".join(parts) + "}" if parts else ""
```

핵심 결정:
- `route` 명시되면 `route="x"`만 부여 (llm-proxy 제외 셀렉터는 redundant이므로 생략).
- `route` 미명시 시 `route!="llm-proxy"` 자동 주입 → 요약/추세/지연/상태코드/볼륨/라우트비교 **모두**에서 llm-proxy 트래픽이 제외됨.
- `consumer`도 같은 방식으로 옵션 처리.

#### 2. `_validate_consumer()` 추가

```python
_SAFE_CONSUMER_RE = re.compile(r"^[a-zA-Z0-9_\-\.]+$")

def _validate_consumer(consumer: str | None) -> None:
    if consumer and not _SAFE_CONSUMER_RE.match(consumer):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid consumer name",
        )
```

`_validate_route`와 동일한 패턴. PromQL injection 방지.

#### 3. 6개 엔드포인트에 `consumer` 쿼리 파라미터 추가

대상:
- `/metrics/summary`
- `/metrics/requests`
- `/metrics/requests-total`
- `/metrics/status-codes`
- `/metrics/latency`
- `/metrics/routes-comparison`

`routes-comparison`은 현재 `route` 파라미터도 없지만, **consumer 필터는 받아야 함**. 라우트 비교 PromQL이 `sum by (route) (increase(apisix_http_status[...]))` 형태이므로, label selector 부분에 `{route!="llm-proxy", consumer="x"}` 형태로 주입한다. 즉:

```python
# Before
"topk(10, sum by (route) (increase(apisix_http_status[{tw.promql_window}])))"

# After (with hs = _labels(None, consumer))
f"topk(10, sum by (route) (increase(apisix_http_status{hs}[{tw.promql_window}])))"
```

`errors_res`, `p50_res`, `p95_res`도 동일하게 변경. 기존 `code=~"5.."` 셀렉터는 `extra`로 합쳐 전달.

#### 4. 라우트 비교의 `_labels` 호출 방식

`routes-comparison`에서는 모든 4개 쿼리가 라벨 셀렉터를 공유해야 함:
- `hs = _labels(None, consumer)` — `{route!="llm-proxy"[, consumer="x"]}`
- `hs5 = _labels(None, consumer, 'code=~"5.."')` — error_rate용

### Frontend (`unibridge-ui`)

#### 1. `src/api/client.ts`

6개 메트릭 함수 시그니처 변경:

```ts
export async function getMetricsSummary(
  sel: TimeSelection = DEFAULT_SELECTION,
  route?: string,
  consumer?: string,
): Promise<MetricsSummary> { ... }

export async function getMetricsRoutesComparison(
  sel: TimeSelection = DEFAULT_SELECTION,
  consumer?: string,
): Promise<RouteComparisonResponse> { ... }
```

axios는 `params: { route: undefined }`를 자동으로 query string에서 생략하므로 호환됨.

#### 2. `src/pages/GatewayMonitoring.tsx`

상태 추가:
```ts
const [selectedConsumer, setSelectedConsumer] = useState<string>(''); // '' = 전체
```

API key 목록 fetch:
```ts
const apiKeysQuery = useQuery({
  queryKey: ['api-keys', 'gateway-monitoring-filter'],
  queryFn: getApiKeys,
  staleTime: 5 * 60 * 1000,
  refetchInterval: false,
});
```

모든 10개 메트릭 `useQuery`에 consumer 전파:
- 상단: `summaryQuery`, `requestsQuery`, `requestsTotalQuery`, `statusQuery`, `latencyQuery`, `routesComparisonQuery` (6개)
- 드릴다운: `routeSummaryQuery`, `routeRequestsQuery`, `routeStatusQuery`, `routeVolumQuery` (4개)

각각 `queryKey`에 `selectedConsumer` 추가, `queryFn`에 인자로 전달.

페이지 헤더 레이아웃:

```jsx
<div className="page-header">
  <div>
    <h1>{t('gatewayMonitoring.title')}</h1>
    <p className="page-subtitle">{t('gatewayMonitoring.subtitle')}</p>
  </div>
  <div className="page-header__filters">
    <label className="api-key-filter">
      <span className="api-key-filter__label">{t('gatewayMonitoring.apiKeyFilter')}</span>
      <select
        value={selectedConsumer}
        onChange={(e) => setSelectedConsumer(e.target.value)}
      >
        <option value="">{t('gatewayMonitoring.allApiKeys')}</option>
        {(apiKeysQuery.data ?? [])
          .slice()
          .sort((a, b) => a.name.localeCompare(b.name))
          .map((k) => (
            <option key={k.name} value={k.name}>{k.name}</option>
          ))}
      </select>
    </label>
    <TimeRangeSelector value={selection} onChange={setSelection} />
  </div>
</div>
```

CSS는 `GatewayMonitoring.css`에 최소한의 flex 정렬만 추가 (`.page-header__filters { display: flex; gap: 12px; align-items: center; }`).

#### 3. i18n

`unibridge-ui/src/locales/ko.json` / `en.json`의 `gatewayMonitoring` 섹션에 추가:

```json
"apiKeyFilter": "API 키" / "API Key",
"allApiKeys": "전체" / "All"
```

### 권한 / 인증

`getApiKeys()`는 `/admin/api-keys` 엔드포인트를 호출하며 `apikeys.read` 권한 필요. 게이트웨이 모니터링은 `gateway.monitoring.read` 권한 사용 중. 두 권한이 같은 admin 역할에 묶여 있다고 가정. 없으면 드롭다운에 옵션이 비어 보일 수 있으며 (그래도 "전체"는 선택 가능), 이는 first version에서 허용 가능한 동작.

추후 권한 분리 이슈가 생기면 `/admin/gateway/metrics/consumers` 같은 가벼운 엔드포인트로 분리.

## Data Flow

1. 사용자가 페이지 진입 → `apiKeysQuery`가 한 번 fetch → 드롭다운 옵션 채움.
2. 기본값 `selectedConsumer === ''` → 모든 메트릭 쿼리가 `consumer` 인자 없이 호출 → 백엔드는 `consumer` 필터 미적용, but `route!="llm-proxy"` 자동 적용.
3. 사용자가 드롭다운에서 API key 선택 → `selectedConsumer` 변경 → React Query가 새 키로 모든 메트릭 재요청 → 백엔드는 `consumer="x"` 셀렉터 추가하여 PromQL 실행.
4. 라우트 비교 표에서 행 클릭 → `selectedRoute` 설정 → 드릴다운 4개 쿼리는 `route` + `consumer` 둘 다 보내 양쪽 모두 필터링.

## Error Handling

- Prometheus 에러: 기존 동작 그대로 (`502 Bad Gateway`).
- 잘못된 `consumer` 형식: `400 Bad Request` ("Invalid consumer name").
- 잘못된 `route` 형식: 기존대로 `400`.
- API key 목록 fetch 실패: 드롭다운은 "전체"만 선택 가능. 페이지 자체는 동작.

## Testing

### Backend — `unibridge-service/tests/test_gateway.py`

- `test_metrics_summary_excludes_llm_proxy_by_default` — `route` 미명시 호출 시 PromQL에 `route!="llm-proxy"` 셀렉터가 포함되는지 확인 (mock prometheus client에서 query string 검증).
- `test_metrics_summary_with_consumer_filter` — `?consumer=alice` 호출 시 PromQL에 `consumer="alice"` 셀렉터 포함.
- `test_metrics_summary_route_explicit_skips_llm_proxy_exclusion` — `?route=llm-proxy` 명시 호출 시 PromQL에 `route="llm-proxy"`만 포함되고 `route!=` 셀렉터가 없는지.
- `test_metrics_invalid_consumer_returns_400` — `?consumer=alice;drop` → 400.
- `test_metrics_routes_comparison_with_consumer` — `routes-comparison`에 consumer 적용 시 4개 PromQL 모두에 셀렉터 포함.

### Frontend — `unibridge-ui/src/test/GatewayMonitoring.test.tsx`

- 드롭다운 렌더링 — 옵션이 "전체" + mock API keys로 구성됨.
- 기본값이 "전체" — 첫 렌더 시 메트릭 API 호출에 `consumer` 파라미터 없음.
- 드롭다운 변경 → 새 메트릭 호출에 `consumer=<선택값>` 포함.
- 라우트 비교 표 행 클릭 → 드릴다운 쿼리에 `route` + `consumer` 둘 다 포함.

## Edge Cases & Decisions

| Case | Decision |
|---|---|
| 익명 트래픽 (consumer 라벨 없음) | "전체" 선택 시 포함. 별도 옵션 없음. |
| 삭제된 API key의 과거 트래픽 | 드롭다운엔 안 보임. "전체"에선 자동 포함. |
| API key 목록 fetch 실패 | "전체"만 선택 가능. 페이지 자체는 정상. |
| 같은 API key가 `llm-proxy`로도 호출 | "전체"에선 보이지 않음 (llm-proxy 자체가 숨겨지므로). LLM 모니터링 페이지에서 확인. |
| `route="llm-proxy"` 명시 직접 호출 | API 레벨에서는 동작 (UI에서 도달할 수 없음). 의도된 동작. |
| 권한 — 모니터링 권한자가 `apikeys.read` 없음 | 드롭다운 비어 보임, 기능은 "전체"로 동작. 추후 분리 가능. |

## Implementation Order (for plan)

1. 백엔드: `_labels()` 확장 + `_validate_consumer()` + 6개 엔드포인트 시그니처 + 테스트.
2. 백엔드 통합 테스트 통과 확인.
3. 프론트엔드: `api/client.ts` 시그니처 확장.
4. 프론트엔드: `GatewayMonitoring.tsx` 상태, 드롭다운 UI, queryKey 확장.
5. 프론트엔드: i18n 키 추가.
6. 프론트엔드: 테스트 업데이트.
7. 수동 검증 — 실제 페이지에서 llm-proxy 숨김, 드롭다운 동작 확인.
