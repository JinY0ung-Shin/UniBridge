# Alert Owner Mail Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 리소스별 담당자 그룹과 기본 메일 채널을 이용해 DB, upstream, route 장애 알림을 담당자 이메일 목록으로 자동 발송한다.

**Architecture:** 기존 `AlertChannel`과 webhook 발송 경로는 유지하고, `OwnerGroup`, `ResourceOwner`, `AlertSettings`를 추가한다. 회사 메일 API의 수신자 객체 구조는 `AlertChannel.recipient_item_template`로 설정하고, 백엔드는 이메일 목록을 `{{recipients_json}}` raw JSON 배열로 렌더링한다. DB/upstream 알림은 리소스 상태 전이만으로 owner 기반 발송하고, route error 알림은 기존 `AlertRule` threshold 감지 정책을 유지하면서 수신자만 owner 기반으로 바꾼다.

**Tech Stack:** FastAPI, SQLAlchemy async ORM, Alembic, Pydantic v2, pytest, React 19, TanStack Query, TypeScript, Vitest.

**Spec:** `docs/superpowers/specs/2026-05-08-alert-owner-routing-design.md`

---

## 파일 구조

**백엔드 데이터 모델**
- Modify: `unibridge-service/app/models.py` - `AlertChannel`, `AlertHistory` 컬럼 확장과 `OwnerGroup`, `ResourceOwner`, `AlertSettings` 모델 추가
- Modify: `unibridge-service/app/database.py` - `ALEMBIC_HEAD_REVISION`을 새 revision으로 갱신
- Create: `unibridge-service/alembic/versions/0004_alert_owner_routing.py` - 새 테이블과 컬럼 migration
- Modify: `unibridge-service/tests/test_database_schema.py` - Alembic head와 신규 컬럼/schema 검증

**백엔드 렌더링/발송**
- Modify: `unibridge-service/app/services/alert_sender.py` - `{{recipients_json}}`와 recipient item rendering 추가
- Create: `unibridge-service/app/services/alert_owner_dispatcher.py` - owner lookup, fallback, mail channel dispatch, history write 담당
- Modify: `unibridge-service/app/services/alert_checker.py` - DB/upstream/route 발송을 owner dispatcher로 연결, global `error_rate`는 legacy rule-channel 발송 유지
- Modify: `unibridge-service/tests/test_alert_sender.py` - recipient item/template rendering 단위 테스트
- Create: `unibridge-service/tests/test_alert_owner_dispatcher.py` - owner/fallback/mail channel dispatch 단위 테스트
- Modify: `unibridge-service/tests/test_alert_checker.py` - checker가 owner dispatch에 resource type/id를 넘기는지 검증

**백엔드 API**
- Modify: `unibridge-service/app/schemas.py` - alert settings, owner group, resource owner schemas 추가
- Modify: `unibridge-service/app/routers/alerts.py` - channel 확장, settings CRUD, owner group CRUD, resource owner CRUD/list 추가
- Modify: `unibridge-service/tests/test_alert_channels.py` - `recipient_item_template` CRUD 및 channel test 검증
- Modify: `unibridge-service/tests/test_alerts_router.py` - settings, owner groups, resource owners endpoint 통합 테스트
- Modify: `unibridge-service/tests/test_alert_rules.py` - rule create/update에서 channels 생략 가능하고 legacy response 유지 검증

**프론트엔드**
- Modify: `unibridge-ui/src/api/client.ts` - 새 타입과 API 함수 추가, `AlertChannel` 확장
- Create: `unibridge-ui/src/pages/alerts/AlertMailChannelPanel.tsx` - 기본 메일 채널과 recipient item template 설정
- Create: `unibridge-ui/src/pages/alerts/AlertOwnerGroupsPanel.tsx` - 담당자 그룹 CRUD
- Create: `unibridge-ui/src/pages/alerts/AlertResourceOwnersPanel.tsx` - 리소스별 담당자 지정
- Create: `unibridge-ui/src/pages/alerts/AlertRulesPanel.tsx` - 기존 rule 목록/생성/수정에서 수신자 입력 제거
- Modify: `unibridge-ui/src/pages/AlertSettings.tsx` - 탭 컨테이너로 축소하고 새 패널 조립
- Modify: `unibridge-ui/src/pages/AlertSettings.css` - 새 탭/테이블/폼 스타일 추가
- Modify: `unibridge-ui/src/locales/en.json`
- Modify: `unibridge-ui/src/locales/ko.json`
- Modify: `unibridge-ui/src/test/AlertSettings.test.tsx` - 새 탭과 owner flow 테스트

---

## Task 1: DB 모델과 Alembic migration

**Files:**
- Modify: `unibridge-service/app/models.py`
- Modify: `unibridge-service/app/database.py`
- Create: `unibridge-service/alembic/versions/0004_alert_owner_routing.py`
- Modify: `unibridge-service/tests/test_database_schema.py`

- [ ] **Step 1: 실패 테스트 작성**

`unibridge-service/tests/test_database_schema.py` 끝에 추가:

```python
@pytest.mark.asyncio
async def test_alert_owner_routing_schema_is_created_by_metadata():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with engine.connect() as conn:
        table_names = await conn.run_sync(
            lambda sync_conn: set(inspect(sync_conn).get_table_names())
        )
        alert_channel_cols = await conn.run_sync(
            lambda sync_conn: {col["name"] for col in inspect(sync_conn).get_columns("alert_channels")}
        )
        alert_history_cols = await conn.run_sync(
            lambda sync_conn: {col["name"] for col in inspect(sync_conn).get_columns("alert_history")}
        )

    assert {"owner_groups", "resource_owners", "alert_settings"} <= table_names
    assert "recipient_item_template" in alert_channel_cols
    assert {"resource_type", "owner_group_id"} <= alert_history_cols
    await engine.dispose()
```

Update the existing `test_init_db_runs_alembic_and_stamps_head_for_file_sqlite` assertion expectation after implementation:

```python
assert revision == "0004_alert_owner_routing"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd unibridge-service && pytest tests/test_database_schema.py::test_alert_owner_routing_schema_is_created_by_metadata -v
```

Expected: FAIL because `owner_groups`, `resource_owners`, `alert_settings`, and new alert columns do not exist.

- [ ] **Step 3: Add SQLAlchemy models**

In `unibridge-service/app/models.py`, extend `AlertChannel`:

```python
class AlertChannel(Base):
    __tablename__ = "alert_channels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    webhook_url = Column(String, nullable=False)
    payload_template = Column(Text, nullable=False)
    recipient_item_template = Column(Text, nullable=True)
    headers = Column(Text, nullable=True)  # JSON object
    enabled = Column(Boolean, default=True, nullable=False, server_default="true")
    created_at = Column(UtcDateTime, default=utcnow)
    updated_at = Column(UtcDateTime, default=utcnow, onupdate=utcnow)
```

Add these classes after `AlertRuleChannel`:

```python
class OwnerGroup(Base):
    __tablename__ = "owner_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    emails = Column(Text, nullable=False)  # JSON array of strings
    enabled = Column(Boolean, default=True, nullable=False, server_default="true")
    created_at = Column(UtcDateTime, default=utcnow, nullable=False)
    updated_at = Column(UtcDateTime, default=utcnow, onupdate=utcnow, nullable=False)

    def __init__(self, **kwargs):
        kwargs.setdefault("enabled", True)
        super().__init__(**kwargs)


class ResourceOwner(Base):
    __tablename__ = "resource_owners"

    id = Column(Integer, primary_key=True, autoincrement=True)
    resource_type = Column(String(20), nullable=False)
    resource_id = Column(String(200), nullable=False)
    owner_group_id = Column(Integer, ForeignKey("owner_groups.id", ondelete="RESTRICT"), nullable=False)
    created_at = Column(UtcDateTime, default=utcnow, nullable=False)
    updated_at = Column(UtcDateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("resource_type", "resource_id", name="uq_resource_owner_type_id"),
    )


class AlertSettings(Base):
    __tablename__ = "alert_settings"

    id = Column(Integer, primary_key=True)
    mail_channel_id = Column(Integer, ForeignKey("alert_channels.id", ondelete="RESTRICT"), nullable=True)
    fallback_owner_group_id = Column(Integer, ForeignKey("owner_groups.id", ondelete="RESTRICT"), nullable=True)
    route_error_threshold_pct = Column(Float, default=10.0, nullable=False)
    check_interval_seconds = Column(Integer, default=60, nullable=False)
    updated_at = Column(UtcDateTime, default=utcnow, onupdate=utcnow, nullable=False)
```

Extend `AlertHistory`:

```python
class AlertHistory(Base):
    __tablename__ = "alert_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rule_id = Column(Integer, ForeignKey("alert_rules.id", ondelete="SET NULL"), nullable=True)
    channel_id = Column(Integer, ForeignKey("alert_channels.id", ondelete="SET NULL"), nullable=True)
    owner_group_id = Column(Integer, ForeignKey("owner_groups.id", ondelete="SET NULL"), nullable=True)
    resource_type = Column(String(20), nullable=True)
    alert_type = Column(String(20), nullable=False)
    target = Column(String(100), nullable=False)
    message = Column(Text, nullable=False)
    recipients = Column(Text, nullable=True)
    sent_at = Column(UtcDateTime, default=utcnow)
    success = Column(Boolean, nullable=True)
    error_detail = Column(Text, nullable=True)
```

- [ ] **Step 4: Add migration**

Create `unibridge-service/alembic/versions/0004_alert_owner_routing.py`:

```python
"""Add owner-based alert routing.

Revision ID: 0004_alert_owner_routing
Revises: 0003_alert_state
Create Date: 2026-05-08
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0004_alert_owner_routing"
down_revision = "0003_alert_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("alert_channels") as batch:
        batch.add_column(sa.Column("recipient_item_template", sa.Text(), nullable=True))

    op.create_table(
        "owner_groups",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("emails", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "resource_owners",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("resource_type", sa.String(length=20), nullable=False),
        sa.Column("resource_id", sa.String(length=200), nullable=False),
        sa.Column("owner_group_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_group_id"], ["owner_groups.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("resource_type", "resource_id", name="uq_resource_owner_type_id"),
    )
    op.create_table(
        "alert_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mail_channel_id", sa.Integer(), nullable=True),
        sa.Column("fallback_owner_group_id", sa.Integer(), nullable=True),
        sa.Column("route_error_threshold_pct", sa.Float(), nullable=False, server_default="10.0"),
        sa.Column("check_interval_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["mail_channel_id"], ["alert_channels.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["fallback_owner_group_id"], ["owner_groups.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("id = 1", name="ck_alert_settings_singleton"),
    )
    with op.batch_alter_table("alert_history") as batch:
        batch.add_column(sa.Column("owner_group_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("resource_type", sa.String(length=20), nullable=True))
        batch.create_foreign_key(
            "fk_alert_history_owner_group_id_owner_groups",
            "owner_groups",
            ["owner_group_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.execute(
        "INSERT INTO alert_settings (id, route_error_threshold_pct, check_interval_seconds, updated_at) "
        "VALUES (1, 10.0, 60, CURRENT_TIMESTAMP)"
    )


def downgrade() -> None:
    with op.batch_alter_table("alert_history") as batch:
        batch.drop_constraint("fk_alert_history_owner_group_id_owner_groups", type_="foreignkey")
        batch.drop_column("resource_type")
        batch.drop_column("owner_group_id")
    op.drop_table("alert_settings")
    op.drop_table("resource_owners")
    op.drop_table("owner_groups")
    with op.batch_alter_table("alert_channels") as batch:
        batch.drop_column("recipient_item_template")
```

Update `unibridge-service/app/database.py`:

```python
ALEMBIC_HEAD_REVISION = "0004_alert_owner_routing"
```

- [ ] **Step 5: Run schema tests**

```bash
cd unibridge-service && pytest tests/test_database_schema.py -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add unibridge-service/app/models.py unibridge-service/app/database.py unibridge-service/alembic/versions/0004_alert_owner_routing.py unibridge-service/tests/test_database_schema.py
git commit -m "feat(alerts): add owner routing schema"
```

---

## Task 2: Recipient JSON template rendering

**Files:**
- Modify: `unibridge-service/app/services/alert_sender.py`
- Modify: `unibridge-service/tests/test_alert_sender.py`

- [ ] **Step 1: Failing tests**

Append to `unibridge-service/tests/test_alert_sender.py`:

```python
import json

from app.services.alert_sender import render_recipient_items


class TestRecipientItemRendering:
    def test_render_recipient_items_builds_json_array(self):
        template = '{"emailAddress":"{{email}}","recipientType":"TO"}'
        result = render_recipient_items(template, ["kim@company.com", "lee@company.com"])
        parsed = json.loads(result)
        assert parsed == [
            {"emailAddress": "kim@company.com", "recipientType": "TO"},
            {"emailAddress": "lee@company.com", "recipientType": "TO"},
        ]

    def test_render_recipient_items_rejects_non_object_template(self):
        template = '"{{email}}"'
        with pytest.raises(ValueError, match="JSON object"):
            render_recipient_items(template, ["kim@company.com"])

    def test_render_template_injects_recipients_json_raw(self):
        payload = render_template(
            '{"recipients":{{recipients_json}},"to":"{{recipients}}"}',
            alert_type="triggered",
            target_name="payment-db",
            status="장애 발생",
            message="Database failed",
            timestamp="2026-05-08T00:00:00+00:00",
            recipients="kim@company.com, lee@company.com",
            recipients_json='[{"emailAddress":"kim@company.com","recipientType":"TO"}]',
        )
        assert json.loads(payload)["recipients"] == [
            {"emailAddress": "kim@company.com", "recipientType": "TO"}
        ]
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd unibridge-service && pytest tests/test_alert_sender.py::TestRecipientItemRendering -v
```

Expected: FAIL because `render_recipient_items` and `recipients_json` do not exist.

- [ ] **Step 3: Implement rendering helpers**

In `unibridge-service/app/services/alert_sender.py`, add imports:

```python
import json
```

Add helper before `render_template`:

```python
def render_recipient_items(template: str, emails: list[str]) -> str:
    """Render one JSON object per email and return a JSON array string."""
    items: list[dict] = []
    for email in emails:
        rendered = template.replace("{{email}}", email)
        try:
            parsed = json.loads(rendered)
        except json.JSONDecodeError as exc:
            raise ValueError(f"recipient_item_template rendered invalid JSON for {email}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("recipient_item_template must render to a JSON object")
        items.append(parsed)
    return json.dumps(items, ensure_ascii=False)
```

Extend `render_template` signature and replacements:

```python
def render_template(
    template: str,
    *,
    alert_type: str,
    target_name: str,
    status: str,
    message: str,
    timestamp: str,
    recipients: str,
    recipients_json: str = "[]",
    rate: str = "",
    threshold: str = "",
    rule_name: str = "",
) -> str:
    replacements = {
        "{{alert_type}}": alert_type,
        "{{target_name}}": target_name,
        "{{status}}": status,
        "{{message}}": message,
        "{{timestamp}}": timestamp,
        "{{recipients}}": recipients,
        "{{recipients_json}}": recipients_json,
        "{{rate}}": rate,
        "{{threshold}}": threshold,
        "{{rule_name}}": rule_name,
    }
```

- [ ] **Step 4: Run alert sender tests**

```bash
cd unibridge-service && pytest tests/test_alert_sender.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add unibridge-service/app/services/alert_sender.py unibridge-service/tests/test_alert_sender.py
git commit -m "feat(alerts): render recipient JSON templates"
```

---

## Task 3: Schemas and channel/settings APIs

**Files:**
- Modify: `unibridge-service/app/schemas.py`
- Modify: `unibridge-service/app/routers/alerts.py`
- Modify: `unibridge-service/tests/test_alert_channels.py`
- Modify: `unibridge-service/tests/test_alerts_router.py`

- [ ] **Step 1: Failing tests for channel extension**

Add to `unibridge-service/tests/test_alert_channels.py`:

```python
    @pytest.mark.asyncio
    async def test_channel_round_trips_recipient_item_template(self, client, admin_token):
        template = '{"emailAddress":"{{email}}","recipientType":"TO"}'
        resp = await client.post("/admin/alerts/channels", json={
            "name": "mail-api",
            "webhook_url": "http://mail.internal/api/send",
            "payload_template": '{"recipients":{{recipients_json}}}',
            "recipient_item_template": template,
        }, headers=auth_header(admin_token))
        assert resp.status_code == 201
        assert resp.json()["recipient_item_template"] == template

        ch_id = resp.json()["id"]
        update = await client.put(f"/admin/alerts/channels/{ch_id}", json={
            "recipient_item_template": '{"mail":"{{email}}"}',
        }, headers=auth_header(admin_token))
        assert update.status_code == 200
        assert update.json()["recipient_item_template"] == '{"mail":"{{email}}"}'
```

Add to `unibridge-service/tests/test_alerts_router.py`:

```python
@pytest.mark.asyncio
async def test_get_and_update_alert_settings(client, admin_token):
    ch = await client.post(
        "/admin/alerts/channels",
        json={"name": "mail-settings", "webhook_url": WEBHOOK, "payload_template": TEMPLATE},
        headers=auth_header(admin_token),
    )
    resp = await client.put(
        "/admin/alerts/settings",
        json={
            "mail_channel_id": ch.json()["id"],
            "fallback_owner_group_id": None,
            "route_error_threshold_pct": 12.5,
            "check_interval_seconds": 90,
        },
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mail_channel_id"] == ch.json()["id"]
    assert body["route_error_threshold_pct"] == 12.5
    assert body["check_interval_seconds"] == 90

    get_resp = await client.get("/admin/alerts/settings", headers=auth_header(admin_token))
    assert get_resp.status_code == 200
    assert get_resp.json()["mail_channel_id"] == ch.json()["id"]
```

- [ ] **Step 2: Run tests to verify failure**

```bash
cd unibridge-service && pytest tests/test_alert_channels.py::TestAlertChannelsAPI::test_channel_round_trips_recipient_item_template tests/test_alerts_router.py::test_get_and_update_alert_settings -v
```

Expected: FAIL because schema fields and `/admin/alerts/settings` do not exist.

- [ ] **Step 3: Add schemas**

In `unibridge-service/app/schemas.py`, add `recipient_item_template` to existing channel schemas:

```python
class AlertChannelCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    webhook_url: str
    payload_template: str
    recipient_item_template: str | None = None
    headers: dict[str, str] | None = None
    enabled: bool = True
```

```python
class AlertChannelUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    webhook_url: str | None = None
    payload_template: str | None = None
    recipient_item_template: str | None = None
    headers: dict[str, str] | None = None
    enabled: bool | None = None
```

```python
class AlertChannelResponse(BaseModel):
    id: int
    name: str
    webhook_url: str
    payload_template: str
    recipient_item_template: str | None = None
    headers: dict[str, str] | None = None
    enabled: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None
```

Add settings schemas:

```python
class AlertSettingsResponse(BaseModel):
    mail_channel_id: int | None = None
    fallback_owner_group_id: int | None = None
    route_error_threshold_pct: float
    check_interval_seconds: int
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class AlertSettingsUpdate(BaseModel):
    mail_channel_id: int | None = None
    fallback_owner_group_id: int | None = None
    route_error_threshold_pct: float | None = Field(None, ge=0, le=100)
    check_interval_seconds: int | None = Field(None, ge=30, le=3600)
```

- [ ] **Step 4: Implement settings helpers and endpoints**

In `unibridge-service/app/routers/alerts.py`, import models and schemas:

```python
from app.models import AlertChannel, AlertHistory, AlertRule, AlertRuleChannel, AlertSettings
from app.schemas import AlertSettingsResponse, AlertSettingsUpdate
```

Add helper near router creation:

```python
async def _get_or_create_alert_settings(db: AsyncSession) -> AlertSettings:
    result = await db.execute(select(AlertSettings).where(AlertSettings.id == 1))
    settings = result.scalar_one_or_none()
    if settings is None:
        settings = AlertSettings(id=1, route_error_threshold_pct=10.0, check_interval_seconds=60)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return settings
```

Add endpoints before Channels section:

```python
@router.get("/settings", response_model=AlertSettingsResponse)
async def get_alert_settings(
    _user: CurrentUser = Depends(require_permission("alerts.read")),
    db: AsyncSession = Depends(get_db),
) -> AlertSettingsResponse:
    settings = await _get_or_create_alert_settings(db)
    return AlertSettingsResponse.model_validate(settings)


@router.put("/settings", response_model=AlertSettingsResponse)
async def update_alert_settings(
    body: AlertSettingsUpdate,
    _user: CurrentUser = Depends(require_permission("alerts.write")),
    db: AsyncSession = Depends(get_db),
) -> AlertSettingsResponse:
    settings = await _get_or_create_alert_settings(db)
    if body.mail_channel_id is not None:
        ch = await db.get(AlertChannel, body.mail_channel_id)
        if ch is None:
            raise HTTPException(status_code=422, detail="Mail channel not found")
        settings.mail_channel_id = body.mail_channel_id
    if body.fallback_owner_group_id is not None:
        from app.models import OwnerGroup
        group = await db.get(OwnerGroup, body.fallback_owner_group_id)
        if group is None:
            raise HTTPException(status_code=422, detail="Fallback owner group not found")
        settings.fallback_owner_group_id = body.fallback_owner_group_id
    if "mail_channel_id" in body.model_fields_set and body.mail_channel_id is None:
        settings.mail_channel_id = None
    if "fallback_owner_group_id" in body.model_fields_set and body.fallback_owner_group_id is None:
        settings.fallback_owner_group_id = None
    if body.route_error_threshold_pct is not None:
        settings.route_error_threshold_pct = body.route_error_threshold_pct
    if body.check_interval_seconds is not None:
        settings.check_interval_seconds = body.check_interval_seconds
    await db.commit()
    await db.refresh(settings)
    return AlertSettingsResponse.model_validate(settings)
```

Update `create_channel`, `update_channel`, and response builders to pass `recipient_item_template`.

- [ ] **Step 5: Run tests**

```bash
cd unibridge-service && pytest tests/test_alert_channels.py tests/test_alerts_router.py::test_get_and_update_alert_settings -v
```

Expected: tests PASS.

- [ ] **Step 6: Commit**

```bash
git add unibridge-service/app/schemas.py unibridge-service/app/routers/alerts.py unibridge-service/tests/test_alert_channels.py unibridge-service/tests/test_alerts_router.py
git commit -m "feat(alerts): add mail channel settings API"
```

---

## Task 4: Owner group API

**Files:**
- Modify: `unibridge-service/app/schemas.py`
- Modify: `unibridge-service/app/routers/alerts.py`
- Modify: `unibridge-service/tests/test_alerts_router.py`

- [ ] **Step 1: Failing tests**

Add to `unibridge-service/tests/test_alerts_router.py`:

```python
@pytest.mark.asyncio
async def test_owner_group_crud_deduplicates_emails(client, admin_token):
    create = await client.post(
        "/admin/alerts/owner-groups",
        json={
            "name": "Payments",
            "emails": ["pay@company.com", "pay@company.com", "kim@company.com"],
            "enabled": True,
        },
        headers=auth_header(admin_token),
    )
    assert create.status_code == 201, create.text
    group_id = create.json()["id"]
    assert create.json()["emails"] == ["pay@company.com", "kim@company.com"]

    update = await client.put(
        f"/admin/alerts/owner-groups/{group_id}",
        json={"emails": ["lee@company.com"], "enabled": False},
        headers=auth_header(admin_token),
    )
    assert update.status_code == 200
    assert update.json()["emails"] == ["lee@company.com"]
    assert update.json()["enabled"] is False

    listed = await client.get("/admin/alerts/owner-groups", headers=auth_header(admin_token))
    assert any(row["id"] == group_id for row in listed.json())

    delete = await client.delete(f"/admin/alerts/owner-groups/{group_id}", headers=auth_header(admin_token))
    assert delete.status_code == 204


@pytest.mark.asyncio
async def test_owner_group_rejects_empty_email_list(client, admin_token):
    resp = await client.post(
        "/admin/alerts/owner-groups",
        json={"name": "Empty", "emails": []},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 422
```

- [ ] **Step 2: Run tests to verify failure**

```bash
cd unibridge-service && pytest tests/test_alerts_router.py::test_owner_group_crud_deduplicates_emails tests/test_alerts_router.py::test_owner_group_rejects_empty_email_list -v
```

Expected: FAIL because endpoints do not exist.

- [ ] **Step 3: Add schemas**

In `unibridge-service/app/schemas.py`:

```python
def _dedupe_emails(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        email = raw.strip()
        if not email or email in seen:
            continue
        seen.add(email)
        result.append(email)
    return result


class OwnerGroupCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    emails: list[str] = Field(..., min_length=1)
    enabled: bool = True

    @field_validator("emails")
    @classmethod
    def normalize_emails(cls, value: list[str]) -> list[str]:
        emails = _dedupe_emails(value)
        if not emails:
            raise ValueError("At least one email is required")
        return emails


class OwnerGroupUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    emails: list[str] | None = None
    enabled: bool | None = None

    @field_validator("emails")
    @classmethod
    def normalize_emails(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        emails = _dedupe_emails(value)
        if not emails:
            raise ValueError("At least one email is required")
        return emails


class OwnerGroupResponse(BaseModel):
    id: int
    name: str
    emails: list[str]
    enabled: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None
```

- [ ] **Step 4: Implement endpoints**

In `unibridge-service/app/routers/alerts.py`, add imports:

