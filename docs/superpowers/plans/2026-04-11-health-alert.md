# Health Check Alert System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** DB 연결 실패, 업스트림 다운, 5xx 에러율 초과 시 Webhook 알림을 발송하고, 복구 시 복구 알림을 발송하는 헬스 체크 알림 시스템 구현

**Architecture:** FastAPI 백그라운드 태스크(asyncio)로 60초 주기 헬스 체크 루프 실행. 알림 규칙/채널/이력을 기존 SQLite DB에 저장. 인메모리 상태 추적으로 중복 방지 (최초 1회 + 복구 알림). Webhook POST로 알림 발송하며 채널별 payload 템플릿 지원.

**Tech Stack:** FastAPI, SQLAlchemy async, httpx, pytest, React + TanStack Query + react-i18next

**Spec:** `docs/superpowers/specs/2026-04-11-health-alert-design.md`

---

## File Structure

### Backend (New)

| File | Responsibility |
|------|---------------|
| `unibridge-service/app/models.py` | AlertChannel, AlertRule, AlertRuleChannel, AlertHistory 모델 추가 |
| `unibridge-service/app/schemas.py` | Alert 관련 Pydantic 스키마 추가 |
| `unibridge-service/app/services/alert_state.py` | 인메모리 상태 추적 (상태 전이 감지) |
| `unibridge-service/app/services/alert_sender.py` | Webhook 발송 + 템플릿 렌더링 |
| `unibridge-service/app/services/alert_checker.py` | 60초 주기 헬스 체크 루프 |
| `unibridge-service/app/routers/alerts.py` | 알림 채널/규칙/이력 CRUD API |
| `unibridge-service/app/main.py` | 알림 라우터 등록 + lifespan에 체커 시작/종료 |
| `unibridge-service/app/auth.py` | `alerts.read`, `alerts.write` 권한 추가 |
| `unibridge-service/app/database.py` | seed roles에 alerts 권한 추가 |
| `unibridge-service/tests/test_alerts.py` | 알림 시스템 전체 테스트 |

### Frontend (New)

| File | Responsibility |
|------|---------------|
| `unibridge-ui/src/pages/AlertSettings.tsx` | 채널 + 규칙 관리 페이지 |
| `unibridge-ui/src/pages/AlertSettings.css` | 스타일 |
| `unibridge-ui/src/pages/AlertHistory.tsx` | 알림 발송 이력 페이지 |
| `unibridge-ui/src/pages/AlertHistory.css` | 스타일 |
| `unibridge-ui/src/api/client.ts` | Alert API 함수 추가 |
| `unibridge-ui/src/App.tsx` | 라우트 추가 |
| `unibridge-ui/src/components/Layout.tsx` | 사이드바 nav 항목 추가 |
| `unibridge-ui/src/locales/ko.json` | 한국어 번역 키 추가 |
| `unibridge-ui/src/locales/en.json` | 영어 번역 키 추가 |

---

## Task 1: 데이터 모델 추가

**Files:**
- Modify: `unibridge-service/app/models.py`
- Test: `unibridge-service/tests/test_alerts.py`

- [ ] **Step 1: Write failing test — 모델 import 확인**

```python
# unibridge-service/tests/test_alerts.py
"""Tests for the health-check alert system."""
from __future__ import annotations

import pytest

from app.models import AlertChannel, AlertRule, AlertRuleChannel, AlertHistory


class TestAlertModels:
    def test_alert_channel_columns(self):
        ch = AlertChannel(name="test", webhook_url="http://example.com/hook", payload_template='{}')
        assert ch.name == "test"
        assert ch.webhook_url == "http://example.com/hook"
        assert ch.enabled is True

    def test_alert_rule_columns(self):
        rule = AlertRule(name="db-check", type="db_health", target="mydb")
        assert rule.type == "db_health"
        assert rule.enabled is True

    def test_alert_rule_channel_columns(self):
        arc = AlertRuleChannel(rule_id=1, channel_id=1, recipients='["a@b.com"]')
        assert arc.recipients == '["a@b.com"]'

    def test_alert_history_columns(self):
        h = AlertHistory(rule_id=1, channel_id=1, alert_type="triggered", target="mydb", message="down")
        assert h.alert_type == "triggered"
        assert h.success is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jinyoung/UniBridge/unibridge-service && python -m pytest tests/test_alerts.py::TestAlertModels -v`
Expected: FAIL with `ImportError: cannot import name 'AlertChannel'`

- [ ] **Step 3: Implement models**

Add to `unibridge-service/app/models.py` after the `SystemConfig` class:

```python
class AlertChannel(Base):
    __tablename__ = "alert_channels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    webhook_url = Column(String, nullable=False)
    payload_template = Column(Text, nullable=False)
    headers = Column(Text, nullable=True)  # JSON object
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    type = Column(String(30), nullable=False)  # "db_health", "upstream_health", "error_rate"
    target = Column(String(100), nullable=False)  # DB alias, upstream ID, or "*"
    threshold = Column(Integer, nullable=True)  # error rate % (error_rate type only)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class AlertRuleChannel(Base):
    __tablename__ = "alert_rule_channels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rule_id = Column(Integer, ForeignKey("alert_rules.id", ondelete="CASCADE"), nullable=False)
    channel_id = Column(Integer, ForeignKey("alert_channels.id", ondelete="CASCADE"), nullable=False)
    recipients = Column(Text, nullable=False)  # JSON array: ["user@example.com"]

    __table_args__ = (UniqueConstraint("rule_id", "channel_id", name="uq_rule_channel"),)


class AlertHistory(Base):
    __tablename__ = "alert_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rule_id = Column(Integer, ForeignKey("alert_rules.id", ondelete="SET NULL"), nullable=True)
    channel_id = Column(Integer, ForeignKey("alert_channels.id", ondelete="SET NULL"), nullable=True)
    alert_type = Column(String(20), nullable=False)  # "triggered" / "resolved"
    target = Column(String(100), nullable=False)
    message = Column(Text, nullable=False)
    recipients = Column(Text, nullable=True)  # JSON array
    sent_at = Column(DateTime, server_default=func.now())
    success = Column(Boolean, nullable=True)
    error_detail = Column(Text, nullable=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jinyoung/UniBridge/unibridge-service && python -m pytest tests/test_alerts.py::TestAlertModels -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add unibridge-service/app/models.py unibridge-service/tests/test_alerts.py
git commit -m "feat: add alert system data models (AlertChannel, AlertRule, AlertRuleChannel, AlertHistory)"
```

---

## Task 2: Pydantic 스키마 추가

**Files:**
- Modify: `unibridge-service/app/schemas.py`
- Test: `unibridge-service/tests/test_alerts.py`

- [ ] **Step 1: Write failing test — 스키마 import 및 검증**

Append to `unibridge-service/tests/test_alerts.py`:

```python
from app.schemas import (
    AlertChannelCreate, AlertChannelUpdate, AlertChannelResponse,
    AlertRuleCreate, AlertRuleUpdate, AlertRuleResponse,
    AlertHistoryResponse, AlertStatusResponse,
)


class TestAlertSchemas:
    def test_channel_create_valid(self):
        ch = AlertChannelCreate(
            name="email",
            webhook_url="http://mail.internal/api/send",
            payload_template='{"to":"{{recipients}}","subject":"{{alert_type}}"}',
        )
        assert ch.name == "email"
        assert ch.headers is None
        assert ch.enabled is True

    def test_rule_create_db_health(self):
        rule = AlertRuleCreate(
            name="order-db-check",
            type="db_health",
            target="order-db",
            channels=[{"channel_id": 1, "recipients": ["team@co.com"]}],
        )
        assert rule.threshold is None
        assert len(rule.channels) == 1

    def test_rule_create_error_rate_requires_threshold(self):
        rule = AlertRuleCreate(
            name="error-check",
            type="error_rate",
            target="*",
            threshold=10.0,
            channels=[{"channel_id": 1, "recipients": ["ops@co.com"]}],
        )
        assert rule.threshold == 10.0

    def test_alert_status_response(self):
        s = AlertStatusResponse(target="mydb", type="db_health", status="alert", since="2026-04-11T12:00:00")
        assert s.status == "alert"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jinyoung/UniBridge/unibridge-service && python -m pytest tests/test_alerts.py::TestAlertSchemas -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement schemas**

Add to `unibridge-service/app/schemas.py` at the end:

```python
# ── Alerts ──────────────────────────────────────────────────────────────────

class AlertChannelCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    webhook_url: str = Field(..., min_length=1)
    payload_template: str = Field(..., min_length=1)
    headers: dict[str, str] | None = None
    enabled: bool = True


class AlertChannelUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    webhook_url: str | None = Field(None, min_length=1)
    payload_template: str | None = None
    headers: dict[str, str] | None = None
    enabled: bool | None = None


class AlertChannelResponse(BaseModel):
    id: int
    name: str
    webhook_url: str
    payload_template: str
    headers: dict[str, str] | None = None
    enabled: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class RuleChannelMapping(BaseModel):
    channel_id: int
    recipients: list[str]


class AlertRuleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    type: str = Field(..., pattern=r"^(db_health|upstream_health|error_rate)$")
    target: str = Field(..., min_length=1, max_length=100)
    threshold: float | None = Field(None, ge=0, le=100)
    enabled: bool = True
    channels: list[RuleChannelMapping] = Field(default_factory=list)


class AlertRuleUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    type: str | None = Field(None, pattern=r"^(db_health|upstream_health|error_rate)$")
    target: str | None = Field(None, min_length=1, max_length=100)
    threshold: float | None = None
    enabled: bool | None = None
    channels: list[RuleChannelMapping] | None = None


class RuleChannelDetail(BaseModel):
    channel_id: int
    channel_name: str
    recipients: list[str]


class AlertRuleResponse(BaseModel):
    id: int
    name: str
    type: str
    target: str
    threshold: float | None = None
    enabled: bool
    channels: list[RuleChannelDetail] = []
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class AlertHistoryResponse(BaseModel):
    id: int
    rule_id: int | None = None
    channel_id: int | None = None
    alert_type: str
    target: str
    message: str
    recipients: list[str] | None = None
    sent_at: datetime | None = None
    success: bool | None = None
    error_detail: str | None = None

    model_config = {"from_attributes": True}


class AlertStatusResponse(BaseModel):
    target: str
    type: str
    status: str  # "ok" | "alert"
    since: str | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jinyoung/UniBridge/unibridge-service && python -m pytest tests/test_alerts.py::TestAlertSchemas -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add unibridge-service/app/schemas.py unibridge-service/tests/test_alerts.py
git commit -m "feat: add alert Pydantic schemas (channel, rule, history, status)"
```

---

## Task 3: 권한 추가

**Files:**
- Modify: `unibridge-service/app/auth.py` (line 26-44: ALL_PERMISSIONS)
- Modify: `unibridge-service/app/database.py` (line 37-58: SEED_ROLES)
- Test: `unibridge-service/tests/test_alerts.py`

- [ ] **Step 1: Write failing test**

Append to `unibridge-service/tests/test_alerts.py`:

```python
from app.auth import ALL_PERMISSIONS


class TestAlertPermissions:
    def test_alerts_read_in_all_permissions(self):
        assert "alerts.read" in ALL_PERMISSIONS

    def test_alerts_write_in_all_permissions(self):
        assert "alerts.write" in ALL_PERMISSIONS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jinyoung/UniBridge/unibridge-service && python -m pytest tests/test_alerts.py::TestAlertPermissions -v`
Expected: FAIL with `AssertionError`

- [ ] **Step 3: Add permissions to auth.py**

In `unibridge-service/app/auth.py`, add to `ALL_PERMISSIONS` list (after `"admin.roles.write"`):

```python
    "alerts.read",
    "alerts.write",
```

- [ ] **Step 4: Add permissions to seed roles in database.py**

In `unibridge-service/app/database.py`, update `SEED_ROLES`:

- `"admin"` already gets `ALL_PERMISSIONS` (includes alerts.read, alerts.write automatically)
- Add `"alerts.read"` to `"developer"` permissions list
- Add `"alerts.read"` to `"viewer"` permissions list

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /home/jinyoung/UniBridge/unibridge-service && python -m pytest tests/test_alerts.py::TestAlertPermissions -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add unibridge-service/app/auth.py unibridge-service/app/database.py unibridge-service/tests/test_alerts.py
git commit -m "feat: add alerts.read and alerts.write permissions"
```

---

## Task 4: alert_state — 인메모리 상태 추적

**Files:**
- Create: `unibridge-service/app/services/alert_state.py`
- Test: `unibridge-service/tests/test_alerts.py`

- [ ] **Step 1: Write failing test**

Append to `unibridge-service/tests/test_alerts.py`:

```python
from app.services.alert_state import AlertStateManager


class TestAlertState:
    def test_initial_state_is_ok(self):
        mgr = AlertStateManager()
        assert mgr.get_status("db_health", "mydb") == "ok"

    def test_transition_ok_to_alert(self):
        mgr = AlertStateManager()
        transition = mgr.update("db_health", "mydb", is_healthy=False)
        assert transition == "triggered"
        assert mgr.get_status("db_health", "mydb") == "alert"

    def test_no_transition_when_still_alert(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "mydb", is_healthy=False)
        transition = mgr.update("db_health", "mydb", is_healthy=False)
        assert transition is None

    def test_transition_alert_to_ok(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "mydb", is_healthy=False)
        transition = mgr.update("db_health", "mydb", is_healthy=True)
        assert transition == "resolved"
        assert mgr.get_status("db_health", "mydb") == "ok"

    def test_no_transition_when_still_ok(self):
        mgr = AlertStateManager()
        transition = mgr.update("db_health", "mydb", is_healthy=True)
        assert transition is None

    def test_get_all_alerts(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "db1", is_healthy=False)
        mgr.update("upstream_health", "svc1", is_healthy=False)
        mgr.update("db_health", "db2", is_healthy=True)
        alerts = mgr.get_all_alerts()
        assert len(alerts) == 2
        targets = {a["target"] for a in alerts}
        assert targets == {"db1", "svc1"}

    def test_reset_clears_all(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "mydb", is_healthy=False)
        mgr.reset()
        assert mgr.get_status("db_health", "mydb") == "ok"
        assert mgr.get_all_alerts() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jinyoung/UniBridge/unibridge-service && python -m pytest tests/test_alerts.py::TestAlertState -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement alert_state.py**

Create `unibridge-service/app/services/alert_state.py`:

```python
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class AlertStateManager:
    """In-memory alert state tracker.

    Tracks (type, target) → status transitions.
    Returns transition type on state change, None if no change.
    """

    def __init__(self) -> None:
        # key: (type, target) → {"status": "ok"|"alert", "since": datetime}
        self._states: dict[tuple[str, str], dict] = {}

    def get_status(self, alert_type: str, target: str) -> str:
        entry = self._states.get((alert_type, target))
        return entry["status"] if entry else "ok"

    def update(self, alert_type: str, target: str, *, is_healthy: bool) -> str | None:
        """Update state and return transition type if changed.

        Returns:
            "triggered" — transitioned ok → alert
            "resolved"  — transitioned alert → ok
            None        — no change
        """
        key = (alert_type, target)
        current = self._states.get(key)
        current_status = current["status"] if current else "ok"
        new_status = "ok" if is_healthy else "alert"

        if current_status == new_status:
            return None

        now = datetime.now(timezone.utc).isoformat()
        self._states[key] = {"status": new_status, "since": now}
        transition = "resolved" if is_healthy else "triggered"
        logger.info("Alert state %s/%s: %s → %s", alert_type, target, current_status, new_status)
        return transition

    def get_all_alerts(self) -> list[dict]:
        """Return all entries currently in 'alert' status."""
        return [
            {"type": k[0], "target": k[1], "status": "alert", "since": v["since"]}
            for k, v in self._states.items()
            if v["status"] == "alert"
        ]

    def reset(self) -> None:
        self._states.clear()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jinyoung/UniBridge/unibridge-service && python -m pytest tests/test_alerts.py::TestAlertState -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add unibridge-service/app/services/alert_state.py unibridge-service/tests/test_alerts.py
