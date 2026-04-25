# 전역 타임존 일관성 수정 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 백엔드는 항상 UTC tz-aware로 저장·직렬화하고 프론트엔드는 항상 KST 고정으로 표시하도록 데이터 경로 전체를 정렬한다.

**Architecture:** SQLAlchemy `UtcDateTime` TypeDecorator 하나로 모든 DB read/write의 UTC 보장. `models.py`의 모든 `DateTime` 컬럼을 이 타입으로 교체하고 `server_default=func.now()`를 Python `default=lambda: datetime.now(timezone.utc)`로 통일. 프론트엔드는 `formatKST` 유틸 하나를 만들어 4개 페이지에서 일관 사용.

**Tech Stack:** SQLAlchemy 2.0 (TypeDecorator), FastAPI + Pydantic (datetime 직렬화), React + TypeScript, Vitest (UI 단위 테스트), pytest (백엔드).

**Spec:** `docs/superpowers/specs/2026-04-24-timezone-fix-design.md`

---

## 파일 구조

**백엔드:**
- Create: `unibridge-service/app/db_types.py` — `UtcDateTime` TypeDecorator 단 하나
- Modify: `unibridge-service/app/models.py` — import 추가, 13곳 `DateTime` → `UtcDateTime`, `server_default`/`onupdate`의 `func.now()` → Python UTC default
- Create: `unibridge-service/tests/test_db_types.py` — TypeDecorator 단위 테스트
- Modify: `unibridge-service/tests/test_audit.py` (파일 존재 시 확장, 없으면 본 계획이 새로 생성) — UTC ISO 응답 통합 테스트

**Docker:**
- Modify: `docker-compose.yml` — `unibridge-service` 서비스 `environment`에 `TZ=UTC` 한 줄 추가

**프론트엔드:**
- Create: `unibridge-ui/src/utils/time.ts` — `formatKST` 유틸
- Create: `unibridge-ui/src/test/time.test.ts` — `formatKST` 단위 테스트
- Modify: `unibridge-ui/src/pages/AuditLogs.tsx` (라인 71-77 로컬 `formatTimestamp` 제거)
- Modify: `unibridge-ui/src/pages/AlertHistory.tsx` (라인 47-52)
- Modify: `unibridge-ui/src/pages/AlertStatus.tsx` (라인 18-24, `string | null` 시그니처 주의)
- Modify: `unibridge-ui/src/pages/S3Browser.tsx` (라인 28)

---

## Task 1: 백엔드 — `UtcDateTime` TypeDecorator + 단위 테스트

**Files:**
- Create: `unibridge-service/app/db_types.py`
- Create: `unibridge-service/tests/test_db_types.py`

- [ ] **Step 1: 실패 테스트 작성**

`unibridge-service/tests/test_db_types.py` 신규 생성:

```python
"""Unit tests for UtcDateTime TypeDecorator."""

from datetime import datetime, timezone, timedelta

import pytest

from app.db_types import UtcDateTime


KST = timezone(timedelta(hours=9))


class TestUtcDateTimeBindParam:
    def test_none_passthrough(self):
        col = UtcDateTime()
        assert col.process_bind_param(None, None) is None

    def test_naive_treated_as_utc(self):
        col = UtcDateTime()
        naive = datetime(2026, 4, 22, 9, 0, 0)
        out = col.process_bind_param(naive, None)
        assert out.tzinfo is not None
        assert out.utcoffset() == timedelta(0)
        assert out.hour == 9

    def test_aware_normalized_to_utc(self):
        col = UtcDateTime()
        kst = datetime(2026, 4, 22, 18, 0, 0, tzinfo=KST)
        out = col.process_bind_param(kst, None)
        assert out.utcoffset() == timedelta(0)
        assert out.hour == 9  # 18 KST - 9 = 09 UTC

    def test_already_utc_unchanged(self):
        col = UtcDateTime()
        utc = datetime(2026, 4, 22, 9, 0, 0, tzinfo=timezone.utc)
        out = col.process_bind_param(utc, None)
        assert out == utc


class TestUtcDateTimeResultValue:
    def test_none_passthrough(self):
        col = UtcDateTime()
        assert col.process_result_value(None, None) is None

    def test_naive_from_db_tagged_utc(self):
        col = UtcDateTime()
        naive = datetime(2026, 4, 22, 9, 0, 0)  # 기존 legacy naive 값 시뮬레이션
        out = col.process_result_value(naive, None)
        assert out.tzinfo is not None
        assert out.utcoffset() == timedelta(0)
        assert out.hour == 9

    def test_aware_from_db_normalized_to_utc(self):
        col = UtcDateTime()
        kst = datetime(2026, 4, 22, 18, 0, 0, tzinfo=KST)
        out = col.process_result_value(kst, None)
        assert out.utcoffset() == timedelta(0)
        assert out.hour == 9
```