```python
from app.models import OwnerGroup, ResourceOwner
from app.schemas import OwnerGroupCreate, OwnerGroupResponse, OwnerGroupUpdate
```

Add helper:

```python
def _owner_group_response(group: OwnerGroup) -> OwnerGroupResponse:
    return OwnerGroupResponse(
        id=group.id,
        name=group.name,
        emails=json.loads(group.emails),
        enabled=group.enabled,
        created_at=group.created_at,
        updated_at=group.updated_at,
    )
```

Add endpoints after Settings:

```python
@router.get("/owner-groups", response_model=list[OwnerGroupResponse])
async def list_owner_groups(
    _user: CurrentUser = Depends(require_permission("alerts.read")),
    db: AsyncSession = Depends(get_db),
) -> list[OwnerGroupResponse]:
    result = await db.execute(select(OwnerGroup).order_by(OwnerGroup.name))
    return [_owner_group_response(group) for group in result.scalars().all()]


@router.post("/owner-groups", response_model=OwnerGroupResponse, status_code=201)
async def create_owner_group(
    body: OwnerGroupCreate,
    _user: CurrentUser = Depends(require_permission("alerts.write")),
    db: AsyncSession = Depends(get_db),
) -> OwnerGroupResponse:
    group = OwnerGroup(name=body.name, emails=json.dumps(body.emails), enabled=body.enabled)
    db.add(group)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Owner group name '{body.name}' already exists")
    await db.refresh(group)
    return _owner_group_response(group)


@router.put("/owner-groups/{group_id}", response_model=OwnerGroupResponse)
async def update_owner_group(
    group_id: int,
    body: OwnerGroupUpdate,
    _user: CurrentUser = Depends(require_permission("alerts.write")),
    db: AsyncSession = Depends(get_db),
) -> OwnerGroupResponse:
    group = await db.get(OwnerGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Owner group not found")
    if body.name is not None:
        group.name = body.name
    if body.emails is not None:
        group.emails = json.dumps(body.emails)
    if body.enabled is not None:
        group.enabled = body.enabled
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Owner group name '{body.name}' already exists")
    await db.refresh(group)
    return _owner_group_response(group)


@router.delete("/owner-groups/{group_id}", status_code=204, response_model=None)
async def delete_owner_group(
    group_id: int,
    _user: CurrentUser = Depends(require_permission("alerts.write")),
    db: AsyncSession = Depends(get_db),
) -> None:
    group = await db.get(OwnerGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Owner group not found")
    owner_count = (await db.execute(select(ResourceOwner).where(ResourceOwner.owner_group_id == group_id))).scalars().first()
    settings = await _get_or_create_alert_settings(db)
    if owner_count is not None or settings.fallback_owner_group_id == group_id:
        raise HTTPException(status_code=409, detail="Owner group is in use")
    await db.delete(group)
    await db.commit()
```

- [ ] **Step 5: Run tests**

```bash
cd unibridge-service && pytest tests/test_alerts_router.py::test_owner_group_crud_deduplicates_emails tests/test_alerts_router.py::test_owner_group_rejects_empty_email_list -v
```

Expected: tests PASS.

- [ ] **Step 6: Commit**

```bash
git add unibridge-service/app/schemas.py unibridge-service/app/routers/alerts.py unibridge-service/tests/test_alerts_router.py
git commit -m "feat(alerts): add owner group API"
```

---

## Task 5: Resource owner API and resource validation

**Files:**
- Modify: `unibridge-service/app/schemas.py`
- Modify: `unibridge-service/app/routers/alerts.py`
- Modify: `unibridge-service/tests/test_alerts_router.py`

- [ ] **Step 1: Failing tests**

Add to `unibridge-service/tests/test_alerts_router.py`:

```python
@pytest.mark.asyncio
async def test_resource_owner_upsert_and_delete_for_db(client, admin_token, seeded_db):
    group = await client.post(
        "/admin/alerts/owner-groups",
        json={"name": "DB Team", "emails": ["db@company.com"]},
        headers=auth_header(admin_token),
    )
    group_id = group.json()["id"]

    from app.models import DBConnection
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add(DBConnection(
            alias="orders-db",
            db_type="postgres",
            host="localhost",
            port=5432,
            database="orders",
            username="u",
            password_encrypted="p",
        ))
        await db.commit()

    put = await client.put(
        "/admin/alerts/resource-owners/db/orders-db",
        json={"owner_group_id": group_id},
        headers=auth_header(admin_token),
    )
    assert put.status_code == 200, put.text
    assert put.json()["owner_group_id"] == group_id

    listed = await client.get("/admin/alerts/resource-owners", headers=auth_header(admin_token))
    row = next(item for item in listed.json() if item["resource_type"] == "db" and item["resource_id"] == "orders-db")
    assert row["owner_group_id"] == group_id
    assert row["owner_group_name"] == "DB Team"

    delete = await client.delete(
        "/admin/alerts/resource-owners/db/orders-db",
        headers=auth_header(admin_token),
    )
    assert delete.status_code == 204


@pytest.mark.asyncio
async def test_resource_owner_rejects_unknown_resource(client, admin_token):
    group = await client.post(
        "/admin/alerts/owner-groups",
        json={"name": "Unknown Team", "emails": ["unknown@company.com"]},
        headers=auth_header(admin_token),
    )
    resp = await client.put(
        "/admin/alerts/resource-owners/db/missing-db",
        json={"owner_group_id": group.json()["id"]},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 422
```

- [ ] **Step 2: Run tests to verify failure**

```bash
cd unibridge-service && pytest tests/test_alerts_router.py::test_resource_owner_upsert_and_delete_for_db tests/test_alerts_router.py::test_resource_owner_rejects_unknown_resource -v
```

Expected: FAIL because resource owner endpoints do not exist.

- [ ] **Step 3: Add schemas**

In `unibridge-service/app/schemas.py`:

```python
class ResourceOwnerUpsert(BaseModel):
    owner_group_id: int


class ResourceOwnerResponse(BaseModel):
    resource_type: str
    resource_id: str
    display_name: str
    owner_group_id: int | None = None
    owner_group_name: str | None = None
```

- [ ] **Step 4: Implement resource listing and validation**

In `unibridge-service/app/routers/alerts.py`, add imports:

```python
from app.models import DBConnection, S3Connection
from app.schemas import ResourceOwnerResponse, ResourceOwnerUpsert
```

Add helpers:

```python
RESOURCE_TYPES = {"db", "s3", "route", "upstream"}


async def _resource_exists(db: AsyncSession, resource_type: str, resource_id: str) -> bool:
    if resource_type == "db":
        result = await db.execute(select(DBConnection).where(DBConnection.alias == resource_id))
        return result.scalar_one_or_none() is not None
    if resource_type == "s3":
        result = await db.execute(select(S3Connection).where(S3Connection.alias == resource_id))
        return result.scalar_one_or_none() is not None
    from app.services import apisix_client
    resource_name = "routes" if resource_type == "route" else "upstreams"
    try:
        data = await apisix_client.list_resources(resource_name)
    except Exception as exc:
        logger.warning("Failed to validate APISIX %s resource %s: %s", resource_type, resource_id, exc)
        raise HTTPException(status_code=503, detail=f"Failed to load {resource_type} resources")
    return any(str(item.get("id")) == resource_id for item in data.get("items", []))


async def _list_resources_for_owners(db: AsyncSession) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    db_rows = (await db.execute(select(DBConnection).order_by(DBConnection.alias))).scalars().all()
    rows.extend(("db", conn.alias, conn.alias) for conn in db_rows)
    s3_rows = (await db.execute(select(S3Connection).order_by(S3Connection.alias))).scalars().all()
    rows.extend(("s3", conn.alias, conn.alias) for conn in s3_rows)
    from app.services import apisix_client
    for resource_type, resource_name in (("route", "routes"), ("upstream", "upstreams")):
        try:
            data = await apisix_client.list_resources(resource_name)
        except Exception as exc:
            logger.warning("Failed to list APISIX %s resources: %s", resource_type, exc)
            raise HTTPException(status_code=503, detail=f"Failed to load {resource_type} resources")
        for item in data.get("items", []):
            rid = str(item.get("id") or "")
            if not rid:
                continue
            display = str(item.get("name") or item.get("uri") or rid)
            rows.append((resource_type, rid, display))
    return rows
```

Add endpoints:

```python
@router.get("/resource-owners", response_model=list[ResourceOwnerResponse])
async def list_resource_owners(
    _user: CurrentUser = Depends(require_permission("alerts.read")),
    db: AsyncSession = Depends(get_db),
) -> list[ResourceOwnerResponse]:
    resources = await _list_resources_for_owners(db)
    owner_rows = (await db.execute(select(ResourceOwner))).scalars().all()
    owner_by_key = {(row.resource_type, row.resource_id): row for row in owner_rows}
    groups = (await db.execute(select(OwnerGroup))).scalars().all()
    group_name_by_id = {group.id: group.name for group in groups}
    return [
        ResourceOwnerResponse(
            resource_type=resource_type,
            resource_id=resource_id,
            display_name=display_name,
            owner_group_id=owner_by_key.get((resource_type, resource_id)).owner_group_id
            if owner_by_key.get((resource_type, resource_id)) else None,
            owner_group_name=group_name_by_id.get(owner_by_key[(resource_type, resource_id)].owner_group_id)
            if (resource_type, resource_id) in owner_by_key else None,
        )
        for resource_type, resource_id, display_name in resources
    ]


@router.put("/resource-owners/{resource_type}/{resource_id}", response_model=ResourceOwnerResponse)
async def upsert_resource_owner(
    resource_type: str,
    resource_id: str,
    body: ResourceOwnerUpsert,
    _user: CurrentUser = Depends(require_permission("alerts.write")),
    db: AsyncSession = Depends(get_db),
) -> ResourceOwnerResponse:
    if resource_type not in RESOURCE_TYPES:
        raise HTTPException(status_code=422, detail="Unsupported resource type")
    group = await db.get(OwnerGroup, body.owner_group_id)
    if group is None:
        raise HTTPException(status_code=422, detail="Owner group not found")
    if not await _resource_exists(db, resource_type, resource_id):
        raise HTTPException(status_code=422, detail="Resource not found")
    result = await db.execute(
        select(ResourceOwner).where(
            ResourceOwner.resource_type == resource_type,
            ResourceOwner.resource_id == resource_id,
        )
    )
    owner = result.scalar_one_or_none()
    if owner is None:
        owner = ResourceOwner(resource_type=resource_type, resource_id=resource_id)
        db.add(owner)
    owner.owner_group_id = body.owner_group_id
    await db.commit()
    return ResourceOwnerResponse(
        resource_type=resource_type,
        resource_id=resource_id,
        display_name=resource_id,
        owner_group_id=group.id,
        owner_group_name=group.name,
    )


@router.delete("/resource-owners/{resource_type}/{resource_id}", status_code=204, response_model=None)
async def delete_resource_owner(
    resource_type: str,
    resource_id: str,
    _user: CurrentUser = Depends(require_permission("alerts.write")),
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(
        select(ResourceOwner).where(
            ResourceOwner.resource_type == resource_type,
            ResourceOwner.resource_id == resource_id,
        )
    )
    owner = result.scalar_one_or_none()
    if owner is not None:
        await db.delete(owner)
        await db.commit()
```

- [ ] **Step 5: Run tests**

```bash
cd unibridge-service && pytest tests/test_alerts_router.py::test_resource_owner_upsert_and_delete_for_db tests/test_alerts_router.py::test_resource_owner_rejects_unknown_resource -v
```

Expected: tests PASS.

- [ ] **Step 6: Commit**

```bash
git add unibridge-service/app/schemas.py unibridge-service/app/routers/alerts.py unibridge-service/tests/test_alerts_router.py
git commit -m "feat(alerts): add resource owner API"
```

---

## Task 6: Owner-based alert dispatch service

**Files:**
- Create: `unibridge-service/app/services/alert_owner_dispatcher.py`
- Modify: `unibridge-service/tests/test_alert_owner_dispatcher.py`
- Modify: `unibridge-service/app/routers/alerts.py`

- [ ] **Step 1: Failing dispatcher tests**

Create `unibridge-service/tests/test_alert_owner_dispatcher.py`:

```python
"""Tests for owner-based alert dispatch."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AlertChannel, AlertSettings, OwnerGroup, ResourceOwner, AlertHistory
from app.services.alert_owner_dispatcher import dispatch_owner_alert


@pytest.mark.asyncio
async def test_dispatch_owner_alert_uses_resource_owner_group(seeded_db):
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = AlertChannel(
            name="mail",
            webhook_url="http://mail.example.com/send",
            payload_template='{"recipients":{{recipients_json}},"body":"{{message}}"}',
            recipient_item_template='{"emailAddress":"{{email}}","recipientType":"TO"}',
        )
        group = OwnerGroup(name="Payments", emails=json.dumps(["pay@company.com"]))
        db.add_all([channel, group])
        await db.flush()
        db.add(AlertSettings(id=1, mail_channel_id=channel.id, fallback_owner_group_id=None))
        db.add(ResourceOwner(resource_type="db", resource_id="payment-db", owner_group_id=group.id))
        await db.commit()

    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", AsyncMock(return_value=(True, None))) as send:
        await dispatch_owner_alert(
            resource_type="db",
            resource_id="payment-db",
            alert_type="triggered",
            target="payment-db",
            message="Database failed",
            rule_id=None,
            display_target="payment-db",
        )

    send.assert_awaited_once()
    payload = json.loads(send.await_args.kwargs["payload"])
    assert payload["recipients"] == [{"emailAddress": "pay@company.com", "recipientType": "TO"}]

    async with session_factory() as db:
        rows = (await db.execute(select(AlertHistory))).scalars().all()
    assert rows[0].resource_type == "db"
    assert json.loads(rows[0].recipients) == ["pay@company.com"]
    assert rows[0].success is True


@pytest.mark.asyncio
async def test_dispatch_owner_alert_records_failure_without_owner_or_fallback(seeded_db):
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = AlertChannel(
            name="mail-no-owner",
            webhook_url="http://mail.example.com/send",
            payload_template='{"recipients":{{recipients_json}}}',
            recipient_item_template='{"emailAddress":"{{email}}","recipientType":"TO"}',
        )
        db.add(channel)
        await db.flush()
        db.add(AlertSettings(id=1, mail_channel_id=channel.id, fallback_owner_group_id=None))
        await db.commit()

    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", AsyncMock()) as send:
        await dispatch_owner_alert(
            resource_type="db",
            resource_id="orphan-db",
            alert_type="triggered",
            target="orphan-db",
            message="Database failed",
            rule_id=None,
            display_target="orphan-db",
        )

    send.assert_not_called()
    async with session_factory() as db:
        rows = (await db.execute(select(AlertHistory))).scalars().all()
    assert rows[0].success is False
    assert "No owner group" in rows[0].error_detail
```

Also add missing import in the test file:

```python
from sqlalchemy import select
```

- [ ] **Step 2: Run tests to verify failure**

```bash
cd unibridge-service && pytest tests/test_alert_owner_dispatcher.py -v
```

Expected: FAIL because `alert_owner_dispatcher.py` does not exist.

- [ ] **Step 3: Implement dispatcher**

Create `unibridge-service/app/services/alert_owner_dispatcher.py`:

