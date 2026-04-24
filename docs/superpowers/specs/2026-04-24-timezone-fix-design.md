# 전역 타임존 일관성 수정

## 배경

UniBridge 전역에서 타임스탬프 표시가 한국 시간과 어긋난다. 감사 로그(Audit Logs) 탭에서 처음 보고되었고, 교차 감사 결과 같은 문제가 Alert History, Alert Status, S3 Browser 등 여러 페이지에 퍼져 있다.

**근본 원인 체인 (naive datetime 전역 패턴):**

- 모든 `DateTime` 컬럼이 SQLAlchemy `timezone=False`(기본값). `models.py`에만 13개 — `created_at`, `updated_at`, `timestamp`, `sent_at` 등.
- `server_default=func.now()` 사용 → DB/컨테이너 로컬 시간으로 저장. 현재 컨테이너는 `TZ` 환경변수 미설정으로 UTC 기본 동작 중이 확인됨 (`docker compose exec unibridge-service date` = `UTC`).
- Pydantic 응답 스키마가 naive datetime을 그대로 직렬화 → JSON이 `"2026-04-22T09:00:00"` (offset/`Z` 없음).
- 프론트엔드는 `new Date(ts).toLocaleString()` 패턴을 4곳(`AuditLogs.tsx`, `AlertHistory.tsx`, `AlertStatus.tsx`, `S3Browser.tsx`)에서 사용. offset 없는 ISO 문자열은 브라우저마다 해석이 갈리며, 대부분 **로컬 시간으로 해석** → 실제 UTC 09:00이 KST 09:00으로 잘못 표시되어 **9시간 틀어짐**.

## 목표

- 백엔드는 **항상 UTC tz-aware**로 저장·직렬화한다.
- 프론트엔드는 **항상 KST 고정**으로 표시한다.
- 기존 저장된 값은 컨테이너가 UTC였으므로 **재계산 불필요** — 메타데이터 해석만 UTC로 태그.
- 영향 받는 4개 페이지 모두 공통 유틸을 통해 일관되게 표시한다.

## 비목표

- **DB DDL 마이그레이션 / Alembic 도입**: SQLite는 `DateTime`과 `DateTime(timezone=True)`를 모두 TEXT로 저장하므로 컬럼 타입 변경이 DB에 영향을 주지 않는다. 코드 변경만으로 해결 가능.
- **naive 데이터 backfill**: 컨테이너가 UTC였으므로 기존 naive 값 = UTC로 해석하면 올바름. 실제 값 재계산 없음.
- **사용자별 타임존 설정**: KST 고정. 향후 `formatKST`를 `formatTime(value, tz?)`로 확장할 여지만 둔다.
- **로그 파이프라인 / `docker logs` 타임스탬프 KST 변환**: 로그 뷰어/수집 쪽 별개 과제.
- **`unibridge-service` 외 컨테이너(keycloak, apisix, litellm 등)의 TZ 설정**: 각각 독립된 관심사. 본 스펙은 UniBridge 애플리케이션 경로에 한정.
- **실시간 갱신 주기, UI 레이아웃 변경**: 무관.

## 설계

### 1. 백엔드 — `UtcDateTime` TypeDecorator

**새 파일:** `unibridge-service/app/db_types.py`

```python
from datetime import timezone
from typing import Any

from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import DateTime, TypeDecorator


class UtcDateTime(TypeDecorator):
    """모든 DB 읽기/쓰기에서 UTC tz-aware datetime을 보장한다.

    - 쓰기: naive 입력은 UTC로 간주하고, aware 입력은 UTC로 정규화한 뒤 저장.
    - 읽기: DB에서 나온 naive 값은 UTC로 태그, aware 값은 UTC로 정규화해서 반환.
    - SQLite(TEXT)/PostgreSQL(timestamptz) 모두에서 일관 동작.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: Any, dialect: Dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
```

**모델 변경 (`unibridge-service/app/models.py`):**

