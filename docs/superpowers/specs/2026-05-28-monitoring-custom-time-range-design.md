# Monitoring: Custom Absolute Time Range

**Date:** 2026-05-28

## Overview

LLM 모니터링(`LlmMonitoring.tsx`)과 게이트웨이/라우트 모니터링(`GatewayMonitoring.tsx`) 페이지는
현재 7개 고정 프리셋(`15m, 1h, 6h, 24h, 7d, 30d, 60d`)으로만 조회할 수 있다. 모든 프리셋은
"현재 시각 기준 상대 lookback"이며 절대 구간 조회가 불가능하다.

이 기능은 프리셋은 그대로 두고, 그 옆에 **`커스텀 ▾` 버튼 + 팝오버**를 추가하여 사용자가
시작/종료 시각을 직접 지정한 **절대 기간**(`[start, end]`)으로 조회할 수 있게 한다.
두 모니터링 페이지에 동일하게 적용한다.

### 핵심 설계 결정 (브레인스토밍 합의)

1. **절대 기간** (시작~종료 datetime 직접 지정). 상대 자유 입력은 범위 밖.
2. UI는 **프리셋 토글 그룹 + `커스텀 ▾` 버튼 + 팝오버** 형태. 항상 노출되는 입력란 방식은 채택하지 않음.
3. 적용 후 커스텀 버튼은 선택 구간 칩(`05/20 09:00~05/22 18:00 ✕`)으로 표시.
4. **타임존: 서울 고정 (KST, +09:00, DST 없음).** 커스텀 입력 해석과 차트축 레이블 모두 `Asia/Seoul`.
   앱 나머지(`formatKST`, `2026-04-24-timezone-fix` 스펙)와 일관. 이 과정에서 현재 브라우저 로컬을
   쓰던 기존 모니터링 차트축도 KST 고정으로 정리한다(작은 범위의 기존 불일치 해소).

### PromQL 전략

| 쿼리 유형 | 기존(프리셋) | 커스텀 절대 기간 |
|---|---|---|
| Instant (`increase(m[range])`) | now 시점 평가 | `increase(m[<span>s])` 를 **`time=end`** 시점에 평가 → `[start, end]` 합 |
| Range (시계열) | `start = now - duration`, `end = now` | `start`/`end` 를 **직접 전달** |
| Instant rate snapshot (`rate(m[5m])`) | now 시점 평가 | **`time=end`** 시점 평가 |

`span = end - start`. instant 쿼리에서 range-vector 윈도우를 `<span>s` 로 두고 평가 시각을 `end` 로
지정하면 절대 구간의 누적합을 정확히 얻는다.

---

## 1. 백엔드

### 1.1 `app/services/prometheus_client.py`

하위호환을 유지하며 파라미터만 추가한다.

```python
async def instant_query(query: str, eval_time: float | None = None) -> list[dict]:
    # eval_time 이 주어지면 /api/v1/query 에 params["time"] = str(eval_time) 추가
    # 없으면 기존과 동일하게 now(서버) 기준 평가

async def range_query(
    query: str,
    duration: str = "1h",
    step: str = "60s",
    start: float | None = None,
    end: float | None = None,
) -> list[dict]:
    # start/end 가 둘 다 주어지면 그대로 사용
    # 아니면 기존 동작: end = now, start = end - _parse_duration(duration)
```

### 1.2 `app/routers/gateway.py` — 공용 시간창 리졸버

13개 메트릭 엔드포인트에 반복되던
`if time_range not in VALID_RANGES: time_range = "1h"` 가드를 단일 리졸버로 대체한다(코드 정리 겸).

```python
@dataclass
class TimeWindow:
    promql_window: str        # increase()용. 프리셋이면 "1h", 커스텀이면 "<span>s"
    step: str                 # range_query step
    volume_window: str        # requests-total 용 increase 윈도우
    eval_time: float | None   # 커스텀이면 end, 프리셋이면 None(=now)
    start: float | None       # range_query 직접 전달용 (커스텀이면 값, 프리셋이면 None)
    end: float | None
    is_custom: bool
```

FastAPI 의존성으로 구현:

```python
def resolve_time_window(
    time_range: str = Query("1h", alias="range"),
    start: int | None = Query(None, description="커스텀 시작 (epoch seconds)"),
    end: int | None = Query(None, description="커스텀 종료 (epoch seconds)"),
) -> TimeWindow:
    if start is not None and end is not None:
        # 커스텀 절대 기간
        _validate_custom_range(start, end)   # 검증 (1.4)
        span = end - start
        step, volume_window = _derive_step_window(span)   # (1.3)
        return TimeWindow(
            promql_window=f"{span}s",
            step=step,
            volume_window=volume_window,
            eval_time=float(end),
            start=float(start),
            end=float(end),
            is_custom=True,
        )
    # 프리셋 (기존 동작)
    if time_range not in VALID_RANGES:
        time_range = "1h"
    step = RANGE_STEPS[time_range]
    _, volume_window = RANGE_VOLUME[time_range]
    return TimeWindow(
        promql_window=time_range,
        step=step,
        volume_window=volume_window,
        eval_time=None,
        start=None,
        end=None,
        is_custom=False,
    )
```

