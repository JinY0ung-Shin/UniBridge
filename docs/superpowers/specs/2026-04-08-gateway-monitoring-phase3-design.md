# Gateway Management Phase 3 — Monitoring Dashboard Design Spec

## Context

Phase 1-2에서 Routes, Upstreams, Consumers 관리를 구현했다. Phase 3에서는 APISIX의 Prometheus 메트릭을 활용하여 트래픽/에러율/레이턴시 모니터링 대시보드를 우리 커스텀 UI에 추가한다. Prometheus 서버를 docker-compose에 추가하고, 외부 Grafana 연동이 가능한 구조로 설계한다.

## Architecture

```
APISIX(9091) ←── Prometheus(9090) 15초 간격 스크래핑
                       ↑
              query-service ── PromQL 쿼리 ──→ Prometheus HTTP API
                       ↑
              외부 Grafana ──────────────────→ Prometheus(9090) 직접 연결
```

- Prometheus: docker-compose에 추가, APISIX를 스크래핑 타겟으로 설정
- Prometheus 포트: `${PROMETHEUS_PORT:-9090}:9090` 외부 노출 (Grafana 연동용)
- query-service: Prometheus HTTP API(`/api/v1/query`, `/api/v1/query_range`)로 메트릭 조회
- 프론트엔드: recharts 라이브러리로 차트 시각화

## Infrastructure — Prometheus 추가

### docker-compose.yml

```yaml
prometheus:
  image: prom/prometheus:latest
  ports:
    - "${PROMETHEUS_PORT:-9090}:9090"
  volumes:
    - ./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
    - prometheus-data:/prometheus
  depends_on:
    apisix:
      condition: service_started
  networks:
    - apihub-net
```

volumes에 `prometheus-data` 추가.

### prometheus/prometheus.yml

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: 'apisix'
    metrics_path: '/apisix/prometheus/metrics'
    static_configs:
      - targets: ['apisix:9091']