```python
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import async_session
from app.models import AlertChannel, AlertHistory, AlertSettings, OwnerGroup, ResourceOwner
from app.services.alert_sender import render_recipient_items, render_template, send_webhook

logger = logging.getLogger(__name__)


async def _load_settings(db) -> AlertSettings | None:
    result = await db.execute(select(AlertSettings).where(AlertSettings.id == 1))
    return result.scalar_one_or_none()


async def _resolve_owner_group(db, resource_type: str, resource_id: str, settings: AlertSettings) -> OwnerGroup | None:
    result = await db.execute(
        select(ResourceOwner).where(
            ResourceOwner.resource_type == resource_type,
            ResourceOwner.resource_id == resource_id,
        )
    )
    owner = result.scalar_one_or_none()
    group_id = owner.owner_group_id if owner is not None else settings.fallback_owner_group_id
    if group_id is None:
        return None
    group = await db.get(OwnerGroup, group_id)
    if group is None or not group.enabled:
        if owner is not None and settings.fallback_owner_group_id and settings.fallback_owner_group_id != group_id:
            fallback = await db.get(OwnerGroup, settings.fallback_owner_group_id)
            if fallback is not None and fallback.enabled:
                return fallback
        return None
    return group


async def _record_history(
    *,
    rule_id: int | None,
    channel_id: int | None,
    owner_group_id: int | None,
    resource_type: str,
    alert_type: str,
    target: str,
    message: str,
    recipients: list[str] | None,
    success: bool,
    error_detail: str | None,
) -> None:
    async with async_session() as db:
        db.add(AlertHistory(
            rule_id=rule_id,
            channel_id=channel_id,
            owner_group_id=owner_group_id,
            resource_type=resource_type,
            alert_type=alert_type,
            target=target,
            message=message,
            recipients=json.dumps(recipients) if recipients is not None else None,
            success=success,
            error_detail=error_detail,
        ))
        await db.commit()


async def dispatch_owner_alert(
    *,
    resource_type: str,
    resource_id: str,
    alert_type: str,
    target: str,
    message: str,
    rule_id: int | None = None,
    display_target: str | None = None,
    rate: float | None = None,
    threshold: float | None = None,
    rule_name: str = "",
) -> None:
    display = display_target or target
    async with async_session() as db:
        settings = await _load_settings(db)
        if settings is None or settings.mail_channel_id is None:
            await _record_history(
                rule_id=rule_id,
                channel_id=None,
                owner_group_id=None,
                resource_type=resource_type,
                alert_type=alert_type,
                target=target,
                message=message,
                recipients=None,
                success=False,
                error_detail="Mail channel not configured",
            )
            return
        channel = await db.get(AlertChannel, settings.mail_channel_id)
        group = await _resolve_owner_group(db, resource_type, resource_id, settings)

    if channel is None or not channel.enabled:
        await _record_history(
            rule_id=rule_id,
            channel_id=settings.mail_channel_id if settings else None,
            owner_group_id=None,
            resource_type=resource_type,
            alert_type=alert_type,
            target=target,
            message=message,
            recipients=None,
            success=False,
            error_detail="Mail channel disabled or missing",
        )
        return
    if group is None:
        await _record_history(
            rule_id=rule_id,
            channel_id=channel.id,
            owner_group_id=None,
            resource_type=resource_type,
            alert_type=alert_type,
            target=target,
            message=message,
            recipients=None,
            success=False,
            error_detail=f"No owner group configured for {resource_type}/{resource_id}",
        )
        return

    emails = json.loads(group.emails)
    try:
        recipients_json = render_recipient_items(channel.recipient_item_template or "{}", emails)
        payload = render_template(
            channel.payload_template,
            alert_type=alert_type,
            target_name=display,
            status="장애 발생" if alert_type == "triggered" else "정상 복구",
            message=message,
            timestamp=datetime.now(timezone.utc).isoformat(),
            recipients=", ".join(emails),
            recipients_json=recipients_json,
            rate=f"{rate:.1f}" if rate is not None else "",
            threshold=f"{threshold:.1f}" if threshold is not None else "",
            rule_name=rule_name,
        )
        headers = json.loads(channel.headers) if channel.headers else None
        ok, err = await send_webhook(url=channel.webhook_url, payload=payload, headers=headers)
    except Exception as exc:
        ok, err = False, str(exc)
    await _record_history(
        rule_id=rule_id,
        channel_id=channel.id,
        owner_group_id=group.id,
        resource_type=resource_type,
        alert_type=alert_type,
        target=target,
        message=message,
        recipients=emails,
        success=ok,
        error_detail=err,
    )
```

- [ ] **Step 4: Update channel test endpoint to exercise recipients_json**

In `unibridge-service/app/routers/alerts.py`, update `test_channel` payload build:

```python
from app.services.alert_sender import render_recipient_items, render_template, send_webhook

test_emails = ["test@example.com"]
try:
    recipients_json = render_recipient_items(
        ch.recipient_item_template or '{"email":"{{email}}"}',
        test_emails,
    )
except ValueError as exc:
    return {"success": False, "error": str(exc)}
payload = render_template(
    ch.payload_template,
    alert_type="test",
    target_name="test-target",
    status="ok",
    message="This is a test alert from UniBridge.",
    timestamp=now,
    recipients=", ".join(test_emails),
    recipients_json=recipients_json,
    rate="5.0",
    threshold="10.0",
    rule_name="test-rule",
)
```

- [ ] **Step 5: Run dispatcher tests**

```bash
cd unibridge-service && pytest tests/test_alert_owner_dispatcher.py tests/test_alert_sender.py -v
```

Expected: tests PASS.

- [ ] **Step 6: Commit**

```bash
git add unibridge-service/app/services/alert_owner_dispatcher.py unibridge-service/app/routers/alerts.py unibridge-service/tests/test_alert_owner_dispatcher.py
git commit -m "feat(alerts): dispatch alerts through owner groups"
```

---

## Task 7: Checker integration

**Files:**
- Modify: `unibridge-service/app/services/alert_checker.py`
- Modify: `unibridge-service/tests/test_alert_checker.py`
- Modify: `unibridge-service/tests/test_alert_rules.py`

- [ ] **Step 1: Failing checker tests**

In `unibridge-service/tests/test_alert_checker.py`, update `test_db_health_triggered` patch target:

```python
             patch("app.services.alert_checker.dispatch_owner_alert", new_callable=AsyncMock) as mock_dispatch:
```

Expected assertions:

```python
mock_dispatch.assert_called_once()
call_args = mock_dispatch.call_args
assert call_args[1]["alert_type"] == "triggered"
assert call_args[1]["resource_type"] == "db"
assert call_args[1]["resource_id"] == "mydb"
assert call_args[1]["target"] == "mydb"
```

Add a route-specific test:

```python
    @pytest.mark.asyncio
    async def test_route_error_rate_dispatches_owner_alert_with_rule_context(self):
        state = AlertStateManager()
        state.update("route_error_rate", "route-1:rule_77", is_healthy=True)
        rule = SimpleNamespace(id=77, name="route high 5xx", type="route_error_rate", target="route-1", threshold=10.0, enabled=True)

        class DbWithRouteRule:
            async def execute(self, _query):
                return _FakeResult([rule])

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock, return_value=[]), \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock, return_value=[]), \
             patch("app.services.alert_checker._check_error_rate", new_callable=AsyncMock, return_value=[]), \
             patch("app.services.alert_checker._check_route_error_rate", new_callable=AsyncMock, return_value=[("route-1", 15.0)]), \
             patch("app.services.alert_checker.async_session", return_value=_FakeSessionContext(DbWithRouteRule())), \
             patch("app.services.alert_checker.dispatch_owner_alert", new_callable=AsyncMock) as mock_dispatch:
            await run_single_check(state)

        kwargs = mock_dispatch.call_args.kwargs
        assert kwargs["resource_type"] == "route"
        assert kwargs["resource_id"] == "route-1"
        assert kwargs["rule_id"] == 77
        assert kwargs["rule_name"] == "route high 5xx"
        assert kwargs["rate"] == 15.0
        assert kwargs["threshold"] == 10.0
```

- [ ] **Step 2: Run tests to verify failure**

```bash
cd unibridge-service && pytest tests/test_alert_checker.py::TestAlertChecker::test_db_health_triggered tests/test_alert_checker.py::TestAlertChecker::test_route_error_rate_dispatches_owner_alert_with_rule_context -v
```

Expected: FAIL because checker still calls legacy `_dispatch_alert`.

- [ ] **Step 3: Integrate owner dispatcher**

In `unibridge-service/app/services/alert_checker.py`, import:

```python
from app.services.alert_owner_dispatcher import dispatch_owner_alert
```

Update DB transition block:

```python
await dispatch_owner_alert(
    resource_type="db",
    resource_id=alias,
    alert_type=transition,
    target=alias,
    message=msg,
    display_target=alias,
)
```

Update upstream transition block:

```python
await dispatch_owner_alert(
    resource_type="upstream",
    resource_id=uid,
    alert_type=transition,
    target=uid,
    message=msg,
    display_target=display,
)
```

Update `_evaluate_route_error_rule` transition block:

```python
await dispatch_owner_alert(
    resource_type="route",
    resource_id=route_id,
    alert_type=transition,
    target=route_id,
    message=msg,
    rule_id=rule.id,
    display_target=display,
    rate=rate,
    threshold=threshold,
    rule_name=rule.name,
)
```

Leave the global `error_rate` block calling legacy `_dispatch_alert` because it has no resource owner identity.

- [ ] **Step 4: Adjust rule create/update defaults for no recipient mappings**

In `unibridge-service/app/schemas.py`, keep `channels: list[RuleChannelMapping] = Field(default_factory=list)`. In frontend task, rule forms send `channels: []`.

In `unibridge-service/tests/test_alert_rules.py`, add:

```python
    @pytest.mark.asyncio
    async def test_create_rule_without_channels_is_supported_for_owner_routing(self, client, admin_token):
        resp = await client.post("/admin/alerts/rules", json={
            "name": "db-owner-rule",
            "type": "db_health",
            "target": "orders-db",
            "channels": [],
        }, headers=auth_header(admin_token))
        assert resp.status_code == 201
        assert resp.json()["channels"] == []
```

- [ ] **Step 5: Run checker and rules tests**

```bash
cd unibridge-service && pytest tests/test_alert_checker.py tests/test_alert_rules.py -v
```