git commit -m "feat: add AlertStateManager for in-memory state transition tracking"
```

---

## Task 5: alert_sender — Webhook 발송 + 템플릿 렌더링

**Files:**
- Create: `unibridge-service/app/services/alert_sender.py`
- Test: `unibridge-service/tests/test_alerts.py`

- [ ] **Step 1: Write failing test**

Append to `unibridge-service/tests/test_alerts.py`:

```python
from app.services.alert_sender import render_template, send_webhook


class TestRenderTemplate:
    def test_renders_all_placeholders(self):
        template = '{"to":"{{recipients}}","subject":"[UniBridge] {{alert_type}}: {{target_name}}","body":"{{message}} at {{timestamp}}"}'
        result = render_template(
            template,
            alert_type="triggered",
            target_name="order-db",
            status="error",
            message="Connection failed",
            timestamp="2026-04-11T14:30:00",
            recipients="team@co.com",
        )
        assert '"to":"team@co.com"' in result
        assert "order-db" in result
        assert "Connection failed" in result

    def test_unknown_placeholder_left_as_is(self):
        template = '{"note":"{{unknown_var}}"}'
        result = render_template(template, alert_type="triggered", target_name="x",
                                 status="ok", message="m", timestamp="t", recipients="r")
        assert "{{unknown_var}}" in result


class TestSendWebhook:
    @pytest.mark.asyncio
    async def test_send_webhook_success(self, httpx_mock):
        httpx_mock.add_response(url="http://example.com/hook", status_code=200)
        ok, err = await send_webhook(
            url="http://example.com/hook",
            payload='{"msg":"test"}',
            headers=None,
        )
        assert ok is True
        assert err is None

    @pytest.mark.asyncio
    async def test_send_webhook_failure(self, httpx_mock):
        httpx_mock.add_response(url="http://example.com/hook", status_code=500)
        ok, err = await send_webhook(
            url="http://example.com/hook",
            payload='{"msg":"test"}',
            headers=None,
        )
        assert ok is False
        assert err is not None

    @pytest.mark.asyncio
    async def test_send_webhook_with_custom_headers(self, httpx_mock):
        httpx_mock.add_response(url="http://example.com/hook", status_code=200)
        ok, _ = await send_webhook(
            url="http://example.com/hook",
            payload='{"msg":"test"}',
            headers={"X-Token": "secret"},
        )
        assert ok is True
        req = httpx_mock.get_request()
        assert req.headers["X-Token"] == "secret"
```

Note: `httpx_mock` requires `pytest-httpx`. Add it to test dependencies if not present.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jinyoung/UniBridge/unibridge-service && pip install pytest-httpx 2>/dev/null; python -m pytest tests/test_alerts.py::TestRenderTemplate tests/test_alerts.py::TestSendWebhook -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement alert_sender.py**

Create `unibridge-service/app/services/alert_sender.py`:

```python
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SEND_TIMEOUT = 10.0


def render_template(
    template: str,
    *,
    alert_type: str,
    target_name: str,
    status: str,
    message: str,
    timestamp: str,
    recipients: str,
) -> str:
    """Replace {{placeholders}} in the template string."""
    replacements = {
        "{{alert_type}}": alert_type,
        "{{target_name}}": target_name,
        "{{status}}": status,
        "{{message}}": message,
        "{{timestamp}}": timestamp,
        "{{recipients}}": recipients,
    }
    result = template
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, value)
    return result


async def send_webhook(
    *,
    url: str,
    payload: str,
    headers: dict[str, str] | None,
) -> tuple[bool, str | None]:
    """POST payload to webhook URL. Returns (success, error_detail)."""
    send_headers = {"Content-Type": "application/json"}
    if headers:
        send_headers.update(headers)
    try:
        async with httpx.AsyncClient(timeout=SEND_TIMEOUT) as client:
            resp = await client.post(url, content=payload, headers=send_headers)
            resp.raise_for_status()
        return True, None
    except Exception as exc:
        logger.warning("Webhook send failed to %s: %s", url, exc)
        return False, str(exc)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jinyoung/UniBridge/unibridge-service && python -m pytest tests/test_alerts.py::TestRenderTemplate tests/test_alerts.py::TestSendWebhook -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add unibridge-service/app/services/alert_sender.py unibridge-service/tests/test_alerts.py
git commit -m "feat: add alert_sender with template rendering and webhook dispatch"
```

---

## Task 6: alerts 라우터 — 채널 CRUD API

**Files:**
- Create: `unibridge-service/app/routers/alerts.py`
- Test: `unibridge-service/tests/test_alerts.py`

- [ ] **Step 1: Write failing test**

Append to `unibridge-service/tests/test_alerts.py`:

```python
from tests.conftest import auth_header


class TestAlertChannelsAPI:
    """Integration tests for /admin/alerts/channels endpoints."""

    @pytest.mark.asyncio
    async def test_create_channel(self, client, admin_token):
        resp = await client.post("/admin/alerts/channels", json={
            "name": "email-api",
            "webhook_url": "http://mail.internal/api/send",
            "payload_template": '{"to":"{{recipients}}","subject":"{{alert_type}}: {{target_name}}"}',
        }, headers=auth_header(admin_token))
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "email-api"
        assert data["enabled"] is True

    @pytest.mark.asyncio
    async def test_list_channels(self, client, admin_token):
        # Create one first
        await client.post("/admin/alerts/channels", json={
            "name": "ch1",
            "webhook_url": "http://example.com/hook",
            "payload_template": "{}",
        }, headers=auth_header(admin_token))
        resp = await client.get("/admin/alerts/channels", headers=auth_header(admin_token))
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    @pytest.mark.asyncio
    async def test_update_channel(self, client, admin_token):
        create = await client.post("/admin/alerts/channels", json={
            "name": "ch-update",
            "webhook_url": "http://example.com/hook",
            "payload_template": "{}",
        }, headers=auth_header(admin_token))
        ch_id = create.json()["id"]
        resp = await client.put(f"/admin/alerts/channels/{ch_id}", json={
            "name": "ch-updated",
            "enabled": False,
        }, headers=auth_header(admin_token))
        assert resp.status_code == 200
        assert resp.json()["name"] == "ch-updated"
        assert resp.json()["enabled"] is False

    @pytest.mark.asyncio
    async def test_delete_channel(self, client, admin_token):
        create = await client.post("/admin/alerts/channels", json={
            "name": "ch-delete",
            "webhook_url": "http://example.com/hook",
            "payload_template": "{}",
        }, headers=auth_header(admin_token))
        ch_id = create.json()["id"]
        resp = await client.delete(f"/admin/alerts/channels/{ch_id}", headers=auth_header(admin_token))
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_test_channel(self, client, admin_token, httpx_mock):
        httpx_mock.add_response(url="http://example.com/hook", status_code=200)
        create = await client.post("/admin/alerts/channels", json={
            "name": "ch-test",
            "webhook_url": "http://example.com/hook",
            "payload_template": '{"msg":"{{message}}"}',
        }, headers=auth_header(admin_token))
        ch_id = create.json()["id"]
        resp = await client.post(f"/admin/alerts/channels/{ch_id}/test", headers=auth_header(admin_token))
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    @pytest.mark.asyncio
    async def test_viewer_cannot_create_channel(self, client, viewer_token):
        resp = await client.post("/admin/alerts/channels", json={
            "name": "nope",
            "webhook_url": "http://example.com",
            "payload_template": "{}",
        }, headers=auth_header(viewer_token))
        assert resp.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jinyoung/UniBridge/unibridge-service && python -m pytest tests/test_alerts.py::TestAlertChannelsAPI -v`
Expected: FAIL

- [ ] **Step 3: Implement alerts router — channels portion**

Create `unibridge-service/app/routers/alerts.py`:

```python
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete as sa_delete, select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, require_permission
from app.database import get_db
from app.models import AlertChannel, AlertHistory, AlertRule, AlertRuleChannel
from app.schemas import (
    AlertChannelCreate, AlertChannelResponse, AlertChannelUpdate,
    AlertHistoryResponse,
    AlertRuleCreate, AlertRuleResponse, AlertRuleUpdate, AlertStatusResponse,
    RuleChannelDetail,
)
from app.services.alert_sender import render_template, send_webhook

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/alerts", tags=["Alerts"])


