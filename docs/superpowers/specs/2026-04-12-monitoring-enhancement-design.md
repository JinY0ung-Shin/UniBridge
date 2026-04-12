# Monitoring Enhancement: Route Drill-down & Dashboard Integration

**Date:** 2026-04-12

## Overview

두 가지 기능을 추가한다:
1. GatewayMonitoring 페이지에서 라우트별 상세 통계 drill-down
2. 메인 Dashboard에 게이트웨이 모니터링 요약 정보 표시

## 1. 백엔드: 라우트별 메트릭 필터링

### 변경 대상
`/admin/gateway/metrics/*` 엔드포인트 4개에 optional `route` 쿼리 파라미터 추가.

### 엔드포인트별 동작

| Endpoint | route 없음 (기존) | route 있음 |
|---|---|---|
| `metrics/summary` | 전체 합산 | `{route="<value>"}` 필터 |
| `metrics/requests` | 전체 합산 | `{route="<value>"}` 필터 |
| `metrics/status-codes` | 전체 합산 | `{route="<value>"}` 필터 |
| `metrics/latency` | 전체 합산 | `{route="<value>"}` 필터 |
| `metrics/top-routes` | 변경 없음 | 변경 없음 |

### PromQL 필터 적용 방식
- `route` 파라미터가 전달되면 PromQL의 `apisix_http_status` → `apisix_http_status{route="<value>"}`로 변환
- `apisix_http_latency_bucket` → `apisix_http_latency_bucket{route="<value>"}`
- `apisix_http_latency_sum` → `apisix_http_latency_sum{route="<value>"}`
- `apisix_http_latency_count` → `apisix_http_latency_count{route="<value>"}`
- 입력 검증: route 값은 `[a-zA-Z0-9_\-\.]` 패턴만 허용 (PromQL injection 방지)

## 2. 프론트엔드: GatewayMonitoring 라우트 Drill-down

### 동작 방식
- Top Routes 테이블의 각 행을 클릭 가능하게 변경
- 클릭 시 테이블 아래에 인라인 상세 패널이 펼쳐짐
- 같은 라우트 재클릭 시 패널 닫힘, 다른 라우트 클릭 시 전환

### 상세 패널 구성
1. **헤더**: 라우트 이름 + 닫기 버튼
2. **요약 카드 3개**: 해당 라우트의 Total Requests / Error Rate / Avg Latency
3. **Request Trend 차트**: 해당 라우트의 요청 추이 라인 차트
4. **Status Code Distribution**: 해당 라우트의 상태코드 분포 바 차트

### 데이터 로딩
- 라우트 선택 시 4개 API를 `route` 파라미터와 함께 호출
- React Query로 캐싱 및 30초 자동 갱신
- 로딩 상태 표시

## 3. 프론트엔드: 메인 Dashboard 모니터링 섹션

### 위치
기존 DB 상태 그리드 아래에 "Gateway Monitoring" 섹션 추가.

### 구성
1. **섹션 헤더**: "Gateway Monitoring" + "View Details →" 링크 (GatewayMonitoring 페이지로 이동)
2. **요약 카드 3개**: Total Requests / Error Rate / Avg Latency (1h 고정)
3. **미니 Request Trend 차트**: 높이 ~160px, 축소 라인 차트 (1h 고정)

### 권한
- `gateway.monitoring.read` 권한이 있는 사용자만 이 섹션이 표시됨
- 권한 없으면 섹션 자체가 렌더링되지 않음

### 데이터
- `getMetricsSummary("1h")`, `getMetricsRequests("1h")` 호출
- 30초 자동 갱신 (React Query refetchInterval)
- Prometheus 연결 실패 시 섹션은 표시하되 "No data available" 메시지