Expected: tests PASS.

- [ ] **Step 6: Commit**

```bash
git add unibridge-service/app/services/alert_checker.py unibridge-service/tests/test_alert_checker.py unibridge-service/tests/test_alert_rules.py
git commit -m "feat(alerts): route checker notifications by resource owner"
```

---

## Task 8: Frontend API client and Alert Settings panels

**Files:**
- Modify: `unibridge-ui/src/api/client.ts`
- Create: `unibridge-ui/src/pages/alerts/AlertMailChannelPanel.tsx`
- Create: `unibridge-ui/src/pages/alerts/AlertOwnerGroupsPanel.tsx`
- Create: `unibridge-ui/src/pages/alerts/AlertResourceOwnersPanel.tsx`
- Create: `unibridge-ui/src/pages/alerts/AlertRulesPanel.tsx`
- Modify: `unibridge-ui/src/pages/AlertSettings.tsx`
- Modify: `unibridge-ui/src/pages/AlertSettings.css`
- Modify: `unibridge-ui/src/locales/en.json`
- Modify: `unibridge-ui/src/locales/ko.json`
- Modify: `unibridge-ui/src/test/AlertSettings.test.tsx`

- [ ] **Step 1: Failing UI tests**

Update the mock block in `unibridge-ui/src/test/AlertSettings.test.tsx`:

```ts
  getAlertSettings: vi.fn(),
  updateAlertSettings: vi.fn(),
  getAlertOwnerGroups: vi.fn(),
  createAlertOwnerGroup: vi.fn(),
  updateAlertOwnerGroup: vi.fn(),
  deleteAlertOwnerGroup: vi.fn(),
  getAlertResourceOwners: vi.fn(),
  setAlertResourceOwner: vi.fn(),
  deleteAlertResourceOwner: vi.fn(),
```

Add imports and mocks:

```ts
  getAlertSettings,
  updateAlertSettings,
  getAlertOwnerGroups,
  createAlertOwnerGroup,
  updateAlertOwnerGroup,
  deleteAlertOwnerGroup,
  getAlertResourceOwners,
  setAlertResourceOwner,
  deleteAlertResourceOwner,
```

```ts
  getSettings: vi.mocked(getAlertSettings),
  updateSettings: vi.mocked(updateAlertSettings),
  getOwnerGroups: vi.mocked(getAlertOwnerGroups),
  createOwnerGroup: vi.mocked(createAlertOwnerGroup),
  updateOwnerGroup: vi.mocked(updateAlertOwnerGroup),
  deleteOwnerGroup: vi.mocked(deleteAlertOwnerGroup),
  getResourceOwners: vi.mocked(getAlertResourceOwners),
  setResourceOwner: vi.mocked(setAlertResourceOwner),
  deleteResourceOwner: vi.mocked(deleteAlertResourceOwner),
```

In `beforeEach`, add:

```ts
mocks.getSettings.mockResolvedValue({
  mail_channel_id: null,
  fallback_owner_group_id: null,
  route_error_threshold_pct: 10,
  check_interval_seconds: 60,
});
mocks.getOwnerGroups.mockResolvedValue([]);
mocks.getResourceOwners.mockResolvedValue([]);
```

Add tests:

```ts
it('shows mail channel tab and saves selected channel', async () => {
  mocks.getChannels.mockResolvedValue([channelFixture]);
  mocks.updateSettings.mockResolvedValue({
    mail_channel_id: 1,
    fallback_owner_group_id: null,
    route_error_threshold_pct: 10,
    check_interval_seconds: 60,
  });
  renderWithProviders(<AlertSettings />);
  fireEvent.click(await screen.findByRole('button', { name: /Mail Channel|메일 채널/i }));
  await userEvent.selectOptions(screen.getByRole('combobox'), '1');
  fireEvent.click(screen.getByRole('button', { name: /^Save$|^저장$/i }));
  await waitFor(() => expect(mocks.updateSettings).toHaveBeenCalled());
  expect(mocks.updateSettings.mock.calls[0][0].mail_channel_id).toBe(1);
});

it('creates owner group with comma separated emails', async () => {
  mocks.createOwnerGroup.mockResolvedValue({
    id: 7,
    name: 'Payments',
    emails: ['pay@company.com', 'kim@company.com'],
    enabled: true,
  });
  renderWithProviders(<AlertSettings />);
  fireEvent.click(await screen.findByRole('button', { name: /Owner Groups|담당자 그룹/i }));
  fireEvent.click(screen.getByRole('button', { name: /\+\s*Owner Group|\+\s*담당자 그룹/i }));
  await userEvent.type(screen.getByLabelText(/Name|이름/i), 'Payments');
  await userEvent.type(screen.getByLabelText(/Emails|이메일/i), 'pay@company.com, kim@company.com');
  fireEvent.click(screen.getByRole('button', { name: /^Save$|^저장$/i }));
  await waitFor(() => expect(mocks.createOwnerGroup).toHaveBeenCalled());
  expect(mocks.createOwnerGroup.mock.calls[0][0].emails).toEqual(['pay@company.com', 'kim@company.com']);
});

it('assigns owner group to resource', async () => {
  mocks.getOwnerGroups.mockResolvedValue([{ id: 2, name: 'Ops', emails: ['ops@company.com'], enabled: true }]);
  mocks.getResourceOwners.mockResolvedValue([
    { resource_type: 'db', resource_id: 'orders-db', display_name: 'orders-db', owner_group_id: null, owner_group_name: null },
  ]);
  mocks.setResourceOwner.mockResolvedValue({
    resource_type: 'db',
    resource_id: 'orders-db',
    display_name: 'orders-db',
    owner_group_id: 2,
    owner_group_name: 'Ops',
  });
  renderWithProviders(<AlertSettings />);
  fireEvent.click(await screen.findByRole('button', { name: /Resource Owners|리소스 담당자/i }));
  await waitFor(() => expect(screen.getByText('orders-db')).toBeInTheDocument());
  await userEvent.selectOptions(screen.getByRole('combobox'), '2');
  await waitFor(() => expect(mocks.setResourceOwner).toHaveBeenCalledWith('db', 'orders-db', { owner_group_id: 2 }));
});
```

- [ ] **Step 2: Run UI tests to verify failure**

```bash
cd unibridge-ui && npx vitest run src/test/AlertSettings.test.tsx
```

Expected: FAIL because API functions and tabs do not exist.

- [ ] **Step 3: Add API client types/functions**

In `unibridge-ui/src/api/client.ts`, extend `AlertChannel` and `AlertChannelCreate`:

```ts
recipient_item_template?: string | null;
```

Add types:

```ts
export interface AlertSettings {
  mail_channel_id: number | null;
  fallback_owner_group_id: number | null;
  route_error_threshold_pct: number;
  check_interval_seconds: number;
  updated_at?: string;
}

export interface AlertOwnerGroup {
  id: number;
  name: string;
  emails: string[];
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface AlertOwnerGroupCreate {
  name: string;
  emails: string[];
  enabled?: boolean;
}

export interface AlertResourceOwner {
  resource_type: 'db' | 's3' | 'route' | 'upstream';
  resource_id: string;
  display_name: string;
  owner_group_id: number | null;
  owner_group_name: string | null;
}
```

Add functions:

```ts
export async function getAlertSettings(): Promise<AlertSettings> {
  const { data } = await client.get('/admin/alerts/settings');
  return data;
}

export async function updateAlertSettings(body: Partial<AlertSettings>): Promise<AlertSettings> {
  const { data } = await client.put('/admin/alerts/settings', body);
  return data;
}

export async function getAlertOwnerGroups(): Promise<AlertOwnerGroup[]> {
  const { data } = await client.get('/admin/alerts/owner-groups');
  return data;
}

export async function createAlertOwnerGroup(body: AlertOwnerGroupCreate): Promise<AlertOwnerGroup> {
  const { data } = await client.post('/admin/alerts/owner-groups', body);
  return data;
}

export async function updateAlertOwnerGroup(id: number, body: Partial<AlertOwnerGroupCreate>): Promise<AlertOwnerGroup> {
  const { data } = await client.put(`/admin/alerts/owner-groups/${id}`, body);
  return data;
}

export async function deleteAlertOwnerGroup(id: number): Promise<void> {
  await client.delete(`/admin/alerts/owner-groups/${id}`);
}

export async function getAlertResourceOwners(): Promise<AlertResourceOwner[]> {
  const { data } = await client.get('/admin/alerts/resource-owners');
  return data;
}

export async function setAlertResourceOwner(
  resourceType: AlertResourceOwner['resource_type'],
  resourceId: string,
  body: { owner_group_id: number },
): Promise<AlertResourceOwner> {
  const { data } = await client.put(`/admin/alerts/resource-owners/${resourceType}/${resourceId}`, body);
  return data;
}

export async function deleteAlertResourceOwner(
  resourceType: AlertResourceOwner['resource_type'],
  resourceId: string,
): Promise<void> {
  await client.delete(`/admin/alerts/resource-owners/${resourceType}/${resourceId}`);
}
```