# ── Channels ────────────────────────────────────────────────────────────────

@router.get("/channels", response_model=list[AlertChannelResponse])
async def list_channels(
    _user: CurrentUser = Depends(require_permission("alerts.read")),
    db: AsyncSession = Depends(get_db),
) -> list[AlertChannelResponse]:
    result = await db.execute(select(AlertChannel).order_by(AlertChannel.id))
    channels = result.scalars().all()
    rows = []
    for ch in channels:
        rows.append(AlertChannelResponse(
            id=ch.id, name=ch.name, webhook_url=ch.webhook_url,
            payload_template=ch.payload_template,
            headers=json.loads(ch.headers) if ch.headers else None,
            enabled=ch.enabled, created_at=ch.created_at, updated_at=ch.updated_at,
        ))
    return rows


@router.post("/channels", response_model=AlertChannelResponse, status_code=201)
async def create_channel(
    body: AlertChannelCreate,
    _user: CurrentUser = Depends(require_permission("alerts.write")),
    db: AsyncSession = Depends(get_db),
) -> AlertChannelResponse:
    ch = AlertChannel(
        name=body.name,
        webhook_url=body.webhook_url,
        payload_template=body.payload_template,
        headers=json.dumps(body.headers) if body.headers else None,
        enabled=body.enabled,
    )
    db.add(ch)
    await db.commit()
    await db.refresh(ch)
    return AlertChannelResponse(
        id=ch.id, name=ch.name, webhook_url=ch.webhook_url,
        payload_template=ch.payload_template,
        headers=body.headers, enabled=ch.enabled,
        created_at=ch.created_at, updated_at=ch.updated_at,
    )


@router.put("/channels/{channel_id}", response_model=AlertChannelResponse)
async def update_channel(
    channel_id: int,
    body: AlertChannelUpdate,
    _user: CurrentUser = Depends(require_permission("alerts.write")),
    db: AsyncSession = Depends(get_db),
) -> AlertChannelResponse:
    result = await db.execute(select(AlertChannel).where(AlertChannel.id == channel_id))
    ch = result.scalar_one_or_none()
    if ch is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    if body.name is not None:
        ch.name = body.name
    if body.webhook_url is not None:
        ch.webhook_url = body.webhook_url
    if body.payload_template is not None:
        ch.payload_template = body.payload_template
    if body.headers is not None:
        ch.headers = json.dumps(body.headers)
    if body.enabled is not None:
        ch.enabled = body.enabled
    await db.commit()
    await db.refresh(ch)
    return AlertChannelResponse(
        id=ch.id, name=ch.name, webhook_url=ch.webhook_url,
        payload_template=ch.payload_template,
        headers=json.loads(ch.headers) if ch.headers else None,
        enabled=ch.enabled, created_at=ch.created_at, updated_at=ch.updated_at,
    )