- [ ] **Step 2: 테스트 실행해 실패 확인**

```bash
cd unibridge-service && .venv/bin/pytest tests/test_db_types.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.db_types'` 또는 import 실패로 collection error.

- [ ] **Step 3: `UtcDateTime` 구현**

`unibridge-service/app/db_types.py` 신규 생성:

```python
"""Shared SQLAlchemy column types."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import DateTime, TypeDecorator


class UtcDateTime(TypeDecorator):
    """Timezone-aware datetime column that always stores/returns UTC.

    - Write: naive input is treated as UTC; aware input is normalized to UTC.
    - Read: naive DB value (legacy rows) is tagged as UTC; aware value is
      normalized to UTC.
    - Works on SQLite (TEXT) and PostgreSQL (timestamptz).
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self, value: Optional[datetime], dialect: Dialect
    ) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(
        self, value: Optional[datetime], dialect: Dialect
    ) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


__all__ = ["UtcDateTime"]
```

- [ ] **Step 4: 테스트 통과 확인**

```bash
cd unibridge-service && .venv/bin/pytest tests/test_db_types.py -v
```

Expected: 7 tests PASS (3 bind_param + 3 result_value + none_passthroughs 중복 없음 — 위 파일 기준 7개).

- [ ] **Step 5: 커밋**

```bash
git add unibridge-service/app/db_types.py unibridge-service/tests/test_db_types.py
git commit -m "feat(db): add UtcDateTime TypeDecorator for UTC-aware columns"
```

---

## Task 2: `models.py` 전체 DateTime → UtcDateTime 교체

**Files:**
- Modify: `unibridge-service/app/models.py`

- [ ] **Step 1: 현재 상태 확인**

```bash
grep -n "Column(DateTime\|^from\|import DateTime" unibridge-service/app/models.py | head -20
```

Expected: 13곳의 `Column(DateTime, ...)` 사용 확인, import 블록 위치 확인.

- [ ] **Step 2: import 조정**

`unibridge-service/app/models.py`의 파일 상단 import 블록 수정:

기존:
```python
from sqlalchemy import (
    ...
    DateTime,
    ...
)
```

그대로 두되(다른 곳에서 쓸 수 있음 — 없으면 삭제 가능), 아래 두 줄을 새로 추가:

```python
from datetime import datetime, timezone

from app.db_types import UtcDateTime
```

import 위치는 기존 `from sqlalchemy.orm import DeclarativeBase` 근처. 프로젝트 스타일에 맞게 stdlib → 3rd party → local 순.

- [ ] **Step 3: 모든 DateTime 컬럼 교체**

다음 13개 라인을 일괄 치환. `sed` 한 번에 안전:

```bash
# models.py 내 Column(DateTime, ...) → Column(UtcDateTime, ...)
# 동시에 server_default=func.now() → default=lambda: datetime.now(timezone.utc)
# onupdate=func.now() → onupdate=lambda: datetime.now(timezone.utc)
```

정확히는 편집 도구로 한 줄씩 교체 (Edit 도구 사용). 총 13줄:

라인 36, 51, 78, 105, 133, 150, 177 (created_at / timestamp / sent_at):
변경 전:
```python
created_at = Column(DateTime, server_default=func.now())
```
변경 후:
```python
created_at = Column(UtcDateTime, default=lambda: datetime.now(timezone.utc))
```

(`timestamp` 행은 컬럼명만 다름. `sent_at`도 동일.)

라인 37, 52, 79, 121, 134, 151 (updated_at):
변경 전:
```python
updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
```
변경 후:
```python
updated_at = Column(UtcDateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
```

**주의:** 위 라인 번호는 현재 HEAD 기준. 편집하면 뒤 라인이 조금씩 밀릴 수 있으니 컨텍스트 기반 Edit을 권장. 각 행을 컬럼 이름(`created_at`, `updated_at`, `timestamp`, `sent_at`)과 클래스 context로 구분.

- [ ] **Step 4: 교체 완료 검증**

```bash
grep -n "Column(DateTime" unibridge-service/app/models.py
```

Expected: 출력 없음 (모든 DateTime이 UtcDateTime으로 바뀜).

```bash
grep -n "Column(UtcDateTime" unibridge-service/app/models.py | wc -l
```

Expected: `13`.

```bash
grep -n "func.now" unibridge-service/app/models.py
```

Expected: 출력 없음 (`server_default`도 `onupdate`도 모두 Python default로 교체).

- [ ] **Step 5: 전체 테스트 재실행 (회귀)**

```bash
cd unibridge-service && .venv/bin/pytest -q
```

Expected: 모든 기존 테스트 통과. 만약 `server_default=func.now()`에 의존하던 테스트(예: DB에 직접 INSERT 후 응답 검증)가 실패하면, 해당 테스트를 UTC aware 비교로 수정. Python default는 객체 생성 시점에 값이 찍히므로 기존 `func.now()`와 의미가 동등.

만약 특정 테스트가 timezone 정보 비교 차이로 실패하면:
- 실패 케이스가 `assert dt == datetime(...)`처럼 naive 비교 중이면 `tzinfo=timezone.utc` 추가해 aware로 수정
- 실패가 지속되면 BLOCKED 상태로 보고하고 원인 분석

- [ ] **Step 6: 커밋**

```bash
git add unibridge-service/app/models.py
git commit -m "feat(models): migrate all DateTime columns to UtcDateTime"
```

---

## Task 3: 백엔드 통합 테스트 — 응답 JSON이 UTC aware ISO인지 확인

**Files:**
- Modify: `unibridge-service/tests/test_audit.py` (없으면 신규 생성)

- [ ] **Step 1: 기존 테스트 파일 존재 여부 확인**

```bash
ls unibridge-service/tests/test_audit* 2>/dev/null
```

파일이 있으면 해당 파일에 테스트 추가. 없으면 새로 만든다.

- [ ] **Step 2: 감사 로그 응답이 UTC offset을 포함하는지 검증하는 테스트 작성**

만약 파일이 없으면 `unibridge-service/tests/test_audit_timezone.py` 신규 생성:

```python
"""Integration test: audit log responses serialize timestamps as UTC-aware ISO."""

from __future__ import annotations

import re

import pytest

from tests.conftest import auth_header


UTC_ISO_SUFFIX = re.compile(r"(\+00:00|Z)$")


class TestAuditLogTimezone:
    async def test_audit_log_timestamp_is_utc_aware_iso(self, client, admin_token):
        # GET 감사 로그. 엔드포인트 경로는 프로젝트 convention을 따른다.
        # 실제 경로가 다르면 (예: /admin/audit-logs vs /api/audit/logs) 맞게 조정.
        resp = await client.get(
            "/admin/audit-logs",
            headers=auth_header(admin_token),
        )
        assert resp.status_code in (200, 204)
        data = resp.json()
        logs = data if isinstance(data, list) else data.get("items") or data.get("logs") or []
        if not logs:
            pytest.skip("no audit logs available to verify timestamp format")
        ts = logs[0].get("timestamp")
        assert ts is not None, "audit log entry is missing 'timestamp' field"
        assert UTC_ISO_SUFFIX.search(ts), (
            f"timestamp {ts!r} does not end with '+00:00' or 'Z' — "
            "Pydantic should serialize tz-aware datetime with UTC offset"
        )
```