- [ ] **Step 4: Split AlertSettings UI into panels**

Replace `unibridge-ui/src/pages/AlertSettings.tsx` with a tab shell:

```tsx
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import AlertMailChannelPanel from './alerts/AlertMailChannelPanel';
import AlertOwnerGroupsPanel from './alerts/AlertOwnerGroupsPanel';
import AlertResourceOwnersPanel from './alerts/AlertResourceOwnersPanel';
import AlertRulesPanel from './alerts/AlertRulesPanel';
import './AlertSettings.css';

type AlertTab = 'mail' | 'owners' | 'resources' | 'rules';

function AlertSettings() {
  const { t } = useTranslation();
  const [activeTab, setActiveTab] = useState<AlertTab>('mail');
  const tabs: Array<{ id: AlertTab; label: string }> = [
    { id: 'mail', label: t('alerts.mailChannelTab') },
    { id: 'owners', label: t('alerts.ownerGroupsTab') },
    { id: 'resources', label: t('alerts.resourceOwnersTab') },
    { id: 'rules', label: t('alerts.rulesTab') },
  ];

  return (
    <div className="alert-settings">
      <div className="page-header">
        <div>
          <h1>{t('alerts.settingsTitle')}</h1>
          <p className="page-subtitle">{t('alerts.settingsSubtitle')}</p>
        </div>
      </div>
      <div className="alert-tabs">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            className={`alert-tab${activeTab === tab.id ? ' alert-tab--active' : ''}`}
            onClick={() => setActiveTab(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>
      {activeTab === 'mail' && <AlertMailChannelPanel />}
      {activeTab === 'owners' && <AlertOwnerGroupsPanel />}
      {activeTab === 'resources' && <AlertResourceOwnersPanel />}
      {activeTab === 'rules' && <AlertRulesPanel />}
    </div>
  );
}

export default AlertSettings;
```

Implement each panel by moving existing channel/rule code into `AlertMailChannelPanel` and `AlertRulesPanel`, then adding focused owner group/resource owner panels with the API functions from Step 3. Keep visual style table-based and compact; no nested cards.

- [ ] **Step 5: Add i18n keys**

In both locale files under `"alerts"`, add keys. English:

```json
"mailChannelTab": "Mail Channel",
"ownerGroupsTab": "Owner Groups",
"resourceOwnersTab": "Resource Owners",
"defaultMailChannel": "Default Mail Channel",
"recipientItemTemplate": "Recipient Item Template",
"ownerGroup": "Owner Group",
"ownerGroups": "Owner Groups",
"addOwnerGroup": "+ Owner Group",
"editOwnerGroup": "Edit Owner Group",
"emails": "Emails",
"resourceType": "Resource Type",
"resourceId": "Resource ID",
"displayName": "Name",
"unassigned": "Unassigned",
"fallbackOwnerGroup": "Fallback Owner Group",
"legacyRecipients": "Legacy recipients",
"legacyRecipientsHint": "Legacy rule recipients are kept for compatibility and are not used by owner-based mail routing.",
"varDesc_recipients_json": "Recipient JSON array rendered from the owner group emails"
```

Korean:

```json
"mailChannelTab": "메일 채널",
"ownerGroupsTab": "담당자 그룹",
"resourceOwnersTab": "리소스 담당자",
"defaultMailChannel": "기본 메일 채널",
"recipientItemTemplate": "수신자 항목 템플릿",
"ownerGroup": "담당자 그룹",
"ownerGroups": "담당자 그룹",
"addOwnerGroup": "+ 담당자 그룹",
"editOwnerGroup": "담당자 그룹 수정",
"emails": "이메일",
"resourceType": "리소스 타입",
"resourceId": "리소스 ID",
"displayName": "이름",
"unassigned": "미지정",
"fallbackOwnerGroup": "기본 담당자 그룹",
"legacyRecipients": "기존 수신자",
"legacyRecipientsHint": "기존 룰 수신자는 호환 목적으로만 보관되며 담당자 기반 메일 라우팅에는 사용되지 않습니다.",
"varDesc_recipients_json": "담당자 그룹 이메일에서 생성한 수신자 JSON 배열"
```

- [ ] **Step 6: Run UI tests**

```bash
cd unibridge-ui && npx vitest run src/test/AlertSettings.test.tsx
```

Expected: tests PASS.

- [ ] **Step 7: Commit**

```bash
git add unibridge-ui/src/api/client.ts unibridge-ui/src/pages/AlertSettings.tsx unibridge-ui/src/pages/AlertSettings.css unibridge-ui/src/pages/alerts unibridge-ui/src/locales/en.json unibridge-ui/src/locales/ko.json unibridge-ui/src/test/AlertSettings.test.tsx
git commit -m "feat(ui): manage alert owner routing"
```

---

## Task 9: Full verification and cleanup

**Files:**
- Inspect: all files changed in Tasks 1-8
- Modify only if verification finds concrete failures

- [ ] **Step 1: Run backend alert-focused tests**

```bash
cd unibridge-service && pytest tests/test_alert_sender.py tests/test_alert_channels.py tests/test_alerts_router.py tests/test_alert_rules.py tests/test_alert_checker.py tests/test_alert_owner_dispatcher.py tests/test_database_schema.py -v
```

Expected: all selected tests PASS.

- [ ] **Step 2: Run frontend alert tests**

```bash
cd unibridge-ui && npx vitest run src/test/AlertSettings.test.tsx src/test/AlertHistory.test.tsx src/test/AlertStatus.test.tsx
```

Expected: all selected tests PASS.

- [ ] **Step 3: Run broader checks**

```bash
cd unibridge-service && pytest tests/ -v
```

Expected: full backend test suite PASS.

```bash
cd unibridge-ui && npm run build
```

Expected: TypeScript build and Vite build PASS.

- [ ] **Step 4: Review git diff for accidental scope creep**

```bash
git diff --stat HEAD
git diff -- unibridge-service/app/services/alert_checker.py unibridge-ui/src/pages/AlertSettings.tsx
```

Expected: changes are limited to owner routing, mail channel settings, and focused UI refactor.

- [ ] **Step 5: Final commit for verification-only fixes**

If Step 1-4 required fixes, commit them:

```bash
git add unibridge-service/app/models.py unibridge-service/app/database.py unibridge-service/alembic/versions/0004_alert_owner_routing.py unibridge-service/app/schemas.py unibridge-service/app/routers/alerts.py unibridge-service/app/services/alert_sender.py unibridge-service/app/services/alert_owner_dispatcher.py unibridge-service/app/services/alert_checker.py unibridge-service/tests/test_database_schema.py unibridge-service/tests/test_alert_sender.py unibridge-service/tests/test_alert_channels.py unibridge-service/tests/test_alerts_router.py unibridge-service/tests/test_alert_rules.py unibridge-service/tests/test_alert_checker.py unibridge-service/tests/test_alert_owner_dispatcher.py unibridge-ui/src/api/client.ts unibridge-ui/src/pages/AlertSettings.tsx unibridge-ui/src/pages/AlertSettings.css unibridge-ui/src/pages/alerts unibridge-ui/src/locales/en.json unibridge-ui/src/locales/ko.json unibridge-ui/src/test/AlertSettings.test.tsx
git commit -m "fix(alerts): stabilize owner routing integration"
```

If no fixes were required, do not create an empty commit.

---

## Self-Review

**Spec coverage:** Covered mail channel selection, recipient item template, owner groups, resource owner mapping, fallback group, channel test rendering, owner-based dispatch, history fields, UI tabs, and tests. S3 owner mapping is implemented in API/UI, but no S3 dispatch is added because the approved first change keeps existing alert check types. Global `error_rate` remains legacy because the spec identifies it as not resource-addressable in the first change.

**Placeholder scan:** This plan contains no reserved placeholder markers, "implement later", or unconstrained "add tests" steps. Every test step names exact files, commands, and expected outcomes.

**Type consistency:** Backend names are `OwnerGroup`, `ResourceOwner`, `AlertSettings`, `recipient_item_template`, `resource_type`, `owner_group_id`, `mail_channel_id`, and `fallback_owner_group_id`. Frontend names mirror API JSON as `snake_case` to match existing client style.