```

## Backend — Metrics Proxy Endpoints

query-service에 `/admin/gateway/metrics/*` 엔드포인트를 추가한다. Prometheus HTTP API를 프록시하여 프론트엔드에 JSON으로 반환한다.

### 환경변수

```
PROMETHEUS_URL=http://prometheus:9090
```

config.py에 추가:
```python
PROMETHEUS_URL: str = "http://prometheus:9090"
```

### 엔드포인트

| Method | Path | 설명 |
|--------|------|------|
| GET | `/admin/gateway/metrics/summary` | 요약 카드 데이터 (총 요청, 에러율, 평균 레이턴시) |
| GET | `/admin/gateway/metrics/requests` | 요청 추이 시계열 데이터 |
| GET | `/admin/gateway/metrics/status-codes` | 상태코드 분포 |
| GET | `/admin/gateway/metrics/latency` | 레이턴시 P50/P95/P99 시계열 |
| GET | `/admin/gateway/metrics/top-routes` | 라우트별 트래픽 순위 |

모든 엔드포인트는 `range` 쿼리 파라미터를 받음: `15m`, `1h`, `6h`, `24h` (기본값 `1h`).

### Prometheus 클라이언트

`services/prometheus_client.py` 생성:

```python
async def instant_query(query: str) -> dict
async def range_query(query: str, start: str, end: str, step: str) -> dict
```

httpx로 Prometheus HTTP API 호출. 응답을 프론트엔드에 적합한 형태로 가공.

### 메트릭별 PromQL

**Summary:**
- 총 요청 수: `sum(increase(apisix_http_status[{range}]))`
- 에러율: `sum(rate(apisix_http_status{code=~"5.."}[5m])) / sum(rate(apisix_http_status[5m])) * 100`
- 평균 레이턴시: `sum(rate(apisix_http_latency_sum[5m])) / sum(rate(apisix_http_latency_count[5m]))`

**Request Trend (range query):**
- `sum(rate(apisix_http_status[5m]))` — step: range에 따라 15s~60s

**Status Codes:**
- `sum by (code) (increase(apisix_http_status[{range}]))`

**Latency (range query):**
- P50: `histogram_quantile(0.5, sum(rate(apisix_http_latency_bucket[5m])) by (le))`
- P95: `histogram_quantile(0.95, sum(rate(apisix_http_latency_bucket[5m])) by (le))`
- P99: `histogram_quantile(0.99, sum(rate(apisix_http_latency_bucket[5m])) by (le))`

**Top Routes:**
- `sum by (route) (increase(apisix_http_status[{range}]))` — topk(10)

## Frontend — GatewayMonitoring Page

### 의존성

`query-ui/package.json`에 `recharts` 추가.

### 사이드바

Gateway 섹션에 추가:
```
Gateway Routes
Gateway Upstreams
Gateway Consumers
Gateway Monitoring    ← 추가
```

### 페이지 레이아웃

```
┌─ Time Range Toggle: [15m] [1h] [6h] [24h] ─────┐
├─ Summary Cards (3열) ────────────────────────────┤
│  Total Requests │ Error Rate (%) │ Avg Latency   │
├─ Request Trend (라인 차트, 시계열) ───────────────┤
├─ Status Code Distribution (바 차트) ─────────────┤
├─ Latency P50/P95/P99 (라인 차트, 3개 라인) ──────┤
├─ Top Routes by Traffic (테이블) ─────────────────┤
└──────────────────────────────────────────────────┘
```

### 차트 스타일 (Vercel Dark)

- 차트 배경: `var(--bg-primary)` + `var(--border-default)` 보더
- 라인 색상: 블루(`#0070f3`), 그린(`#50e3c2`), 옐로(`#f5a623`)
- 그리드: `var(--border-subtle)`
- 텍스트: `var(--text-tertiary)`
- 툴팁: `var(--bg-secondary)` 배경

### 데이터 갱신

React Query로 30초마다 자동 갱신 (`refetchInterval: 30_000`).

### API Client 확장

```typescript
interface MetricsSummary {
  total_requests: number;
  error_rate: number;
  avg_latency_ms: number;
}

interface TimeSeriesPoint {
  timestamp: number;
  value: number;
}

interface StatusCodeData {
  code: string;
  count: number;
}

interface TopRoute {
  route: string;
  requests: number;
}

getMetricsSummary(range?: string): Promise<MetricsSummary>
getMetricsRequests(range?: string): Promise<TimeSeriesPoint[]>
getMetricsStatusCodes(range?: string): Promise<StatusCodeData[]>
getMetricsLatency(range?: string): Promise<{ p50: TimeSeriesPoint[]; p95: TimeSeriesPoint[]; p99: TimeSeriesPoint[] }>
getMetricsTopRoutes(range?: string): Promise<TopRoute[]>
```

## File Structure

### Infrastructure (new)
```
prometheus/
  prometheus.yml              # CREATE: Prometheus 설정
docker-compose.yml            # MODIFY: Prometheus 서비스 + volume 추가
```

### Backend
```
query-service/app/
  config.py                   # MODIFY: PROMETHEUS_URL 추가
  services/
    prometheus_client.py      # CREATE: Prometheus HTTP API 클라이언트
  routers/
    gateway.py                # MODIFY: metrics 엔드포인트 추가
```

### Frontend
```
query-ui/
  package.json                # MODIFY: recharts 추가
  src/
    api/client.ts             # MODIFY: metrics 타입 + API 함수
    components/Layout.tsx     # MODIFY: Gateway Monitoring 메뉴
    pages/
      GatewayMonitoring.tsx   # CREATE: 대시보드 페이지
      GatewayMonitoring.css   # CREATE: 차트/카드 스타일
    App.tsx                   # MODIFY: /gateway/monitoring 라우트
```

## What Does NOT Change

- 기존 모든 페이지 (Dashboard, Connections, Permissions, AuditLogs, QueryPlayground)
- Gateway Routes, Upstreams, Consumers 페이지
- APISIX config (prometheus 플러그인 이미 활성화됨)
- apisix_client.py (Prometheus는 별도 클라이언트)