@router.delete("/channels/{channel_id}", status_code=204)
async def delete_channel(
    channel_id: int,
    _user: CurrentUser = Depends(require_permission("alerts.write")),
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(select(AlertChannel).where(AlertChannel.id == channel_id))
    ch = result.scalar_one_or_none()
    if ch is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    await db.delete(ch)
    await db.commit()


@router.post("/channels/{channel_id}/test")
async def test_channel(
    channel_id: int,
    _user: CurrentUser = Depends(require_permission("alerts.write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(select(AlertChannel).where(AlertChannel.id == channel_id))
    ch = result.scalar_one_or_none()
    if ch is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    now = datetime.now(timezone.utc).isoformat()
    payload = render_template(
        ch.payload_template,
        alert_type="test",
        target_name="test-target",
        status="ok",
        message="This is a test alert from UniBridge.",
        timestamp=now,
        recipients="test@example.com",
    )
    headers = json.loads(ch.headers) if ch.headers else None
    ok, err = await send_webhook(url=ch.webhook_url, payload=payload, headers=headers)
    return {"success": ok, "error": err}
```

- [ ] **Step 4: Register router in main.py**

In `unibridge-service/app/main.py`:

Add import: `from app.routers import admin, alerts, api_keys, gateway, query, roles, users`

Add router: `app.include_router(alerts.router)` after the existing `include_router` calls.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /home/jinyoung/UniBridge/unibridge-service && python -m pytest tests/test_alerts.py::TestAlertChannelsAPI -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add unibridge-service/app/routers/alerts.py unibridge-service/app/main.py unibridge-service/tests/test_alerts.py
git commit -m "feat: add alert channels CRUD API with test endpoint"
```

---

## Task 7: alerts 라우터 — 규칙 CRUD + 이력 + 상태 API

**Files:**
- Modify: `unibridge-service/app/routers/alerts.py`
- Test: `unibridge-service/tests/test_alerts.py`

- [ ] **Step 1: Write failing test**

Append to `unibridge-service/tests/test_alerts.py`:

```python
class TestAlertRulesAPI:
    @pytest.mark.asyncio
    async def test_create_rule_with_channel(self, client, admin_token):
        # Create a channel first
        ch = await client.post("/admin/alerts/channels", json={
            "name": "rule-test-ch",
            "webhook_url": "http://example.com/hook",
            "payload_template": "{}",
        }, headers=auth_header(admin_token))
        ch_id = ch.json()["id"]

        resp = await client.post("/admin/alerts/rules", json={
            "name": "order-db-check",
            "type": "db_health",
            "target": "order-db",
            "channels": [{"channel_id": ch_id, "recipients": ["team@co.com"]}],
        }, headers=auth_header(admin_token))
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "order-db-check"
        assert len(data["channels"]) == 1
        assert data["channels"][0]["recipients"] == ["team@co.com"]

    @pytest.mark.asyncio
    async def test_list_rules(self, client, admin_token):
        resp = await client.get("/admin/alerts/rules", headers=auth_header(admin_token))
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_update_rule(self, client, admin_token):
        ch = await client.post("/admin/alerts/channels", json={
            "name": "rule-upd-ch",
            "webhook_url": "http://example.com/hook",
            "payload_template": "{}",
        }, headers=auth_header(admin_token))
        ch_id = ch.json()["id"]

        create = await client.post("/admin/alerts/rules", json={
            "name": "upd-rule",
            "type": "db_health",
            "target": "db1",
            "channels": [{"channel_id": ch_id, "recipients": ["a@b.com"]}],
        }, headers=auth_header(admin_token))
        rule_id = create.json()["id"]

        resp = await client.put(f"/admin/alerts/rules/{rule_id}", json={
            "name": "upd-rule-v2",
            "enabled": False,
        }, headers=auth_header(admin_token))
        assert resp.status_code == 200
        assert resp.json()["name"] == "upd-rule-v2"
        assert resp.json()["enabled"] is False

    @pytest.mark.asyncio
    async def test_delete_rule(self, client, admin_token):
        create = await client.post("/admin/alerts/rules", json={
            "name": "del-rule",
            "type": "upstream_health",
            "target": "*",
            "channels": [],
        }, headers=auth_header(admin_token))
        rule_id = create.json()["id"]
        resp = await client.delete(f"/admin/alerts/rules/{rule_id}", headers=auth_header(admin_token))
        assert resp.status_code == 204


class TestAlertHistoryAPI:
    @pytest.mark.asyncio
    async def test_list_history_empty(self, client, admin_token):
        resp = await client.get("/admin/alerts/history", headers=auth_header(admin_token))
        assert resp.status_code == 200
        assert resp.json() == []


class TestAlertStatusAPI:
    @pytest.mark.asyncio
    async def test_status_empty(self, client, admin_token):
        resp = await client.get("/admin/alerts/status", headers=auth_header(admin_token))
        assert resp.status_code == 200
        assert resp.json() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jinyoung/UniBridge/unibridge-service && python -m pytest tests/test_alerts.py::TestAlertRulesAPI tests/test_alerts.py::TestAlertHistoryAPI tests/test_alerts.py::TestAlertStatusAPI -v`
Expected: FAIL

- [ ] **Step 3: Add rules, history, status endpoints to alerts.py**

Append to `unibridge-service/app/routers/alerts.py`:

```python
# ── Rules ───────────────────────────────────────────────────────────────────

async def _build_rule_response(db: AsyncSession, rule: AlertRule) -> AlertRuleResponse:
    """Build AlertRuleResponse with channel details."""
    result = await db.execute(
        select(AlertRuleChannel).where(AlertRuleChannel.rule_id == rule.id)
    )
    mappings = result.scalars().all()
    channel_details: list[RuleChannelDetail] = []
    for m in mappings:
        ch_result = await db.execute(select(AlertChannel).where(AlertChannel.id == m.channel_id))
        ch = ch_result.scalar_one_or_none()
        channel_details.append(RuleChannelDetail(
            channel_id=m.channel_id,
            channel_name=ch.name if ch else "deleted",
            recipients=json.loads(m.recipients),
        ))
    return AlertRuleResponse(
        id=rule.id, name=rule.name, type=rule.type, target=rule.target,
        threshold=rule.threshold, enabled=rule.enabled, channels=channel_details,
        created_at=rule.created_at, updated_at=rule.updated_at,
    )


@router.get("/rules", response_model=list[AlertRuleResponse])
async def list_rules(
    _user: CurrentUser = Depends(require_permission("alerts.read")),
    db: AsyncSession = Depends(get_db),
) -> list[AlertRuleResponse]:
    result = await db.execute(select(AlertRule).order_by(AlertRule.id))
    rules = result.scalars().all()
    return [await _build_rule_response(db, r) for r in rules]


@router.post("/rules", response_model=AlertRuleResponse, status_code=201)
async def create_rule(
    body: AlertRuleCreate,
    _user: CurrentUser = Depends(require_permission("alerts.write")),
    db: AsyncSession = Depends(get_db),
) -> AlertRuleResponse:
    rule = AlertRule(
        name=body.name, type=body.type, target=body.target,
        threshold=body.threshold, enabled=body.enabled,
    )
    db.add(rule)
    await db.flush()
    for ch_map in body.channels:
        db.add(AlertRuleChannel(
            rule_id=rule.id, channel_id=ch_map.channel_id,
            recipients=json.dumps(ch_map.recipients),
        ))
    await db.commit()
    await db.refresh(rule)
    return await _build_rule_response(db, rule)


@router.put("/rules/{rule_id}", response_model=AlertRuleResponse)
async def update_rule(
    rule_id: int,
    body: AlertRuleUpdate,
    _user: CurrentUser = Depends(require_permission("alerts.write")),
    db: AsyncSession = Depends(get_db),
) -> AlertRuleResponse:
    result = await db.execute(select(AlertRule).where(AlertRule.id == rule_id))
    rule = result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    if body.name is not None:
        rule.name = body.name
    if body.type is not None:
        rule.type = body.type
    if body.target is not None:
        rule.target = body.target
    if body.threshold is not None:
        rule.threshold = body.threshold
    if body.enabled is not None:
        rule.enabled = body.enabled
    if body.channels is not None:
        await db.execute(sa_delete(AlertRuleChannel).where(AlertRuleChannel.rule_id == rule.id))
        for ch_map in body.channels:
            db.add(AlertRuleChannel(
                rule_id=rule.id, channel_id=ch_map.channel_id,
                recipients=json.dumps(ch_map.recipients),
            ))
    await db.commit()
    await db.refresh(rule)
    return await _build_rule_response(db, rule)


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: int,
    _user: CurrentUser = Depends(require_permission("alerts.write")),
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(select(AlertRule).where(AlertRule.id == rule_id))
    rule = result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    await db.delete(rule)
    await db.commit()


# ── History ─────────────────────────────────────────────────────────────────

@router.get("/history", response_model=list[AlertHistoryResponse])
async def list_history(
    alert_type: str | None = Query(None),
    target: str | None = Query(None),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _user: CurrentUser = Depends(require_permission("alerts.read")),
    db: AsyncSession = Depends(get_db),
) -> list[AlertHistoryResponse]:
    q = select(AlertHistory).order_by(AlertHistory.sent_at.desc())
    if alert_type:
        q = q.where(AlertHistory.alert_type == alert_type)
    if target:
        q = q.where(AlertHistory.target == target)
    if from_date:
        q = q.where(AlertHistory.sent_at >= from_date)
    if to_date:
        q = q.where(AlertHistory.sent_at <= to_date)
    q = q.offset(offset).limit(limit)
    result = await db.execute(q)
    rows = result.scalars().all()
    return [
        AlertHistoryResponse(
            id=h.id, rule_id=h.rule_id, channel_id=h.channel_id,
            alert_type=h.alert_type, target=h.target, message=h.message,
            recipients=json.loads(h.recipients) if h.recipients else None,
            sent_at=h.sent_at, success=h.success, error_detail=h.error_detail,
        )
        for h in rows
    ]


# ── Status ──────────────────────────────────────────────────────────────────

# Shared alert_state instance — initialized by alert_checker at startup
_alert_state = None


def set_alert_state(state) -> None:
    global _alert_state
    _alert_state = state


@router.get("/status", response_model=list[AlertStatusResponse])
async def alert_status(
    _user: CurrentUser = Depends(require_permission("alerts.read")),
) -> list[AlertStatusResponse]:
    if _alert_state is None:
        return []
    alerts = _alert_state.get_all_alerts()
    return [
        AlertStatusResponse(target=a["target"], type=a["type"], status=a["status"], since=a["since"])
        for a in alerts
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jinyoung/UniBridge/unibridge-service && python -m pytest tests/test_alerts.py::TestAlertRulesAPI tests/test_alerts.py::TestAlertHistoryAPI tests/test_alerts.py::TestAlertStatusAPI -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add unibridge-service/app/routers/alerts.py unibridge-service/tests/test_alerts.py
git commit -m "feat: add alert rules CRUD, history, and status API endpoints"
```

---

## Task 8: alert_checker — 60초 주기 헬스 체크 루프

**Files:**
- Create: `unibridge-service/app/services/alert_checker.py`
- Modify: `unibridge-service/app/main.py`
- Test: `unibridge-service/tests/test_alerts.py`

- [ ] **Step 1: Write failing test**

Append to `unibridge-service/tests/test_alerts.py`:

```python
from unittest.mock import AsyncMock, patch, MagicMock
from app.services.alert_checker import run_single_check


class TestAlertChecker:
    @pytest.mark.asyncio
    async def test_db_health_triggered(self, app):
        """Simulates a DB health check failure triggering an alert."""
        from app.services.alert_state import AlertStateManager
        state = AlertStateManager()

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_error_rate", new_callable=AsyncMock) as mock_err, \
             patch("app.services.alert_checker._dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.return_value = [("mydb", False)]
            mock_up.return_value = []
            mock_err.return_value = []

            await run_single_check(state)

            assert state.get_status("db_health", "mydb") == "alert"
            mock_dispatch.assert_called_once()
            call_args = mock_dispatch.call_args
            assert call_args[1]["alert_type"] == "triggered"
            assert call_args[1]["target"] == "mydb"

    @pytest.mark.asyncio
    async def test_db_health_resolved(self, app):
        """Simulates a DB recovery sending resolved alert."""
        from app.services.alert_state import AlertStateManager
        state = AlertStateManager()
        state.update("db_health", "mydb", is_healthy=False)

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_error_rate", new_callable=AsyncMock) as mock_err, \
             patch("app.services.alert_checker._dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.return_value = [("mydb", True)]
            mock_up.return_value = []
            mock_err.return_value = []

            await run_single_check(state)

            assert state.get_status("db_health", "mydb") == "ok"
            mock_dispatch.assert_called_once()
            assert mock_dispatch.call_args[1]["alert_type"] == "resolved"

    @pytest.mark.asyncio
    async def test_no_dispatch_when_no_transition(self, app):
        """No alert sent when state doesn't change."""
        from app.services.alert_state import AlertStateManager
        state = AlertStateManager()

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_error_rate", new_callable=AsyncMock) as mock_err, \
             patch("app.services.alert_checker._dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.return_value = [("mydb", True)]
            mock_up.return_value = []
            mock_err.return_value = []

            await run_single_check(state)

            mock_dispatch.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jinyoung/UniBridge/unibridge-service && python -m pytest tests/test_alerts.py::TestAlertChecker -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement alert_checker.py**

Create `unibridge-service/app/services/alert_checker.py`:

```python
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import AlertChannel, AlertHistory, AlertRule, AlertRuleChannel
from app.services.alert_sender import render_template, send_webhook
from app.services.alert_state import AlertStateManager

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 60  # seconds


async def _check_db_health() -> list[tuple[str, bool]]:
    """Check all registered DB connections. Returns [(alias, is_healthy)]."""
    from app.services.connection_manager import connection_manager
    results = []
    for alias in connection_manager.list_aliases():
        try:
            ok, _ = await connection_manager.test_connection(alias)
            results.append((alias, ok))
        except Exception as exc:
            logger.warning("DB health check failed for '%s': %s", alias, exc)
            results.append((alias, False))
    return results


async def _check_upstream_health() -> list[tuple[str, bool]]:
    """Check APISIX upstream health. Returns [(upstream_id, is_healthy)]."""
    from app.services import apisix_client
    results = []
    try:
        data = await apisix_client.list_resources("upstreams")
        for item in data.get("items", []):
            uid = item.get("id", "unknown")
            nodes = item.get("nodes", {})
            # If no nodes or all nodes have 0 weight, consider unhealthy
            is_healthy = bool(nodes) and any(
                w > 0 for w in (nodes.values() if isinstance(nodes, dict) else [])
            )
            results.append((str(uid), is_healthy))
    except Exception as exc:
        logger.warning("Upstream health check failed: %s", exc)
    return results


async def _check_error_rate() -> list[tuple[str, float]]:
    """Check 5xx error rate from Prometheus. Returns [("global", rate_pct)]."""
    from app.services import prometheus_client
    try:
        result = await prometheus_client.instant_query(
            'sum(rate(apisix_http_status{code=~"5.."}[5m])) / sum(rate(apisix_http_status[5m])) * 100'
        )
        if result:
            val = float(result[0].get("value", [0, 0])[1])
            if val != val:  # NaN check
                val = 0.0
            return [("global", val)]
    except Exception as exc:
        logger.warning("Error rate check failed: %s", exc)
    return []


async def _dispatch_alert(
    *,
    rule_type: str,
    alert_type: str,
    target: str,
    message: str,
) -> None:
    """Find matching rules and send alerts through mapped channels."""
    async with async_session() as db:
        # Find matching enabled rules
        q = select(AlertRule).where(
            AlertRule.enabled.is_(True),
            AlertRule.type == rule_type,
            AlertRule.target.in_([target, "*"]),
        )
        result = await db.execute(q)
        rules = result.scalars().all()

        now = datetime.now(timezone.utc).isoformat()

        for rule in rules:
            rc_result = await db.execute(
                select(AlertRuleChannel).where(AlertRuleChannel.rule_id == rule.id)
            )
            mappings = rc_result.scalars().all()

            for mapping in mappings:
                ch_result = await db.execute(
                    select(AlertChannel).where(
                        AlertChannel.id == mapping.channel_id,
                        AlertChannel.enabled.is_(True),
                    )
                )
                channel = ch_result.scalar_one_or_none()
                if channel is None:
                    continue

                recipients_list = json.loads(mapping.recipients)
                recipients_str = ", ".join(recipients_list)

                status_label = "장애 발생" if alert_type == "triggered" else "정상 복구"
                payload = render_template(
                    channel.payload_template,
                    alert_type=alert_type,
                    target_name=target,
                    status=status_label,
                    message=message,
                    timestamp=now,
                    recipients=recipients_str,
                )
                headers = json.loads(channel.headers) if channel.headers else None
                ok, err = await send_webhook(url=channel.webhook_url, payload=payload, headers=headers)

                # Record history
                history = AlertHistory(
                    rule_id=rule.id, channel_id=channel.id,
                    alert_type=alert_type, target=target, message=message,
                    recipients=mapping.recipients,
                    success=ok, error_detail=err,
                )
                db.add(history)

            await db.commit()


async def run_single_check(state: AlertStateManager) -> None:
    """Execute one round of all health checks."""
    # 1. DB health
    db_results = await _check_db_health()
    for alias, is_healthy in db_results:
        transition = state.update("db_health", alias, is_healthy=is_healthy)
        if transition:
            msg = f"Database '{alias}' connection {'restored' if transition == 'resolved' else 'failed'}."
            await _dispatch_alert(
                rule_type="db_health", alert_type=transition,
                target=alias, message=msg,
            )

    # 2. Upstream health
    upstream_results = await _check_upstream_health()
    for uid, is_healthy in upstream_results:
        transition = state.update("upstream_health", uid, is_healthy=is_healthy)
        if transition:
            msg = f"Upstream '{uid}' {'recovered' if transition == 'resolved' else 'is down'}."
            await _dispatch_alert(
                rule_type="upstream_health", alert_type=transition,
                target=uid, message=msg,
            )

    # 3. Error rate
    error_results = await _check_error_rate()
    for target, rate in error_results:
        # Check against each matching rule's threshold
        async with async_session() as db:
            q = select(AlertRule).where(
                AlertRule.enabled.is_(True),
                AlertRule.type == "error_rate",
                AlertRule.target.in_([target, "*"]),
            )
            result = await db.execute(q)
            rules = result.scalars().all()

        for rule in rules:
            threshold = rule.threshold or 10.0
            is_healthy = rate < threshold
            transition = state.update("error_rate", target, is_healthy=is_healthy)
            if transition:
                msg = f"5xx error rate is {rate:.1f}% (threshold: {threshold}%)."
                await _dispatch_alert(
                    rule_type="error_rate", alert_type=transition,
                    target=target, message=msg,
                )
            break  # One error_rate check per target


async def start_checker(state: AlertStateManager) -> asyncio.Task:
    """Start the periodic health check loop as a background task."""
    async def _loop():
        logger.info("Alert checker started (interval=%ds)", CHECK_INTERVAL)
        while True:
            try:
                await run_single_check(state)
            except Exception:
                logger.exception("Alert checker cycle failed")
            await asyncio.sleep(CHECK_INTERVAL)

    return asyncio.create_task(_loop())
```

- [ ] **Step 4: Wire up in main.py lifespan**

In `unibridge-service/app/main.py`, add to lifespan startup (after APISIX provisioning, before `yield`):

```python
    from app.services.alert_state import AlertStateManager
    from app.services.alert_checker import start_checker
    from app.routers.alerts import set_alert_state

    alert_state = AlertStateManager()
    set_alert_state(alert_state)
    _alert_task = await start_checker(alert_state)
    logger.info("Alert checker started")
```

Add to lifespan shutdown (before `logger.info("Shutdown complete.")`):

```python
    _alert_task.cancel()
    logger.info("Alert checker stopped")
```

Note: `_alert_task` needs to be stored. Use a module-level variable or store on `app.state`:

```python
    app.state.alert_task = await start_checker(alert_state)
```

And in shutdown:

```python
    if hasattr(app, "state") and hasattr(app.state, "alert_task"):
        app.state.alert_task.cancel()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /home/jinyoung/UniBridge/unibridge-service && python -m pytest tests/test_alerts.py::TestAlertChecker -v`
Expected: PASS

- [ ] **Step 6: Run all alert tests**

Run: `cd /home/jinyoung/UniBridge/unibridge-service && python -m pytest tests/test_alerts.py -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add unibridge-service/app/services/alert_checker.py unibridge-service/app/main.py unibridge-service/tests/test_alerts.py
git commit -m "feat: add alert_checker background loop with DB/upstream/error-rate checks"
```

---

## Task 9: 프론트엔드 — API 클라이언트 함수 추가

**Files:**
- Modify: `unibridge-ui/src/api/client.ts`

- [ ] **Step 1: Add alert API types and functions to client.ts**

Append to `unibridge-ui/src/api/client.ts`:

```typescript
// ── Alerts ──────────────────────────────────────────────────────────────────

export interface AlertChannel {
  id: number;
  name: string;
  webhook_url: string;
  payload_template: string;
  headers: Record<string, string> | null;
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface AlertChannelCreate {
  name: string;
  webhook_url: string;
  payload_template: string;
  headers?: Record<string, string>;
  enabled?: boolean;
}

export interface RuleChannelMapping {
  channel_id: number;
  recipients: string[];
}

export interface RuleChannelDetail {
  channel_id: number;
  channel_name: string;
  recipients: string[];
}

export interface AlertRule {
  id: number;
  name: string;
  type: 'db_health' | 'upstream_health' | 'error_rate';
  target: string;
  threshold: number | null;
  enabled: boolean;
  channels: RuleChannelDetail[];
  created_at?: string;
  updated_at?: string;
}

export interface AlertRuleCreate {
  name: string;
  type: 'db_health' | 'upstream_health' | 'error_rate';
  target: string;
  threshold?: number;
  enabled?: boolean;
  channels: RuleChannelMapping[];
}

export interface AlertHistoryEntry {
  id: number;
  rule_id: number | null;
  channel_id: number | null;
  alert_type: 'triggered' | 'resolved';
  target: string;
  message: string;
  recipients: string[] | null;
  sent_at: string;
  success: boolean | null;
  error_detail: string | null;
}

export interface AlertStatus {
  target: string;
  type: string;
  status: 'ok' | 'alert';
  since: string | null;
}

// Channels
export async function getAlertChannels(): Promise<AlertChannel[]> {
  const { data } = await api.get('/admin/alerts/channels');
  return data;
}

export async function createAlertChannel(body: AlertChannelCreate): Promise<AlertChannel> {
  const { data } = await api.post('/admin/alerts/channels', body);
  return data;
}

export async function updateAlertChannel(id: number, body: Partial<AlertChannelCreate>): Promise<AlertChannel> {
  const { data } = await api.put(`/admin/alerts/channels/${id}`, body);
  return data;
}

export async function deleteAlertChannel(id: number): Promise<void> {
  await api.delete(`/admin/alerts/channels/${id}`);
}

export async function testAlertChannel(id: number): Promise<{ success: boolean; error: string | null }> {
  const { data } = await api.post(`/admin/alerts/channels/${id}/test`);
  return data;
}

// Rules
export async function getAlertRules(): Promise<AlertRule[]> {
  const { data } = await api.get('/admin/alerts/rules');
  return data;
}

export async function createAlertRule(body: AlertRuleCreate): Promise<AlertRule> {
  const { data } = await api.post('/admin/alerts/rules', body);
  return data;
}

export async function updateAlertRule(id: number, body: Partial<AlertRuleCreate>): Promise<AlertRule> {
  const { data } = await api.put(`/admin/alerts/rules/${id}`, body);
  return data;
}

export async function deleteAlertRule(id: number): Promise<void> {
  await api.delete(`/admin/alerts/rules/${id}`);
}

// History & Status
export async function getAlertHistory(params?: {
  alert_type?: string;
  target?: string;
  from_date?: string;
  to_date?: string;
  limit?: number;
  offset?: number;
}): Promise<AlertHistoryEntry[]> {
  const { data } = await api.get('/admin/alerts/history', { params });
  return data;
}

export async function getAlertStatus(): Promise<AlertStatus[]> {
  const { data } = await api.get('/admin/alerts/status');
  return data;
}
```

- [ ] **Step 2: Commit**

```bash
git add unibridge-ui/src/api/client.ts
git commit -m "feat: add alert API client functions (channels, rules, history, status)"
```

---

## Task 10: 프론트엔드 — i18n 번역 키 추가

**Files:**
- Modify: `unibridge-ui/src/locales/ko.json`
- Modify: `unibridge-ui/src/locales/en.json`

- [ ] **Step 1: Add Korean translation keys**

Add under root level of `ko.json`:

```json
"nav.alertSettings": "알림 설정",
"nav.alertHistory": "알림 이력",

"alerts.settingsTitle": "알림 설정",
"alerts.settingsSubtitle": "알림 채널과 규칙을 관리합니다.",
"alerts.channelsTab": "채널",
"alerts.rulesTab": "규칙",
"alerts.addChannel": "채널 추가",
"alerts.addRule": "규칙 추가",
"alerts.editChannel": "채널 수정",
"alerts.editRule": "규칙 수정",
"alerts.channelName": "채널 이름",
"alerts.webhookUrl": "Webhook URL",
"alerts.payloadTemplate": "Payload 템플릿",
"alerts.headers": "HTTP 헤더",
"alerts.headerName": "헤더 이름",
"alerts.headerValue": "헤더 값",
"alerts.addHeader": "헤더 추가",
"alerts.enabled": "활성",
"alerts.testChannel": "테스트 발송",
"alerts.testSuccess": "테스트 발송 성공",
"alerts.testFailed": "테스트 발송 실패",
"alerts.ruleName": "규칙 이름",
"alerts.ruleType": "타입",
"alerts.ruleTarget": "대상",
"alerts.threshold": "임계치 (%)",
"alerts.channels": "채널",
"alerts.recipients": "수신자",
"alerts.addRecipient": "수신자 추가",
"alerts.typeDbHealth": "DB 연결",
"alerts.typeUpstreamHealth": "업스트림",
"alerts.typeErrorRate": "에러율",
"alerts.targetAll": "전체 (*)",
"alerts.noChannels": "등록된 채널이 없습니다.",
"alerts.noRules": "등록된 규칙이 없습니다.",
"alerts.deleteConfirm": "정말 삭제하시겠습니까?",
"alerts.save": "저장",
"alerts.cancel": "취소",
"alerts.delete": "삭제",

"alerts.historyTitle": "알림 이력",
"alerts.historySubtitle": "발송된 알림 이력을 확인합니다.",
"alerts.filterAlertType": "알림 유형",
"alerts.filterTarget": "대상",
"alerts.triggered": "장애 발생",
"alerts.resolved": "정상 복구",
"alerts.sentAt": "발송 시각",
"alerts.target": "대상",
"alerts.message": "메시지",
"alerts.success": "성공",
"alerts.failed": "실패",
"alerts.noHistory": "알림 이력이 없습니다.",
"alerts.templateHelp": "사용 가능한 변수: {{alert_type}}, {{target_name}}, {{status}}, {{message}}, {{timestamp}}, {{recipients}}"
```

- [ ] **Step 2: Add English translation keys**

Add the same keys in English to `en.json`:

```json
"nav.alertSettings": "Alert Settings",
"nav.alertHistory": "Alert History",

"alerts.settingsTitle": "Alert Settings",
"alerts.settingsSubtitle": "Manage alert channels and rules.",
"alerts.channelsTab": "Channels",
"alerts.rulesTab": "Rules",
"alerts.addChannel": "Add Channel",
"alerts.addRule": "Add Rule",
"alerts.editChannel": "Edit Channel",
"alerts.editRule": "Edit Rule",
"alerts.channelName": "Channel Name",
"alerts.webhookUrl": "Webhook URL",
"alerts.payloadTemplate": "Payload Template",
"alerts.headers": "HTTP Headers",
"alerts.headerName": "Header Name",
"alerts.headerValue": "Header Value",
"alerts.addHeader": "Add Header",
"alerts.enabled": "Enabled",
"alerts.testChannel": "Test Send",
"alerts.testSuccess": "Test send succeeded",
"alerts.testFailed": "Test send failed",
"alerts.ruleName": "Rule Name",
"alerts.ruleType": "Type",
"alerts.ruleTarget": "Target",
"alerts.threshold": "Threshold (%)",
"alerts.channels": "Channels",
"alerts.recipients": "Recipients",
"alerts.addRecipient": "Add Recipient",
"alerts.typeDbHealth": "DB Health",
"alerts.typeUpstreamHealth": "Upstream",
"alerts.typeErrorRate": "Error Rate",
"alerts.targetAll": "All (*)",
"alerts.noChannels": "No channels configured.",
"alerts.noRules": "No rules configured.",
"alerts.deleteConfirm": "Are you sure you want to delete?",
"alerts.save": "Save",
"alerts.cancel": "Cancel",
"alerts.delete": "Delete",

"alerts.historyTitle": "Alert History",
"alerts.historySubtitle": "View sent alert history.",
"alerts.filterAlertType": "Alert Type",
"alerts.filterTarget": "Target",
"alerts.triggered": "Triggered",
"alerts.resolved": "Resolved",
"alerts.sentAt": "Sent At",
"alerts.target": "Target",
"alerts.message": "Message",
"alerts.success": "Success",
"alerts.failed": "Failed",
"alerts.noHistory": "No alert history.",
"alerts.templateHelp": "Available variables: {{alert_type}}, {{target_name}}, {{status}}, {{message}}, {{timestamp}}, {{recipients}}"
```

- [ ] **Step 3: Commit**

```bash
git add unibridge-ui/src/locales/ko.json unibridge-ui/src/locales/en.json
git commit -m "feat: add alert i18n translation keys (ko, en)"
```

---

## Task 11: 프론트엔드 — AlertSettings 페이지

**Files:**
- Create: `unibridge-ui/src/pages/AlertSettings.tsx`
- Create: `unibridge-ui/src/pages/AlertSettings.css`

- [ ] **Step 1: Implement AlertSettings page**

This page has two tabs (Channels, Rules) following the existing UI patterns from GatewayRoutes.tsx and Connections.tsx:

- Channels tab: table with name, URL, enabled toggle, edit/delete/test buttons + add modal
- Rules tab: table with name, type, target, threshold, enabled toggle, channel mappings + add/edit modal
- Rule create/edit modal: type selector, target dropdown (DB list from `/admin/databases` or upstream list, or `*`), threshold input (for error_rate), channel multi-select with per-channel recipients input
- Follow existing CSS class patterns (`page-header`, `page-subtitle`, `table`, `modal`, etc.)

Full implementation in the AlertSettings.tsx file — two tabs, modals for create/edit, using `useQuery` and `useMutation` from TanStack Query, `useTranslation` for i18n. Pattern matches GatewayRoutes.tsx closely.

- [ ] **Step 2: Implement AlertSettings.css**

Follow existing page CSS patterns (from `Dashboard.css`, `GatewayMonitoring.css`).

- [ ] **Step 3: Commit**

```bash
git add unibridge-ui/src/pages/AlertSettings.tsx unibridge-ui/src/pages/AlertSettings.css
git commit -m "feat: add AlertSettings page (channels + rules management UI)"
```

---

## Task 12: 프론트엔드 — AlertHistory 페이지

**Files:**
- Create: `unibridge-ui/src/pages/AlertHistory.tsx`
- Create: `unibridge-ui/src/pages/AlertHistory.css`

- [ ] **Step 1: Implement AlertHistory page**

Pattern matches AuditLogs.tsx:

- Filter bar: alert_type dropdown (triggered/resolved), target input, date range
- Table: sent_at, alert_type badge, target, message, success/fail indicator
- Pagination (50 per page)
- Auto-refresh every 30 seconds

- [ ] **Step 2: Implement AlertHistory.css**

Follow existing page CSS patterns.

- [ ] **Step 3: Commit**

```bash
git add unibridge-ui/src/pages/AlertHistory.tsx unibridge-ui/src/pages/AlertHistory.css
git commit -m "feat: add AlertHistory page with filters and pagination"
```

---

## Task 13: 프론트엔드 — 라우트 + 사이드바 등록

**Files:**
- Modify: `unibridge-ui/src/App.tsx`
- Modify: `unibridge-ui/src/components/Layout.tsx`

- [ ] **Step 1: Add routes to App.tsx**

Add imports for `AlertSettings` and `AlertHistory`, then add routes:

```tsx
import AlertSettings from './pages/AlertSettings';
import AlertHistory from './pages/AlertHistory';

// Inside <Routes>, after the users route:
<Route path="/alerts/settings" element={<ProtectedRoute permission="alerts.write"><AlertSettings /></ProtectedRoute>} />
<Route path="/alerts/history" element={<ProtectedRoute permission="alerts.read"><AlertHistory /></ProtectedRoute>} />
```

- [ ] **Step 2: Add nav items to Layout.tsx**

Add to the `navItems` array, creating a new `alerts` section:

```typescript
  { to: '/alerts/settings', labelKey: 'nav.alertSettings', icon: 'Alert Settings', section: 'alerts', permission: 'alerts.write' },
  { to: '/alerts/history', labelKey: 'nav.alertHistory', icon: 'Alert History', section: 'alerts', permission: 'alerts.read' },
```

Add SVG icons for the new nav items in the icon rendering section:

```tsx
{item.icon === 'Alert Settings' && (
  <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
    <path d="M9 2L10.5 6H7.5L9 2z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    <path d="M5 8h8v5a2 2 0 01-2 2H7a2 2 0 01-2-2V8z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    <path d="M9 15v2" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
  </svg>
)}
{item.icon === 'Alert History' && (
  <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
    <circle cx="9" cy="9" r="7" stroke="currentColor" strokeWidth="1.5" />
    <path d="M9 5v4l3 2" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
)}
```

- [ ] **Step 3: Verify build**

Run: `cd /home/jinyoung/UniBridge/unibridge-ui && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
git add unibridge-ui/src/App.tsx unibridge-ui/src/components/Layout.tsx
git commit -m "feat: register alert pages in router and sidebar navigation"
```

---

## Task 14: 전체 통합 테스트

**Files:**
- Test: `unibridge-service/tests/test_alerts.py`
- Test: `unibridge-ui/` (type check)

- [ ] **Step 1: Run all backend tests**

Run: `cd /home/jinyoung/UniBridge/unibridge-service && python -m pytest tests/ -v`
Expected: ALL PASS (no regressions)

- [ ] **Step 2: Run frontend type check**

Run: `cd /home/jinyoung/UniBridge/unibridge-ui && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 3: Run frontend lint**

Run: `cd /home/jinyoung/UniBridge/unibridge-ui && npx eslint src/ --ext .ts,.tsx`
Expected: No errors

- [ ] **Step 4: Run frontend tests**

Run: `cd /home/jinyoung/UniBridge/unibridge-ui && npx vitest run`
Expected: ALL PASS

- [ ] **Step 5: Final commit (if any fixes needed)**

```bash
git add -A
git commit -m "fix: resolve integration issues from alert system implementation"
```
