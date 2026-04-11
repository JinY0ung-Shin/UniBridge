# Health Check Alert System Design

## Overview

UniBridge에 헬스 체크 알림 시스템을 추가한다. DB 연결 실패, API 업스트림 다운, 에러율 임계치 초과 시 Webhook(POST)으로 알림을 발송하고, 복구 시 복구 알림을 발송한다.

## Requirements

- **알림 대상**: DB 연결 실패, 업스트림 다운, 5xx 에러율 초과
- **알림 채널**: Webhook POST (사내 이메일 API, Slack 등)
- **수신자 라우팅**: API/DB별 수신자 개별 설정, 복수 수신자 지원
- **Webhook 템플릿**: 채널별 payload 포맷 커스터마이징
- **중복 방지**: 최초 1회 알림 + 복구 알림 (상태 전이 기반)
- **체크 주기**: 전체 60초

## Data Models

### AlertChannel

Webhook 채널 정의 (이메일 API, Slack 등).

| Field | Type | Description |
|-------|------|-------------|
| id | Integer PK | Auto-increment |
| name | String(100), unique | 채널 이름 |
| webhook_url | String, not null | POST 요청 URL |
| payload_template | Text, not null | JSON 템플릿 (플레이스홀더 포함) |
| headers | Text, nullable | 커스텀 HTTP 헤더 (JSON object) |
| enabled | Boolean, default True | 활성 여부 |
| created_at | DateTime | 생성 시각 |
| updated_at | DateTime | 수정 시각 |

### AlertRule

알림 규칙 정의.

| Field | Type | Description |
|-------|------|-------------|
| id | Integer PK | Auto-increment |
| name | String(100), not null | 규칙 이름 |
| type | String(30), not null | `db_health`, `upstream_health`, `error_rate` |
| target | String(100), not null | 대상 식별자 (DB alias, upstream ID, `*` for all) |
| threshold | Float, nullable | 에러율 임계치 (%, error_rate 전용) |
| enabled | Boolean, default True | 활성 여부 |
| created_at | DateTime | 생성 시각 |
| updated_at | DateTime | 수정 시각 |

### AlertRuleChannel

규칙-채널 M:N 매핑 + 수신자.

| Field | Type | Description |
|-------|------|-------------|
| id | Integer PK | Auto-increment |
| rule_id | FK → AlertRule (CASCADE) | 알림 규칙 |
| channel_id | FK → AlertChannel (CASCADE) | 채널 |
| recipients | Text, not null | 수신자 목록 (JSON array) |

Unique constraint: (rule_id, channel_id)

### AlertHistory

알림 발송 이력.

| Field | Type | Description |
|-------|------|-------------|
| id | Integer PK | Auto-increment |
| rule_id | FK → AlertRule (SET NULL), nullable | 알림 규칙 |
| channel_id | FK → AlertChannel (SET NULL), nullable | 채널 |
| alert_type | String(20), not null | `triggered` / `resolved` |
| target | String(100), not null | 대상 이름 |
| message | Text, not null | 상세 내용 |
| recipients | Text, nullable | 발송 대상 (JSON array) |
| sent_at | DateTime | 발송 시각 |
| success | Boolean | 발송 성공 여부 |
| error_detail | Text, nullable | 실패 시 에러 내용 |

## Backend Architecture

### New Files

```
unibridge-service/app/
├── services/
│   ├── alert_checker.py      # 60초 주기 헬스 체크 루프
│   ├── alert_sender.py       # webhook 발송 + 템플릿 렌더링
│   └── alert_state.py        # 인메모리 상태 추적 (중복 방지)
├── routers/
│   └── alerts.py             # CRUD API (규칙, 채널, 이력)
├── schemas.py                # AlertChannel/Rule/History 스키마 추가
├── models.py                 # AlertChannel/Rule/RuleChannel/History 모델 추가
```

### alert_state.py — State Tracking

인메모리 딕셔너리로 상태 전이 추적:

```python
# key: (rule_type, target) → value: "ok" | "alert"
# 서버 재시작 시 "ok"로 초기화
# 상태 전이 시에만 알림 발송:
#   ok → alert: triggered 알림
#   alert → ok: resolved 알림
```

### alert_checker.py — Health Check Loop

FastAPI lifespan에서 asyncio 백그라운드 태스크로 실행.