- 모든 `Column(DateTime, ...)`를 `Column(UtcDateTime, ...)`로 교체 (13곳 + 다른 모듈에서 사용되는 곳 모두 grep으로 누락 확인).
- `server_default=func.now()` → Python side default로 통일:
  ```python
  from datetime import datetime, timezone
  default=lambda: datetime.now(timezone.utc)
  ```
  - `onupdate=func.now()`도 동일하게 `onupdate=lambda: datetime.now(timezone.utc)`로.
  - 이유: `func.now()`는 DB 네이티브 함수라 DB 서버 타임존에 영향받음. Python side default가 컨테이너 TZ와 무관하게 UTC 보장.
- `from app.db_types import UtcDateTime` 추가, 기존 `DateTime` 직접 import는 이 파일에서 제거(또는 유지하되 사용 안 함).

**Pydantic 응답 (`schemas.py`):**

- datetime 필드는 변경 불필요. tz-aware datetime은 FastAPI/Pydantic 기본 동작으로 `"2026-04-22T09:00:00+00:00"`으로 직렬화된다.
- 확인: 몇 곳(e.g. `schemas.py:104` 인근)이 명시적으로 `datetime` 타입을 선언하고 있으니 그대로 두면 된다.

### 2. Docker — `unibridge-service`에 `TZ=UTC` 명시

**파일:** `docker-compose.yml`

```yaml
  unibridge-service:
    ...
    environment:
      - TZ=UTC  # 추가
      ...
```

현재 컨테이너가 우연히 UTC인 상태를 명시적 계약으로 고정. 호스트 OS 변경이나 베이스 이미지 업데이트에도 안전. `func.now()` 잔여 호출이 어딘가 있더라도 UTC 반환 보장.

**범위:** `unibridge-service`만. 다른 서비스(keycloak, apisix, litellm 등)는 별개의 관심사라 본 스펙에서 다루지 않는다.

### 3. 프론트엔드 — 공통 유틸 + 4곳 교체

**새 파일:** `unibridge-ui/src/utils/time.ts`

```typescript
const KST_OPTS: Intl.DateTimeFormatOptions = {
  timeZone: 'Asia/Seoul',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
};

export function formatKST(value: string | Date | null | undefined): string {
  if (!value) return '—';
  const d = typeof value === 'string' ? new Date(value) : value;
  if (Number.isNaN(d.getTime())) {
    return typeof value === 'string' ? value : '';
  }
  return d.toLocaleString('ko-KR', KST_OPTS);
}
```

**교체 대상 4곳:**

| 파일 | 위치 | 조치 |
|---|---|---|
| `unibridge-ui/src/pages/AuditLogs.tsx` | `formatTimestamp` (~71-77) | 로컬 함수 삭제, 호출부를 `formatKST`로 변경 |
| `unibridge-ui/src/pages/AlertHistory.tsx` | ~47-52 | 동일 |
| `unibridge-ui/src/pages/AlertStatus.tsx` | ~18-24 | 동일 |
| `unibridge-ui/src/pages/S3Browser.tsx` | 해당 렌더 지점 | 동일 |

추가로 `unibridge-ui/src/pages/` 전체에서 `new Date(...).toLocaleString` 패턴을 grep해 누락된 곳이 없는지 확인. 있으면 본 스펙 범위에 포함.

### 데이터 흐름

```
write path:
  app code → datetime.now(timezone.utc) (aware, UTC)
  SQLAlchemy UtcDateTime.process_bind_param → UTC aware 확인
  SQLite TEXT / Postgres timestamptz 저장 (둘 다 UTC 계산)

read path:
  DB → SQLAlchemy UtcDateTime.process_result_value → UTC aware datetime
  Pydantic 직렬화 → "2026-04-22T09:00:00+00:00"
  API 응답 → 프론트엔드
  formatKST(iso) → new Date(iso) (UTC로 정확히 해석)
    → toLocaleString('ko-KR', { timeZone: 'Asia/Seoul' })
    → "2026. 04. 22. 18:00:00" (KST 표시)
```