**경로 확인 필요:** 감사 로그 GET 엔드포인트가 `/admin/audit-logs`인지 `/api/audit/logs`인지 프로젝트 코드에서 확인:
```bash
grep -rn "audit" unibridge-service/app/routers/ | grep -E "@router\.get|APIRouter" | head -10
```

발견한 경로로 위 테스트의 URL 수정.

- [ ] **Step 3: 테스트 실행**

```bash
cd unibridge-service && .venv/bin/pytest tests/test_audit_timezone.py -v
```

Expected: PASS. 기존 감사 로그 데이터가 없으면 skip. fixture에 감사 로그 생성이 있다면 skip 없이 PASS.

- [ ] **Step 4: 커밋**

```bash
git add unibridge-service/tests/test_audit_timezone.py
git commit -m "test(audit): verify timestamps serialize with UTC offset"
```

---

## Task 4: Docker — `unibridge-service`에 `TZ=UTC` 명시

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: `unibridge-service` 서비스의 environment 블록에 TZ=UTC 추가**

`docker-compose.yml`의 `unibridge-service.environment:` 리스트(현재 라인 112 근처 시작)에 `HOST_IP` 바로 아래 혹은 리스트 첫 줄에 추가:

변경 전:
```yaml
    environment:
      - HOST_IP=${HOST_IP:-localhost}
```

변경 후:
```yaml
    environment:
      - TZ=UTC
      - HOST_IP=${HOST_IP:-localhost}
```

- [ ] **Step 2: docker-compose 설정 문법 확인**

```bash
docker compose config --quiet 2>&1 | head -5
```

Expected: 출력 없음 (설정 OK). 또는 docker compose 없는 로컬 환경이라면:
```bash
python3 -c "import yaml; yaml.safe_load(open('docker-compose.yml'))"
```

Expected: 에러 없음.

- [ ] **Step 3: 커밋**

```bash
git add docker-compose.yml
git commit -m "chore(compose): pin unibridge-service TZ to UTC"
```

---

## Task 5: 프론트엔드 — `formatKST` 유틸 + 단위 테스트

**Files:**
- Create: `unibridge-ui/src/utils/time.ts`
- Create: `unibridge-ui/src/test/time.test.ts`

- [ ] **Step 1: 실패 테스트 작성**

`unibridge-ui/src/test/time.test.ts` 신규 생성:

```typescript
import { describe, it, expect } from 'vitest';
import { formatKST } from '../utils/time';

describe('formatKST', () => {
  it('converts UTC ISO to KST string', () => {
    const out = formatKST('2026-04-22T00:00:00+00:00');
    // 00:00 UTC → 09:00 KST
    expect(out).toMatch(/09:00:00/);
  });

  it('converts Z-suffixed UTC ISO to KST string', () => {
    const out = formatKST('2026-04-22T12:00:00Z');
    // 12:00 UTC → 21:00 KST
    expect(out).toMatch(/21:00:00/);
  });

  it('accepts Date instance', () => {
    const d = new Date('2026-04-22T00:00:00Z');
    const out = formatKST(d);
    expect(out).toMatch(/09:00:00/);
  });

  it('returns em-dash for null', () => {
    expect(formatKST(null)).toBe('—');
  });

  it('returns em-dash for undefined', () => {
    expect(formatKST(undefined)).toBe('—');
  });

  it('returns em-dash for empty string', () => {
    expect(formatKST('')).toBe('—');
  });

  it('falls back to original string for invalid input', () => {
    expect(formatKST('not-a-date')).toBe('not-a-date');
  });
});
```