```
매 60초:
  1. 활성화된 AlertRule 목록 DB 조회
  2. 타입별 체크 실행:
     - db_health: connection_manager.test_connection(target)
     - upstream_health: APISIX admin API로 upstream node 상태 확인
     - error_rate: prometheus_client.instant_query() → 5xx 비율 vs threshold
  3. 결과를 alert_state와 비교하여 상태 전이 감지
  4. 전이 발생 시 alert_sender로 발송
  5. AlertHistory에 기록
```

### alert_sender.py — Webhook Dispatch

```
1. AlertRuleChannel에서 매핑된 채널 + 수신자 조회
2. 채널의 payload_template에서 플레이스홀더 치환:
   - {{alert_type}}: "triggered" | "resolved"
   - {{target_name}}: 대상 이름
   - {{status}}: 현재 상태 설명
   - {{message}}: 상세 내용
   - {{timestamp}}: ISO 8601 발생 시각
   - {{recipients}}: 수신자 목록 (comma-separated)
3. 채널의 headers와 함께 httpx.AsyncClient로 POST 발송
4. 발송 결과(성공/실패) 반환
```

## API Endpoints

Permission: `alerts.read`, `alerts.write`

### Channels

| Method | Path | Description |
|--------|------|-------------|
| GET | `/alerts/channels` | 채널 목록 |
| POST | `/alerts/channels` | 채널 생성 |
| PUT | `/alerts/channels/{id}` | 채널 수정 |
| DELETE | `/alerts/channels/{id}` | 채널 삭제 |
| POST | `/alerts/channels/{id}/test` | 테스트 발송 |

### Rules

| Method | Path | Description |
|--------|------|-------------|
| GET | `/alerts/rules` | 규칙 목록 (채널/수신자 포함) |
| POST | `/alerts/rules` | 규칙 생성 (채널/수신자 매핑 포함) |
| PUT | `/alerts/rules/{id}` | 규칙 수정 |
| DELETE | `/alerts/rules/{id}` | 규칙 삭제 |

### History & Status

| Method | Path | Description |
|--------|------|-------------|
| GET | `/alerts/history` | 발송 이력 (필터: type, target, from/to, limit/offset) |
| GET | `/alerts/status` | 현재 알림 상태 (정상/장애 목록) |

## Frontend UI

### Alert Settings Page (`/alerts/settings`)

두 탭 구성:

**Channels 탭:**
- 채널 목록 테이블 (이름, URL, 활성 상태)
- 채널 추가/수정 모달: name, webhook_url, payload_template (코드 에디터), headers, enabled
- 테스트 발송 버튼

**Rules 탭:**
- 규칙 목록 테이블 (이름, 타입, 대상, 활성 상태)
- 규칙 추가/수정 모달: name, type 선택, target 선택 (DB 목록/upstream 목록에서), threshold (error_rate일 때), 채널 선택 + 수신자 입력
- 규칙 활성/비활성 토글

### Alert History Page (`/alerts/history`)

- 발송 이력 테이블 (시각, 타입, 대상, 메시지, 성공 여부)
- 필터: alert_type, target, 날짜 범위
- 페이지네이션

### Sidebar Navigation

기존 사이드바에 "Alerts" 섹션 추가:
- Alert Settings (alerts.write 권한)
- Alert History (alerts.read 권한)

## Flow Example

```
[order-db 연결 실패]
  → alert_checker: test_connection("order-db") = fail
  → alert_state: ("db_health", "order-db") ok → alert 전이
  → AlertRule 조회: type=db_health, target="order-db" or "*"
  → AlertRuleChannel에서 채널/수신자 조회
  → 채널 템플릿 렌더링:
    POST https://mail.internal/api/send
    {
      "to": ["backend-team@company.com"],
      "subject": "[UniBridge] DB 연결 실패: order-db",
      "body": "order-db 연결이 실패했습니다.\n시각: 2026-04-11T14:30:00+09:00"
    }
  → AlertHistory에 기록

[order-db 복구]
  → alert_state: ("db_health", "order-db") alert → ok 전이
  → 복구 알림 발송 (alert_type: "resolved")
  → AlertHistory에 기록
```

## Error Handling

- Webhook 발송 실패 시: AlertHistory에 success=False, error_detail 기록. 체크 루프는 중단하지 않음.
- DB 세션 에러 시: 해당 사이클 skip, 다음 사이클에 재시도.
- APISIX/Prometheus 접속 불가 시: 해당 체크 타입만 skip, 로그 경고.

## Permissions

기존 RBAC 시스템에 추가:
- `alerts.read` — 알림 설정 조회, 이력 조회
- `alerts.write` — 채널/규칙 생성/수정/삭제
- `administrator` 시스템 역할에 기본 포함