## 테스트

### 백엔드

**`unibridge-service/tests/test_db_types.py`** (신규):
- `UtcDateTime.process_bind_param`이 naive → UTC aware로 태그, aware → UTC 정규화, None → None을 반환
- `process_result_value`가 naive(기존 데이터) → UTC 태그, aware → UTC 정규화, None → None
- 다른 타임존 입력(KST, +05:00 등)이 모두 UTC로 변환되는지

**`unibridge-service/tests/test_audit.py`** 또는 해당 테스트 파일 보강:
- 감사 로그를 생성한 뒤 API 응답의 `timestamp` 필드가 `+00:00` 또는 `Z`로 끝나는 ISO 문자열인지
- 기존 naive timestamp가 DB에 남아있는 상황 시뮬레이션(raw INSERT) → 응답에서 UTC tag가 올바르게 붙는지

**회귀:** `pytest tests/`의 기존 테스트 전체가 계속 통과해야 함. DateTime 관련 assertion(특히 `datetime.fromisoformat` 비교, naive datetime 비교)이 깨지는 곳은 해당 테스트를 UTC aware 기준으로 수정.

### 프론트엔드

**`unibridge-ui/src/test/time.test.ts`** (신규):
- `formatKST('2026-04-22T00:00:00+00:00')` → `"2026. 04. 22. 09:00:00"` (또는 테스트 환경의 ko-KR Intl 포맷)
- `formatKST('2026-04-22T12:00:00Z')` → KST 21:00
- `formatKST(null)` → `'—'`, `formatKST(undefined)` → `'—'`
- `formatKST('invalid-date')` → 원본 문자열 반환 (폴백)
- `formatKST(new Date('2026-04-22T00:00:00Z'))` → Date 입력도 처리

**기존 페이지 렌더링 테스트 보강:**
- `AuditLogs.test.tsx`, `AlertHistory.test.tsx`, `AlertStatus.test.tsx` (있는 경우) — 모킹된 타임스탬프 응답으로 KST 포맷 문자열이 렌더되는지 확인

## 마이그레이션 / 배포 고려

- **기존 DB 파일 그대로 유지.** 컨테이너가 UTC였으므로 저장된 naive 값은 올바른 UTC 기준. `UtcDateTime.process_result_value`가 읽을 때 자동으로 UTC 태그를 붙인다.
- **컨테이너 재시작 시점**: `TZ=UTC` 환경변수 추가는 재배포 필요. 애플리케이션 로직은 Python side default로 바뀌어서 컨테이너 TZ에 의존하지 않으므로 일시적 혼재 상황에서도 안전.
- **관찰:** 배포 직후 첫 몇 개의 감사 로그가 KST로 정상 표시되는지 UI에서 확인.

## 영향 범위 체크리스트

| 영역 | 파일 |
|---|---|
| 백엔드 타입 | `unibridge-service/app/db_types.py` (신규) |
| 백엔드 모델 | `unibridge-service/app/models.py` |
| Docker | `docker-compose.yml` |
| UI 유틸 | `unibridge-ui/src/utils/time.ts` (신규) |
| UI 페이지 | `AuditLogs.tsx`, `AlertHistory.tsx`, `AlertStatus.tsx`, `S3Browser.tsx` (+ grep으로 발견되는 추가 위치) |
| 백엔드 테스트 | `tests/test_db_types.py` (신규), 감사 로그 관련 테스트 보강 |
| 프론트 테스트 | `src/test/time.test.ts` (신규), 기존 페이지 테스트 보강 |

## 오픈 이슈 / 후속 과제

- 다른 컨테이너(keycloak, apisix 등)의 TZ 일관성은 별도 점검 필요 시 후속 과제.
- 사용자별 타임존 설정 기능이 필요해지면 `formatKST` 확장.
- `docker logs` 포함 로그 뷰어의 KST 표시 편의 기능.