- [ ] **Step 2: 테스트 실행해 실패 확인**

```bash
cd unibridge-ui && npx vitest run src/test/time.test.ts
```

Expected: `Cannot find module '../utils/time'` 또는 import 에러로 FAIL.

- [ ] **Step 3: `formatKST` 구현**

`unibridge-ui/src/utils/time.ts` 신규 생성:

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

/**
 * Format a UTC timestamp (ISO string or Date) as Korea Standard Time.
 * Returns '—' for null/undefined/empty input; falls back to the original
 * string when parsing fails.
 */
export function formatKST(value: string | Date | null | undefined): string {
  if (!value) return '—';
  const d = typeof value === 'string' ? new Date(value) : value;
  if (Number.isNaN(d.getTime())) {
    return typeof value === 'string' ? value : '';
  }
  return d.toLocaleString('ko-KR', KST_OPTS);
}
```

- [ ] **Step 4: 테스트 통과 확인**

```bash
cd unibridge-ui && npx vitest run src/test/time.test.ts
```

Expected: 7 tests PASS.

- [ ] **Step 5: 커밋**

```bash
git add unibridge-ui/src/utils/time.ts unibridge-ui/src/test/time.test.ts
git commit -m "feat(ui): add formatKST timestamp utility"
```

---

## Task 6: 프론트엔드 — 4개 페이지에서 공통 유틸 사용

**Files:**
- Modify: `unibridge-ui/src/pages/AuditLogs.tsx`
- Modify: `unibridge-ui/src/pages/AlertHistory.tsx`
- Modify: `unibridge-ui/src/pages/AlertStatus.tsx`
- Modify: `unibridge-ui/src/pages/S3Browser.tsx`

- [ ] **Step 1: `AuditLogs.tsx` 교체**

파일 상단 import 섹션에 추가:
```tsx
import { formatKST } from '../utils/time';
```

라인 71-77의 로컬 `formatTimestamp` 함수 전부 삭제:
```tsx
function formatTimestamp(ts: string) {
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}
```

라인 152의 호출부를 변경:
```tsx
<td className="cell-timestamp">{formatTimestamp(log.timestamp)}</td>
```
→
```tsx
<td className="cell-timestamp">{formatKST(log.timestamp)}</td>
```

- [ ] **Step 2: `AlertHistory.tsx` 교체**

import 추가:
```tsx
import { formatKST } from '../utils/time';
```

라인 47-52의 로컬 `formatTimestamp` 함수 삭제:
```tsx
function formatTimestamp(ts: string) {
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}
```

라인 110:
```tsx
<td className="cell-timestamp">{formatTimestamp(entry.sent_at)}</td>
```
→
```tsx
<td className="cell-timestamp">{formatKST(entry.sent_at)}</td>
```

- [ ] **Step 3: `AlertStatus.tsx` 교체**

import 추가:
```tsx
import { formatKST } from '../utils/time';
```

라인 18-24의 로컬 `formatTimestamp` 함수 삭제:
```tsx
function formatTimestamp(ts: string | null): string {
  if (!ts) return '—';
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}
```

라인 124:
```tsx
<td className="cell-timestamp">{formatTimestamp(e.since)}</td>
```
→
```tsx
<td className="cell-timestamp">{formatKST(e.since)}</td>
```

(`formatKST`의 null 처리가 기존 `formatTimestamp`와 동등 — null/undefined/빈문자 모두 `—` 반환.)

- [ ] **Step 4: `S3Browser.tsx` 교체**

import 추가:
```tsx
import { formatKST } from '../utils/time';
```

라인 28을 포함하는 주변 코드 확인:
```bash
sed -n '25,35p' unibridge-ui/src/pages/S3Browser.tsx
```

`new Date(iso).toLocaleString()`을 사용하는 함수 전체를 제거하거나 `formatKST(iso)`로 교체. 해당 위치가 로컬 함수 `function formatDate(iso: string)` 같은 형태면 그 함수를 삭제하고 호출부를 `formatKST`로 교체.

- [ ] **Step 5: 타입 체크**

```bash
cd unibridge-ui && npx tsc --noEmit
```

Expected: 에러 없음.

- [ ] **Step 6: 전체 프론트 테스트 실행**

```bash
cd unibridge-ui && npx vitest run
```

Expected: 모든 기존 테스트 + Task 5의 7개 `time.test.ts` 통과. 만약 `AuditLogs.test.tsx` 등이 기존 `toLocaleString()` 결과에 의존하는 assertion이 있으면 그 assertion을 KST 포맷(`/\d{4}\. \d{2}\. \d{2}\./` 같은 패턴)으로 수정.

- [ ] **Step 7: 기타 장소 감사 (누락 확인)**

```bash
grep -rn "new Date.*toLocaleString" unibridge-ui/src/pages/ unibridge-ui/src/components/
```

Expected: `AuditLogs.tsx`, `AlertHistory.tsx`, `AlertStatus.tsx`, `S3Browser.tsx`의 원래 라인이 모두 사라지고, 남은 grep 매치는 차트 x축 포맷(`GatewayMonitoring.tsx`, `LlmMonitoring.tsx` 안의 `new Date(ts * 1000)` 같은 숫자 timestamp 변환)뿐이어야 한다. 숫자 timestamp → 라벨 포맷은 본 스펙 범위 밖이므로 그대로 둔다.

만약 놓친 page가 있으면 같은 패턴으로 교체.

- [ ] **Step 8: 커밋**

```bash
git add unibridge-ui/src/pages/AuditLogs.tsx unibridge-ui/src/pages/AlertHistory.tsx unibridge-ui/src/pages/AlertStatus.tsx unibridge-ui/src/pages/S3Browser.tsx
git commit -m "refactor(ui): use formatKST across audit/alert/s3 timestamp cells"
```

---

## Task 7: 수동 검증 + 전체 회귀

**Files:** 없음 (검증만)

- [ ] **Step 1: 백엔드 전체 테스트**

```bash
cd unibridge-service && .venv/bin/pytest -q
```

Expected: 모두 PASS.

- [ ] **Step 2: 프론트엔드 전체 테스트**

```bash
cd unibridge-ui && npx vitest run
```

Expected: 모두 PASS.

- [ ] **Step 3: 타입 체크**

```bash
cd unibridge-ui && npx tsc --noEmit
```

Expected: 0 에러.

- [ ] **Step 4: 커밋 로그 확인**

```bash
git log --oneline c34536b..HEAD
```

Expected: Task 1~6의 커밋들이 순서대로 보여야 함.

- [ ] **Step 5: UI 수동 확인 (배포 후)**

배포 환경에서 `docker compose up --build unibridge-service`로 서비스 재시작 후 UI 접속:
- Audit Logs 페이지에서 새로 생성되는 감사 로그의 시각이 현재 한국 시간과 일치
- Alert History 페이지 동일
- Alert Status, S3 Browser 페이지 동일
- 기존 데이터도 KST로 정상 표시

---

## 구현 완료 체크리스트

- [ ] `UtcDateTime` TypeDecorator + 7개 단위 테스트 PASS
- [ ] `models.py` 13개 DateTime 컬럼 모두 교체, `func.now()` 잔여 없음
- [ ] 감사 로그 응답 JSON이 `+00:00` 또는 `Z` 포함 ISO 반환
- [ ] `docker-compose.yml` `unibridge-service.environment`에 `TZ=UTC` 명시
- [ ] `formatKST` 유틸 + 7개 단위 테스트 PASS
- [ ] 4개 페이지 로컬 `formatTimestamp` 제거 + `formatKST` 사용
- [ ] `tsc --noEmit` 0 에러
- [ ] 백엔드 pytest, 프론트엔드 vitest 전체 PASS
- [ ] 배포 후 UI에서 KST 표시 확인
