# DB Security Hardening Design

## Overview

API Hub의 query-service에 누락된 보안 기능 4가지를 추가한다.
기존 권한 체계(Role + Permission + DB alias 단위)를 확장하여 테이블 단위 접근 제어, 요청 제한, SQL 검증을 구현한다.

## 1. 테이블 단위 접근 제어

### 목적
역할(role) + DB별로 접근 가능한 테이블을 화이트리스트 방식으로 제한한다.

### 모델 변경
- `Permission` 모델에 `allowed_tables` 필드 추가 (Text, nullable=True, JSON 배열)
- `null`이면 해당 DB의 모든 테이블 접근 가능 (하위 호환)
- 값이 있으면 명시된 테이블만 허용

### API
- `GET /admin/query/databases/{alias}/tables` — 타겟 DB에서 실시간 테이블 목록 조회
  - PostgreSQL: `information_schema.tables` (public 스키마)
  - MSSQL: `information_schema.tables`
- 허용 테이블 저장/수정 시 (`PUT /admin/query/permissions`) 실제 DB에 테이블 존재 여부 검증 → 없으면 400

### 서비스 — `services/table_access.py`
- SQL에서 참조 테이블명 추출 (정규식 기반)
  - `FROM`, `JOIN`, `INTO`, `UPDATE` 뒤의 테이블명 파싱
  - 기존 `_strip_strings_and_comments()` 활용하여 문자열/주석 제거 후 파싱
- 추출된 테이블 목록과 `allowed_tables` 대조
- 허용되지 않은 테이블이 하나라도 있으면 403 반환

### 제한 사항
- 정규식 기반 파싱이므로 동적 SQL, 복잡한 서브쿼리에서 누락 가능성 존재
- CTE(`WITH`)는 문자열/주석 제거 후 파싱으로 대응

### UI
- Permissions 페이지에서 역할+DB별 허용 테이블을 멀티셀렉트로 선택
- 테이블 목록은 `GET /admin/query/databases/{alias}/tables`에서 실시간 조회

## 2. Rate Limiting

### 목적
사용자별 분당 요청 수를 제한하여 무분별한 DB 요청을 방지한다.

### 구현 — `middleware/rate_limiter.py`
- 슬라이딩 윈도우 방식, 인메모리 딕셔너리
- 사용자별(username) 분당 60회 기본값
- `/query/execute` 엔드포인트에만 적용 (health check, admin API 제외)
- 초과 시 `429 Too Many Requests` 반환, `Retry-After` 헤더 포함
- 만료된 요청 기록은 다음 요청 시 자동 정리
- 서버 재시작 시 초기화

### 설정
- `config.py`: `RATE_LIMIT_PER_MINUTE: int = 60`
- 런타임 변경: `SystemConfig` 테이블에서 오버라이드 가능

## 3. 동시 쿼리 제한

### 목적
한 사용자가 동시에 너무 많은 쿼리를 실행하는 것을 방지한다.

### 구현 — `middleware/rate_limiter.py` (Rate Limiting과 동일 모듈)
- 사용자별 `asyncio.Semaphore(5)` 기반
- 초과 시 `429 Too Many Requests` + `"Too many concurrent queries"` 메시지
- 쿼리 완료 시 세마포어 해제

### 설정
- `config.py`: `MAX_CONCURRENT_QUERIES: int = 5`
- 런타임 변경: `SystemConfig` 테이블에서 오버라이드 가능

## 4. SQL 키워드 블랙리스트

### 목적
위험한 SQL 키워드를 사전 차단하여 DB 보안 사고를 방지한다.

### 서비스 — `services/sql_validator.py`
- 기존 `_strip_strings_and_comments()` 재활용하여 문자열/주석 제거 후 키워드 검사
- 기본 차단 키워드 (하드코딩):
  - 권한 조작: `GRANT`, `REVOKE`
  - 사용자 조작: `CREATE USER`, `DROP USER`, `ALTER USER`, `CREATE LOGIN`, `DROP LOGIN`
  - 시스템: `SHUTDOWN`, `KILL`, `BACKUP`, `RESTORE`
- 차단 시 `403 Forbidden` + 차단된 키워드 명시 메시지

### 블랙리스트 관리
- 기본 목록은 코드에 하드코딩 (변경 빈도 매우 낮음)
- 추가 차단 키워드: `SystemConfig` 테이블에 `"blocked_sql_keywords"` 키로 저장
- Admin API에서 조회/수정 가능

## 5. Admin 설정 관리

### 모델 — `SystemConfig`
```python
class SystemConfig(Base):
    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)
```

### API
- `GET /admin/query/settings` — 현재 설정값 조회
- `PUT /admin/query/settings` — 설정값 변경 (인메모리 즉시 반영)

### 설정 항목
| key | 기본값 | 설명 |
|-----|--------|------|
| `rate_limit_per_minute` | 60 | 사용자별 분당 요청 수 |
| `max_concurrent_queries` | 5 | 사용자별 최대 동시 쿼리 수 |
| `blocked_sql_keywords` | (없음) | 추가 차단 SQL 키워드 (JSON 배열) |

### 동작
- 서버 시작 시 DB에서 로드, 없으면 `config.py` 기본값 사용
- Admin API에서 변경 시 인메모리 값도 즉시 반영

## 파일 구조

### 신규 파일
- `query-service/app/services/table_access.py` — 테이블 접근 제어 로직
- `query-service/app/services/sql_validator.py` — SQL 키워드 블랙리스트
- `query-service/app/middleware/rate_limiter.py` — Rate limiting + 동시 쿼리 제한

### 변경 파일
- `query-service/app/models.py` — `Permission.allowed_tables`, `SystemConfig` 모델 추가
- `query-service/app/schemas.py` — 관련 Pydantic 스키마 추가
- `query-service/app/config.py` — `RATE_LIMIT_PER_MINUTE`, `MAX_CONCURRENT_QUERIES` 기본값
- `query-service/app/routers/admin.py` — 테이블 목록 조회, settings API 추가
- `query-service/app/routers/query.py` — `table_access`, `sql_validator` 검증 호출 추가
- `query-service/app/main.py` — 미들웨어 등록, `SystemConfig` 초기화
- `query-service/app/database.py` — `SystemConfig` 테이블 생성

### UI 변경
- `query-ui/src/pages/Permissions.tsx` — 테이블 멀티셀렉트 추가
- `query-ui/src/pages/Settings.tsx` (또는 기존 설정 페이지) — rate limit, 동시 쿼리, 차단 키워드 설정
- `query-ui/src/api/client.ts` — 새 API 엔드포인트 함수 추가
