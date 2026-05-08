# 리소스별 담당자 기반 메일 알림 라우팅 설계

- 작성일: 2026-05-08
- 상태: Draft (사용자 검토 대기)
- 대상 모듈: `unibridge-service/app/{models,routers,schemas,services}`, `unibridge-ui/src/{api,pages,test}`

## 1. 배경과 목표

현재 알림 시스템은 `AlertChannel`, `AlertRule`, `AlertRuleChannel`을 통해 webhook을 호출한다. 사내 메일 API도 webhook 채널로 등록할 수 있고, payload template의 `{{recipients}}` 변수로 수신자를 넣을 수 있다.

운영 문제는 수신자 설정이 룰 중심이라는 점이다. 장애가 생길 때마다 "이 리소스 담당자가 누구인가"를 룰과 채널 매핑에서 반복 설정해야 하므로, DB나 Route별 담당자에게 자동으로 메일을 보내는 작업이 번거롭다.

이번 변경의 목표는 다음과 같다.

- 사내 메일 API webhook 채널은 기존 `AlertChannel`로 계속 관리한다.
- 리소스별 담당자 그룹을 별도로 관리한다.
- 장애 발생 시 `resource_type + resource_id`로 담당자 그룹을 찾고, 담당자가 없으면 기본 담당자 그룹으로 보낸다.
- 회사 메일 API 전용 payload 구조는 코드에 하드코딩하지 않고 채널 템플릿 설정으로 표현한다.
- 기존 상태 전이 기반 중복 방지와 복구 알림 흐름은 유지한다.

비목표:

- Slack, Discord, SMTP 같은 새 채널 타입 추가
- 온콜 로테이션, 에스컬레이션, 근무 시간 정책
- 기존 알림 체크 타입의 전면 재설계
- 모든 기존 `AlertRule` 제거

## 2. 핵심 결정

### 2.1 메일 채널 식별

시스템이 어떤 webhook이 메일 발송 채널인지 자동 추론하지 않는다. 관리자가 전역 알림 설정에서 기존 `AlertChannel` 중 하나를 기본 메일 채널로 지정한다.

```text
AlertSettings.mail_channel_id -> AlertChannel.id
```

알림 발송 시 담당자 이메일 목록은 이 메일 채널의 template에 주입된다.

### 2.2 담당자 그룹

한 리소스에는 담당자 한 명이 아니라 담당자 그룹 하나를 매핑한다. 담당자 그룹은 여러 이메일을 가진다.

```text
OwnerGroup("결제팀") -> ["pay-oncall@company.com", "kim@company.com"]
ResourceOwner("db", "payment-db") -> OwnerGroup("결제팀")
```

리소스에 담당자 그룹이 없으면 `AlertSettings.fallback_owner_group_id`를 사용한다. fallback도 없으면 발송하지 않고 이력과 로그에 "수신자 미설정"으로 남긴다.

### 2.3 회사 API 특화 구조 처리

회사 메일 API는 여러 수신자를 다음처럼 받는다.

```json
{
  "recipients": [
    {
      "emailAddress": "kim@company.com",
      "recipientType": "TO"
    }
  ]
}
```

`emailAddress`, `recipientType` 같은 필드는 UniBridge 코드에 넣지 않는다. 대신 `AlertChannel.recipient_item_template`을 추가한다.

예시 채널 설정:

```json
{
  "emailAddress": "{{email}}",
  "recipientType": "TO"
}
```

메일 payload template은 `{{recipients_json}}`을 사용한다.

```json
{
  "recipients": {{recipients_json}},
  "subject": "[UniBridge] {{target_name}} 장애",
  "body": "{{message}}"
}
```

발송 시 시스템은 담당자 이메일마다 `recipient_item_template`을 렌더링하고, 결과 객체 배열을 `{{recipients_json}}` 자리에 raw JSON으로 넣는다. 기존 `{{recipients}}`는 사람이 읽는 문자열 변수로 유지한다.