각 엔드포인트는 `tw: TimeWindow = Depends(resolve_time_window)` 를 받고:
- instant 쿼리: `increase(metric[{tw.promql_window}])` + `instant_query(q, eval_time=tw.eval_time)`
- range 쿼리: `range_query(q, duration=tw.promql_window, step=tw.step, start=tw.start, end=tw.end)`
- volume: `RANGE_VOLUME` 대신 `tw.volume_window`, `tw.step` 사용

> `route` 같은 엔드포인트 고유 파라미터는 각자 유지하고, 시간 파라미터(`range`/`start`/`end`)만
> 리졸버가 흡수한다.

### 1.3 step / volume window 자동 산출

`span = end - start` 를 "그 값을 담을 수 있는 가장 작은 tier"에 매핑한다(기존 프리셋 튜닝값 재사용).

| span ≤ | step | volume window |
|---|---|---|
| 15m (900s) | 15s | 1m |
| 1h (3600s) | 60s | 5m |
| 6h (21600s) | 300s | 30m |
| 24h (86400s) | 600s | 1h |
| 7d (604800s) | 3600s | 1h |
| 30d (2592000s) | 21600s | 1d |
| 그 이상 | 43200s | 1d |

이 매핑으로 포인트 수가 항상 유한하게 유지되어 Prometheus의 11000 포인트 제한도 안전하다
(최대 step 43200s 기준 11000 포인트 ≈ 15000일).

### 1.4 검증

`_validate_custom_range(start, end)`:
- `start < end` 필수 — 위반 시 `400`.
- `end <= now + 60` (시계 오차 허용) — 미래 종료 거부, `400`.
- `end - start >= 60` (최소 span 60초) — 위반 시 `400`.
- Prometheus 보존 기간 밖이면 자연히 빈 데이터 반환 (별도 상한 캡 없음).
- `start`/`end` 중 하나만 오면 `400` (둘 다 필요).

기존 `route` 검증(`_validate_route`)은 그대로 유지.

### 1.5 영향받는 엔드포인트 (13개, prefix `/admin/gateway`)

라우트: `metrics/summary`, `metrics/requests`, `metrics/status-codes`, `metrics/latency`,
`metrics/top-routes`, `metrics/routes-comparison`, `metrics/requests-total`
LLM: `metrics/llm/summary`, `metrics/llm/tokens`, `metrics/llm/by-model`,
`metrics/llm/top-keys`, `metrics/llm/errors`, `metrics/llm/requests-total`

모두 `resolve_time_window` 의존성으로 전환. 권한(`gateway.monitoring.read`)은 변경 없음.

---

## 2. 프론트엔드

### 2.1 공용 컴포넌트 추출: `components/TimeRangeSelector.tsx`

현재 두 페이지가 인라인으로 갖고 있는 `.time-range-toggle` 토글 그룹을 공용 컴포넌트로 추출한다.

```ts
type TimeSelection =
  | { kind: 'preset'; value: string }
  | { kind: 'custom'; start: number; end: number };  // epoch seconds

interface TimeRangeSelectorProps {
  value: TimeSelection;
  onChange: (next: TimeSelection) => void;
}
```

구성:
- 프리셋 버튼 7개 (기존과 동일).
- `커스텀 ▾` 버튼: 클릭 시 팝오버 토글.
- 팝오버: `시작`/`종료` `<input type="datetime-local">`, `취소`/`적용` 버튼.
- 커스텀 적용 시: 버튼이 선택 구간 칩(`05/20 09:00~05/22 18:00 ✕`)으로 표시, `✕` 클릭 시 프리셋(`1h`)으로 복귀.
- 클라이언트 검증: 시작 < 종료, 종료 미래 불가 → 위반 시 `적용` 비활성 + 인라인 메시지.
- `datetime-local` 값을 **KST로 해석**(`kstLocalToEpoch`, 2.7)하여 epoch seconds로 변환 후 `onChange` 전달.
  초기값은 `epochToKstLocal`로 채움.

### 2.2 페이지 상태 모델

각 페이지의 `const [range, setRange] = useState('1h')` →
`const [selection, setSelection] = useState<TimeSelection>({ kind: 'preset', value: '1h' })`.

### 2.3 API 클라이언트 (`src/api/client.ts`)

`get*` 메트릭 함수들의 시그니처를 `TimeSelection` 기반으로 변경(또는 optional `start`/`end` 추가):
- `kind === 'preset'` → 쿼리 파라미터 `range=<value>` (기존과 동일).
- `kind === 'custom'` → `start=<epoch>&end=<epoch>`.

