# API 메트릭 컨벤션 (UniBridge 통합 모니터링)

UniBridge 게이트웨이를 **지나지 않는** API 서비스도 UniBridge 모니터링에서
요청 수·오류율·응답 지연 통계가 함께 잡히게 하기 위한 최소 규약입니다.
트래픽 경로는 바꾸지 않습니다 — 각 서비스가 `/metrics` 주소만 열면,
UniBridge의 Prometheus가 주기적으로(15초) 읽어갑니다.

인증/API 키 통합이 필요한 서비스는 게이트웨이 온보딩(별도 절차)을 하면
키별 사용량 분석까지 추가로 제공됩니다. 이 문서는 **모니터링만** 원하는
서비스를 위한 것입니다.

```
[api-a] /metrics ─┐
[api-b] /metrics ─┤→ UniBridge Prometheus scrape ──→ UniBridge 모니터링 UI/알림
[api-c] /metrics ─┘   (등록제, file_sd)
```

## 필수 메트릭 (이 2개면 끝)

| 이름 | 타입 | 라벨 | 의미 |
|---|---|---|---|
| `http_requests_total` | Counter | `method`, `status`, `handler` | 처리한 HTTP 요청 수 |
| `http_request_duration_seconds` | Histogram | `method`, `handler` | 요청 처리 시간 (초) |

이 2개로 UniBridge가 계산하는 것: 요청 수/속도, 오류율(status 기준),
p50/p95/p99 지연, 상태 코드 분포. 서비스 생존 여부(`up`)는 수집 자체에서
자동으로 생기므로 따로 만들 필요 없습니다.

### 라벨 규칙

* `status` — 숫자 코드 문자열: `"200"`, `"404"`, `"500"`.
* `method` — HTTP 메서드: `"GET"`, `"POST"`, …
* `handler` — **라우트 패턴**이어야 합니다: `/users/{id}` (O), `/users/1234` (X).
  값의 종류가 유한해야 하며(수십 개 수준), 실제 URL·사용자 ID·토큰 등
  무한히 늘어나는 값을 라벨에 넣으면 안 됩니다. Prometheus 카디널리티가
  폭발하면 수집 대상에서 제외될 수 있습니다.
* `service` 라벨은 **달지 마세요** — UniBridge에 등록할 때 등록 정보에서
  자동으로 부여합니다(충돌 방지).
* 지연 단위는 **초(seconds)** 입니다. 밀리초 히스토그램을 쓰지 마세요.

## 프레임워크별 적용

### FastAPI / Starlette

```bash
pip install prometheus-fastapi-instrumentator
```

```python
from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI()
Instrumentator().instrument(app).expose(app)   # GET /metrics
```

기본 메트릭 이름이 이 컨벤션과 동일합니다. 끝.

### Spring Boot

`spring-boot-starter-actuator` + `micrometer-registry-prometheus` 의존성을
추가하고 `/actuator/prometheus`를 노출하세요:

```properties
management.endpoints.web.exposure.include=prometheus,health
```

Spring의 기본 이름(`http_server_requests_seconds{status,uri,...}`)은 UniBridge가
수집 시점에 표준 이름으로 정규화(relabel)하므로 **그대로 두면 됩니다**.
등록할 때 metrics path를 `/actuator/prometheus`로 지정하세요.

### Node.js (Express)

```bash
npm install express-prom-bundle
```

```js
const promBundle = require("express-prom-bundle");
app.use(promBundle({ includeMethod: true, includePath: true }));  // GET /metrics
```

`includePath`는 라우트 패턴 기준으로 정규화되지만, 커스텀 라우팅을 쓰면
`normalizePath`로 패턴화를 확인하세요 (handler 규칙 참고).

### 코드를 못 고치는 서비스

앞단 프록시(nginx/traefik) exporter 또는 호스트 에이전트(eBPF) 방식이
가능합니다 — 서비스 구조에 따라 방법이 다르니 UniBridge 관리자와 상의하세요.

## 배포 전 체크리스트

```bash
curl -s http://<host>:<port>/metrics | grep -m3 http_requests_total
curl -s http://<host>:<port>/metrics | grep -m1 http_request_duration_seconds_bucket
```

* [ ] 위 두 grep이 결과를 반환한다.
* [ ] `handler` 값이 라우트 패턴이고 종류가 유한하다 (raw URL 없음).
* [ ] UniBridge Prometheus 호스트에서 이 포트에 접근 가능하다
      (방화벽은 Prometheus IP만 열 것 — `/metrics`에는 인증이 없습니다).
* [ ] `/metrics`가 외부망에 노출되지 않는다.

## 등록

UniBridge UI → **Servers → External services**에서 `호스트:포트`(+ metrics
path)로 등록하면 30초 내에 수집이 시작됩니다. 등록/수집 파이프라인은 서버
모니터링과 같은 구조를 사용합니다 — [server-monitoring.md](server-monitoring.md) 참고.

## 이 컨벤션으로 안 되는 것

* **API 키별/사용자별 분석** — 인증을 UniBridge가 처리하지 않으므로 불가능
  합니다. 필요하면 게이트웨이 온보딩을 검토하세요.
* **요청 단위 로그/트레이싱** — 이 규약은 집계 통계(메트릭)만 다룹니다.