## 3. 데이터 모델

### AlertChannel 변경

기존 `alert_channels` 테이블을 유지하고 컬럼만 추가한다.

```sql
ALTER TABLE alert_channels
ADD COLUMN recipient_item_template TEXT NULL;
```

- `recipient_item_template`은 선택값이다.
- 기본 메일 채널로 지정된 채널은 `{{recipients_json}}`을 쓰려면 이 값을 설정해야 한다.
- 값은 JSON 객체 템플릿이어야 한다. 렌더링 후 JSON 객체로 파싱되지 않으면 채널 저장 또는 테스트 발송에서 오류를 반환한다.

### OwnerGroup

```sql
owner_groups (
  id          INTEGER PRIMARY KEY,
  name        VARCHAR(100) UNIQUE NOT NULL,
  emails      TEXT NOT NULL,       -- JSON array of strings
  enabled     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at  UtcDateTime NOT NULL,
  updated_at  UtcDateTime NOT NULL
)
```

규칙:

- `emails`는 비어 있지 않은 문자열 배열이어야 한다.
- 중복 이메일은 저장 시 제거한다.
- 비활성 그룹은 발송 대상에서 제외한다. 리소스 담당자 그룹이 비활성이면 fallback 그룹을 사용한다.

### ResourceOwner

```sql
resource_owners (
  id             INTEGER PRIMARY KEY,
  resource_type  VARCHAR(20) NOT NULL,   -- db | s3 | route | upstream
  resource_id    VARCHAR(200) NOT NULL,
  owner_group_id INTEGER NOT NULL REFERENCES owner_groups(id) ON DELETE RESTRICT,
  created_at     UtcDateTime NOT NULL,
  updated_at     UtcDateTime NOT NULL,
  UNIQUE(resource_type, resource_id)
)
```

리소스 식별자는 다음 기준을 사용한다.

- `db`: DB connection alias
- `s3`: S3 connection alias
- `route`: APISIX route id
- `upstream`: APISIX upstream id

owner 매핑 저장 시 가능한 리소스는 존재 여부를 검증한다. APISIX 조회 실패처럼 외부 시스템 일시 장애가 있으면 503을 반환하고, 잘못된 id면 422를 반환한다.

### AlertSettings

```sql
alert_settings (
  id                       INTEGER PRIMARY KEY CHECK(id = 1),
  mail_channel_id           INTEGER NULL REFERENCES alert_channels(id) ON DELETE RESTRICT,
  fallback_owner_group_id   INTEGER NULL REFERENCES owner_groups(id) ON DELETE RESTRICT,
  route_error_threshold_pct FLOAT NOT NULL DEFAULT 10.0,
  check_interval_seconds    INTEGER NOT NULL DEFAULT 60,
  updated_at                UtcDateTime NOT NULL
)
```

설정 row는 하나만 존재한다. 부팅 또는 마이그레이션 후 row가 없으면 기본값으로 생성한다.

### AlertHistory 변경

기존 이력과 호환되도록 기존 컬럼을 유지하고 리소스 식별 정보를 추가한다.

```sql
ALTER TABLE alert_history ADD COLUMN resource_type VARCHAR(20) NULL;
ALTER TABLE alert_history ADD COLUMN owner_group_id INTEGER NULL REFERENCES owner_groups(id) ON DELETE SET NULL;
```

- 새 owner 기반 발송은 `resource_type`, `owner_group_id`, `channel_id`를 기록한다.
- 기존 `rule_id`, `recipients`, `target`, `success`, `error_detail`은 유지한다.
- `recipients`에는 발송에 사용한 이메일 배열 JSON을 저장한다.

## 4. API 설계

모든 endpoint는 기존 `alerts.read`, `alerts.write` 권한을 사용한다.

### Settings

```text
GET /admin/alerts/settings
PUT /admin/alerts/settings
```

설정 항목:

- `mail_channel_id`
- `fallback_owner_group_id`
- `route_error_threshold_pct`
- `check_interval_seconds`

### Owner Groups

```text
GET    /admin/alerts/owner-groups
POST   /admin/alerts/owner-groups
PUT    /admin/alerts/owner-groups/{id}
DELETE /admin/alerts/owner-groups/{id}
```

삭제 정책:

- `resource_owners` 또는 `alert_settings.fallback_owner_group_id`에서 사용 중이면 409를 반환한다.
- 단순 비활성화는 허용한다.

### Resource Owners

```text
GET    /admin/alerts/resource-owners
PUT    /admin/alerts/resource-owners/{resource_type}/{resource_id}
DELETE /admin/alerts/resource-owners/{resource_type}/{resource_id}
```

`GET`은 현재 등록 가능한 리소스 목록과 담당자 그룹 매핑 상태를 함께 반환한다. UI는 이 응답으로 "담당자 미지정" 리소스를 표시한다.

### Channel Test 확장

기존 `POST /admin/alerts/channels/{id}/test`는 다음 값을 테스트 payload에 넣는다.

- `recipients`: `"test@example.com"`
- `recipients_json`: `recipient_item_template`을 사용해 만든 JSON 배열

채널이 기본 메일 채널로 지정되어 있고 `recipient_item_template`이 없거나 잘못된 JSON이면 테스트 실패로 반환한다.

## 5. 발송 흐름

```text
1. alert_checker가 장애 또는 복구 전이를 감지한다.
2. checker는 resource_type, resource_id, display_target, message를 만든다.
3. dispatcher가 ResourceOwner에서 담당자 그룹을 찾는다.
4. 담당자 그룹이 없거나 비활성이면 fallback owner group을 사용한다.
5. 유효한 담당자 그룹이 없으면 발송하지 않고 경고 이력을 남긴다.
6. AlertSettings.mail_channel_id로 메일 채널을 로드한다.
7. owner group 이메일 배열을 recipient_item_template으로 렌더링해 recipients_json을 만든다.
8. payload_template을 렌더링해 webhook으로 POST한다.
9. AlertHistory에 성공/실패, owner_group_id, channel_id, recipients를 기록한다.
```

`alert_checker`는 기존 체크 타입을 유지한다.

- `db_health`: `resource_type = "db"`, `resource_id = alias`
- `upstream_health`: `resource_type = "upstream"`, `resource_id = upstream id`
- `route_error_rate`: `resource_type = "route"`, `resource_id = route id`
- `error_rate`: 글로벌 알림이라 리소스 담당자 모델과 맞지 않는다. 1차 변경에서는 기존 rule/channel 발송 흐름을 유지한다.

route별 알림은 기존 룰별 threshold가 있을 수 있으므로 1차 변경에서는 감지 정책은 기존 `AlertRule`을 따른다. 단, 발송 수신자는 `AlertRuleChannel.recipients`가 아니라 `ResourceOwner`에서 결정한다.

## 6. 기존 AlertRule과 마이그레이션 정책

이번 변경에서는 `AlertRule`, `AlertRuleChannel` 테이블을 제거하지 않는다.

이유:

- 기존 체크 로직이 `AlertRule`의 type, target, threshold에 의존한다.
- route별 threshold 같은 감지 정책은 여전히 룰 개념이 필요하다.
- 수신자 라우팅만 분리하면 운영 불편을 해결하면서 마이그레이션 위험을 낮출 수 있다.

동작 변경:

- 새 owner 기반 메일 발송 경로에서는 `AlertRuleChannel.recipients`를 사용하지 않는다.
- 기존 rule-channel 매핑은 호환 목적으로 유지한다.
- UI의 룰 생성/수정 모달에서는 수신자 입력을 제거한다. 기존 rule-channel 매핑이 있는 룰의 상세 영역에는 읽기 전용 "기존 수신자 설정"으로만 표시하고, 새 발송 경로에서는 사용되지 않는다고 안내한다.