`route` 인자가 있는 함수는 그 인자를 유지하고 시간 파라미터만 위 규칙으로 직렬화.

### 2.4 React Query 연동

- `queryKey` 에 직렬화된 `selection` 포함 → 선택 변경 시 자동 refetch.
- `refetchInterval`: 프리셋은 기존 30s 유지. **커스텀(과거 고정 구간)은 비활성화**(`false`) — 데이터가 변하지 않음.

### 2.5 차트 레이블 포맷 (KST 고정)

현재 두 페이지의 `formatTime`/`formatTimestamp` 는 `new Date(ts*1000).getHours()` 등
**브라우저 로컬 타임존**을 쓴다. 이를 **`Asia/Seoul` 고정**으로 바꾸고 공용 유틸로 이동한다(2.7).
- `formatChartTime(epoch)` → `HH:mm` (KST).
- `formatChartTimestamp(epoch, spanSeconds)` → span 기반 granularity (KST):
  큰 span은 `M/D`, 중간은 `M/D HHh`, 작으면 `HH:MM`. (현 `7d`/`30d`/`60d` 분기 로직을 span 임계값으로 일반화)
- 프리셋도 동일 함수 사용 — 프리셋의 span은 해당 duration 초로 계산.

### 2.6 요약 카드 레이블

`totalRequests, { range }` 형태의 i18n 레이블은 커스텀일 때 칩 문자열(또는 `시작~종료`)을 표시.

### 2.7 타임존 헬퍼 (`src/utils/time.ts`에 추가)

KST는 고정 `+09:00`(DST 없음)이라 변환이 단순하다. 백엔드는 epoch seconds(절대 시각)만
주고받으므로 **타임존 책임은 전적으로 프론트**에 있다.

- `kstLocalToEpoch(local: string): number` — `datetime-local` 값(`"2026-05-20T09:00"`, tz 없음)을
  KST 벽시계로 해석 → epoch seconds. 구현: `Date.parse(`${local}:00+09:00`) / 1000`.
- `epochToKstLocal(epoch: number): string` — epoch → `"YYYY-MM-DDTHH:mm"` (Asia/Seoul). 피커 기본값
  채우기/표시용. `Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Seoul', ... })` 또는 `formatToParts` 사용.
- `formatChartTime` / `formatChartTimestamp` (2.5) — 차트축 KST 포맷. 기존 `formatKST`와 동일하게
  `timeZone: 'Asia/Seoul'`.

`TimeRangeSelector`(2.1)는 팝오버에서 `epochToKstLocal`로 input 초기값을 채우고, 적용 시
`kstLocalToEpoch`로 epoch 변환하여 `onChange`에 전달한다. 칩 텍스트도 KST 기준 포맷.

---

## 3. i18n

ko/en 번역 파일에 추가:
- `커스텀 / Custom`, `시작 / Start`, `종료 / End`, `적용 / Apply`, `취소 / Cancel`
- 검증 메시지: "시작은 종료보다 빨라야 합니다", "종료 시각은 미래일 수 없습니다" (및 영문)

---

## 4. 테스트

**백엔드**
- `resolve_time_window`: 프리셋 경로, 커스텀 경로, 잘못된 range fallback.
- `_validate_custom_range`: start≥end / 미래 end / span<60 / 한쪽만 전달 → 각각 400.
- `_derive_step_window`: 경계값(900, 3600, ..., 60d 초과) 매핑.
- `instant_query` 가 `eval_time` 전달 시 `time` 파라미터 포함.
- `range_query` 가 `start`/`end` 직접 전달 시 그대로 사용.

**프론트**
- `TimeRangeSelector`: 팝오버 열기, 검증(시작≥종료 시 적용 비활성), 적용 → 칩 표시, 칩 제거 → 프리셋 복귀.
- 커스텀 선택 시 client 함수가 `start`/`end` 쿼리 파라미터를 보내는지(프리셋은 `range`).
- 타임존 헬퍼: `kstLocalToEpoch("2026-05-20T09:00")` → 정확한 epoch(=UTC 00:00), `epochToKstLocal` 라운드트립,
  `formatChartTime`/`formatChartTimestamp` 가 KST로 포맷(브라우저 TZ와 무관하게 고정).

---

## 5. 범위 밖 (YAGNI)

- 커스텀 달력 위젯 직접 구현 (네이티브 `datetime-local` 사용).
- 저장된 커스텀 기간 프리셋 / 공유 가능한 URL 파라미터.
- 절대 기간의 step(해상도)을 사용자가 수동 조절.
- 사용자별 타임존 선택 UI (KST `Asia/Seoul` 고정).
- 상대 기간 자유 입력(예: "지난 45분").