마이그레이션:

- `recipient_item_template` 컬럼 추가
- `owner_groups`, `resource_owners`, `alert_settings` 생성
- `alert_history.resource_type`, `alert_history.owner_group_id` 추가
- 기존 데이터 자동 변환은 최소화한다. 기존 `AlertRuleChannel.recipients`를 owner group으로 자동 변환하지 않는다. 같은 수신자 조합이 여러 룰에 반복될 수 있고, 어떤 리소스의 소유자인지 확정하기 어렵기 때문이다.

## 7. UI 설계

`AlertSettings` 화면을 기존 탭 구조에서 다음 탭으로 재구성한다.

### Mail Channel

- 기본 메일 채널 드롭다운
- 선택된 채널의 webhook URL, payload template 요약
- `recipient_item_template` 편집 영역
- `{{recipients_json}}`, `{{recipients}}`, `{{email}}` 변수 설명
- 테스트 발송 버튼

### Owner Groups

- 그룹 목록: 이름, 이메일 개수, 활성 여부
- 생성/수정 모달: 이름, 이메일 목록, 활성 여부
- 삭제 시 사용 중이면 409 메시지 표시

### Resource Owners

- 리소스 타입 필터: DB, S3, Route, Upstream
- 리소스 목록: 이름/id, 현재 담당자 그룹, 상태
- 담당자 그룹 드롭다운으로 즉시 지정
- 미지정 리소스 강조 표시
- fallback 그룹 안내

### Rules

- 기존 룰 목록은 유지한다.
- 룰 생성/수정에서는 수신자 설정을 받지 않는다.
- 기존 rule-channel 수신자 데이터는 상세 보기에서 읽기 전용 legacy 정보로만 표시한다.
- 룰 생성/수정은 감지 타입, target, threshold, enabled에 집중한다.

## 8. 오류 처리

- `mail_channel_id`가 없으면 owner 기반 메일 발송을 하지 않고 이력에 실패로 기록한다.
- 리소스 담당자와 fallback 모두 없으면 webhook을 호출하지 않고 이력에 실패로 기록한다.
- `recipient_item_template` 렌더링 결과가 JSON 객체가 아니면 발송 실패로 기록한다.
- payload template에서 `{{recipients_json}}`이 JSON 문자열로 감싸져 잘못된 payload가 되면 테스트 발송에서 발견할 수 있게 한다.
- webhook 실패는 기존처럼 `AlertHistory.success=false`, `error_detail`에 기록한다.

## 9. 테스트 전략

백엔드:

- `recipient_item_template`으로 이메일 목록을 JSON 객체 배열로 렌더링하는 단위 테스트
- `{{recipients_json}}` raw JSON 삽입 테스트
- owner group CRUD와 validation 테스트
- resource owner upsert/delete와 리소스 존재 검증 테스트
- 담당자 그룹 없음, fallback 사용, fallback 없음, 비활성 그룹 처리 테스트
- 기존 route rule threshold가 유지되면서 수신자만 owner group으로 바뀌는 checker 테스트
- channel test endpoint가 `recipients_json`을 포함하는지 테스트

프론트엔드:

- Mail Channel 설정 저장과 테스트 버튼
- Owner Groups CRUD
- Resource Owners 리소스 타입 필터와 담당자 지정
- 미지정 리소스 표시
- Rules 탭에서 수신자 입력이 제거되거나 deprecated로 표시되는지 검증

## 10. 롤아웃 순서

1. DB 마이그레이션 적용
2. 기본 메일 채널 선택
3. 메일 채널에 `recipient_item_template` 설정
4. fallback owner group 생성 및 지정
5. 주요 DB, S3, Route, Upstream에 owner group 지정
6. 테스트 발송으로 사내 메일 API payload 검증
7. 알림 checker 발송 경로를 owner 기반으로 전환
