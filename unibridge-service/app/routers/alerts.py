from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from urllib.parse import urlparse

from app.auth import CurrentUser, get_role_permissions, require_permission
from app.database import get_db
from app.models import (
    AlertChannel,
    AlertHistory,
    AlertSettings,
    DBConnection,
    ResourceOwner,
    S3Connection,
)
from app.schemas import (
    AlertChannelCreate, AlertChannelResponse, AlertChannelUpdate,
    AlertDeliveryTestResponse,
    AlertHistoryResponse,
    RecipientTestRequest,
    ResourceOwnerResponse, ResourceOwnerUpsert,
    AlertStatusResponse,
    AlertSettingsResponse, AlertSettingsUpdate,
)
from app.services import apisix_client
from app.services.alert_sender import render_recipient_items, render_template, send_webhook

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/alerts", tags=["Alerts"])


def _mask_webhook_url(url: str) -> str:
    """Reconstruct from hostname/port only so userinfo, path, query, and fragment never leak to non-writers."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        return "***"
    host = parsed.hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme}://{host}/***"


RESOURCE_TYPES = {"db", "s3", "route", "upstream"}
APISIX_RESOURCE_TYPES = {
    "route": "routes",
    "upstream": "upstreams",
}


def _parse_emails(emails_json: str | None) -> list[str]:
    if not emails_json:
        return []
    try:
        parsed = json.loads(emails_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [e for e in parsed if isinstance(e, str)]


async def _get_or_create_alert_settings(
    db: AsyncSession,
    *,
    commit: bool = True,
) -> AlertSettings:
    result = await db.execute(select(AlertSettings).where(AlertSettings.id == 1))
    settings = result.scalar_one_or_none()
    if settings is None:
        settings = AlertSettings(
            id=1,
            admin_emails="[]",
            route_error_threshold_pct=10.0,
            check_interval_seconds=60,
            trigger_after_failures=2,
        )
        db.add(settings)
        try:
            if commit:
                await db.commit()
                await db.refresh(settings)
            else:
                await db.flush()
        except IntegrityError:
            await db.rollback()
            result = await db.execute(select(AlertSettings).where(AlertSettings.id == 1))
            settings = result.scalar_one_or_none()
            if settings is None:
                raise
    return settings


def _build_settings_response(settings: AlertSettings) -> AlertSettingsResponse:
    return AlertSettingsResponse(
        mail_channel_id=settings.mail_channel_id,
        admin_emails=_parse_emails(settings.admin_emails),
        route_error_threshold_pct=settings.route_error_threshold_pct,
        check_interval_seconds=settings.check_interval_seconds,
        trigger_after_failures=settings.trigger_after_failures,
        updated_at=settings.updated_at,
    )


def _validate_resource_type(resource_type: str) -> None:
    if resource_type not in RESOURCE_TYPES:
        raise HTTPException(status_code=422, detail="Unsupported resource type")


async def _load_apisix_resources(resource_type: str) -> list[dict[str, Any]]:
    apisix_type = APISIX_RESOURCE_TYPES[resource_type]
    try:
        result = await apisix_client.list_resources(apisix_type)
    except Exception as exc:
        logger.exception("Failed to load APISIX %s resources", apisix_type)
        raise HTTPException(
            status_code=503,
            detail=f"Failed to load {apisix_type} resources",
        ) from exc
    return result.get("items", [])


def _apisix_resource_display_name(item: dict[str, Any], fallback: str) -> str:
    return str(item.get("name") or item.get("uri") or fallback)


async def _resource_display_name(
    db: AsyncSession,
    resource_type: str,
    resource_id: str,
) -> str | None:
    _validate_resource_type(resource_type)
    if resource_type == "db":
        result = await db.execute(select(DBConnection.alias).where(DBConnection.alias == resource_id))
        return result.scalar_one_or_none()
    if resource_type == "s3":
        result = await db.execute(select(S3Connection.alias).where(S3Connection.alias == resource_id))
        return result.scalar_one_or_none()

    items = await _load_apisix_resources(resource_type)
    for item in items:
        raw_id = item.get("id")
        if raw_id is not None and str(raw_id) == resource_id:
            return _apisix_resource_display_name(item, resource_id)
    return None


async def _list_resources_for_owners(db: AsyncSession) -> list[ResourceOwnerResponse]:
    owner_result = await db.execute(select(ResourceOwner))
    owners = {
        (owner.resource_type, owner.resource_id): _parse_emails(owner.emails)
        for owner in owner_result.scalars().all()
    }

    rows: list[ResourceOwnerResponse] = []

    db_result = await db.execute(select(DBConnection.alias).order_by(DBConnection.alias))
    for alias in db_result.scalars().all():
        rows.append(ResourceOwnerResponse(
            resource_type="db",
            resource_id=alias,
            display_name=alias,
            emails=owners.get(("db", alias), []),
        ))

    s3_result = await db.execute(select(S3Connection.alias).order_by(S3Connection.alias))
    for alias in s3_result.scalars().all():
        rows.append(ResourceOwnerResponse(
            resource_type="s3",
            resource_id=alias,
            display_name=alias,
            emails=owners.get(("s3", alias), []),
        ))

    for resource_type in ("route", "upstream"):
        for item in await _load_apisix_resources(resource_type):
            raw_id = item.get("id")
            if raw_id is None:
                continue
            resource_id = str(raw_id)
            display_name = _apisix_resource_display_name(item, resource_id)
            rows.append(ResourceOwnerResponse(
                resource_type=resource_type,
                resource_id=resource_id,
                display_name=display_name,
                emails=owners.get((resource_type, resource_id), []),
            ))

    return rows


# ── Settings ────────────────────────────────────────────────────────────────

@router.get("/settings", response_model=AlertSettingsResponse)
async def get_alert_settings(
    _user: CurrentUser = Depends(require_permission("alerts.read")),
    db: AsyncSession = Depends(get_db),
) -> AlertSettingsResponse:
    settings = await _get_or_create_alert_settings(db)
    return _build_settings_response(settings)


@router.put("/settings", response_model=AlertSettingsResponse)
async def update_alert_settings(
    body: AlertSettingsUpdate,
    _user: CurrentUser = Depends(require_permission("alerts.write")),
    db: AsyncSession = Depends(get_db),
) -> AlertSettingsResponse:
    if body.mail_channel_id is not None:
        ch = await db.get(AlertChannel, body.mail_channel_id)
        if ch is None:
            raise HTTPException(status_code=422, detail="Mail channel not found")

    settings = await _get_or_create_alert_settings(db, commit=False)
    if "mail_channel_id" in body.model_fields_set:
        settings.mail_channel_id = body.mail_channel_id
    if body.admin_emails is not None:
        settings.admin_emails = json.dumps(body.admin_emails, ensure_ascii=False)
    if body.route_error_threshold_pct is not None:
        settings.route_error_threshold_pct = body.route_error_threshold_pct
    if body.check_interval_seconds is not None:
        settings.check_interval_seconds = body.check_interval_seconds
    if body.trigger_after_failures is not None:
        settings.trigger_after_failures = body.trigger_after_failures
    await db.commit()
    await db.refresh(settings)
    return _build_settings_response(settings)


@router.post("/settings/recipients/test", response_model=AlertDeliveryTestResponse)
async def test_recipient_delivery(
    body: RecipientTestRequest,
    _user: CurrentUser = Depends(require_permission("alerts.write")),
    db: AsyncSession = Depends(get_db),
) -> AlertDeliveryTestResponse:
    """Send a test alert to an explicit set of emails (e.g. admins or a resource's assignees)."""
    ch = await db.get(AlertChannel, body.mail_channel_id)
    if ch is None:
        return AlertDeliveryTestResponse(success=False, error="Mail channel not found")
    if not ch.enabled:
        return AlertDeliveryTestResponse(success=False, error="Mail channel disabled")

    emails = body.emails
    try:
        recipients_json = _render_channel_recipients_json(ch, emails, require_template=True)
        payload = render_template(
            ch.payload_template,
            alert_type="test",
            target_name="recipient-test",
            status="테스트",
            message="[TEST] UniBridge recipient delivery test.",
            timestamp=datetime.now(timezone.utc).isoformat(),
            recipients=", ".join(emails),
            recipients_json=recipients_json,
            rate="",
            threshold="",
            rule_name="recipient-test",
        )
        headers = json.loads(ch.headers) if ch.headers else None
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return AlertDeliveryTestResponse(success=False, error=str(exc))

    ok, err = await send_webhook(url=ch.webhook_url, payload=payload, headers=headers)
    return AlertDeliveryTestResponse(success=ok, error=err)


# ── Resource Owners (담당자) ──────────────────────────────────────────────────

@router.get("/resource-owners", response_model=list[ResourceOwnerResponse])
async def list_resource_owners(
    _user: CurrentUser = Depends(require_permission("alerts.read")),
    db: AsyncSession = Depends(get_db),
) -> list[ResourceOwnerResponse]:
    return await _list_resources_for_owners(db)


@router.put(
    "/resource-owners/{resource_type}/{resource_id}",
    response_model=ResourceOwnerResponse,
)
async def upsert_resource_owner(
    resource_type: str,
    resource_id: str,
    body: ResourceOwnerUpsert,
    _user: CurrentUser = Depends(require_permission("alerts.write")),
    db: AsyncSession = Depends(get_db),
) -> ResourceOwnerResponse:
    _validate_resource_type(resource_type)

    display_name = await _resource_display_name(db, resource_type, resource_id)
    if display_name is None:
        raise HTTPException(status_code=422, detail="Resource not found")

    emails = body.emails
    result = await db.execute(
        select(ResourceOwner).where(
            ResourceOwner.resource_type == resource_type,
            ResourceOwner.resource_id == resource_id,
        )
    )
    owner = result.scalar_one_or_none()

    if not emails:
        # Empty assignee list clears the resource owner row entirely.
        if owner is not None:
            await db.delete(owner)
            await db.commit()
        return ResourceOwnerResponse(
            resource_type=resource_type,
            resource_id=resource_id,
            display_name=display_name,
            emails=[],
        )

    emails_json = json.dumps(emails, ensure_ascii=False)
    if owner is None:
        owner = ResourceOwner(
            resource_type=resource_type,
            resource_id=resource_id,
            emails=emails_json,
        )
        db.add(owner)
    else:
        owner.emails = emails_json
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        result = await db.execute(
            select(ResourceOwner).where(
                ResourceOwner.resource_type == resource_type,
                ResourceOwner.resource_id == resource_id,
            )
        )
        owner = result.scalar_one_or_none()
        if owner is None:
            raise HTTPException(status_code=409, detail="Resource owner conflict")
        owner.emails = emails_json
        await db.commit()

    return ResourceOwnerResponse(
        resource_type=resource_type,
        resource_id=resource_id,
        display_name=display_name,
        emails=emails,
    )


@router.delete(
    "/resource-owners/{resource_type}/{resource_id}",
    status_code=204,
    response_model=None,
)
async def delete_resource_owner(
    resource_type: str,
    resource_id: str,
    _user: CurrentUser = Depends(require_permission("alerts.write")),
    db: AsyncSession = Depends(get_db),
) -> None:
    _validate_resource_type(resource_type)
    await db.execute(
        sa_delete(ResourceOwner).where(
            ResourceOwner.resource_type == resource_type,
            ResourceOwner.resource_id == resource_id,
        )
    )
    await db.commit()


# ── Channels ────────────────────────────────────────────────────────────────

@router.get("/channels", response_model=list[AlertChannelResponse])
async def list_channels(
    user: CurrentUser = Depends(require_permission("alerts.read")),
    db: AsyncSession = Depends(get_db),
) -> list[AlertChannelResponse]:
    user_perms = await get_role_permissions(db, user.role)
    can_write = "alerts.write" in user_perms
    result = await db.execute(select(AlertChannel).order_by(AlertChannel.id))
    channels = result.scalars().all()
    rows = []
    for ch in channels:
        webhook_url = ch.webhook_url if can_write else _mask_webhook_url(ch.webhook_url)
        headers = (json.loads(ch.headers) if ch.headers else None) if can_write else None
        rows.append(AlertChannelResponse(
            id=ch.id, name=ch.name, webhook_url=webhook_url,
            payload_template=ch.payload_template,
            recipient_item_template=ch.recipient_item_template,
            headers=headers,
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
        recipient_item_template=body.recipient_item_template,
        headers=json.dumps(body.headers) if body.headers else None,
        enabled=body.enabled,
    )
    db.add(ch)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Channel name '{body.name}' already exists")
    await db.refresh(ch)
    return AlertChannelResponse(
        id=ch.id, name=ch.name, webhook_url=ch.webhook_url,
        payload_template=ch.payload_template,
        recipient_item_template=ch.recipient_item_template,
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
    if "recipient_item_template" in body.model_fields_set:
        ch.recipient_item_template = body.recipient_item_template
    if body.headers is not None:
        ch.headers = json.dumps(body.headers)
    if body.enabled is not None:
        ch.enabled = body.enabled
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Channel name '{body.name}' already exists")
    await db.refresh(ch)
    return AlertChannelResponse(
        id=ch.id, name=ch.name, webhook_url=ch.webhook_url,
        payload_template=ch.payload_template,
        recipient_item_template=ch.recipient_item_template,
        headers=json.loads(ch.headers) if ch.headers else None,
        enabled=ch.enabled, created_at=ch.created_at, updated_at=ch.updated_at,
    )


@router.delete("/channels/{channel_id}", status_code=204, response_model=None)
async def delete_channel(
    channel_id: int,
    _user: CurrentUser = Depends(require_permission("alerts.write")),
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(select(AlertChannel).where(AlertChannel.id == channel_id))
    ch = result.scalar_one_or_none()
    if ch is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    settings_result = await db.execute(
        select(AlertSettings).where(AlertSettings.mail_channel_id == channel_id)
    )
    if settings_result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail="Channel is configured as the default mail channel",
        )
    try:
        await db.delete(ch)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Channel is still referenced")


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
    test_emails = ["test@example.com"]
    settings_result = await db.execute(
        select(AlertSettings).where(AlertSettings.mail_channel_id == ch.id)
    )
    is_mail_channel = settings_result.scalar_one_or_none() is not None
    try:
        recipients_json = _render_channel_recipients_json(
            ch,
            test_emails,
            require_template=is_mail_channel,
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
    headers = json.loads(ch.headers) if ch.headers else None
    ok, err = await send_webhook(url=ch.webhook_url, payload=payload, headers=headers)
    return {"success": ok, "error": err}


def _render_channel_recipients_json(
    ch: AlertChannel,
    emails: list[str],
    *,
    require_template: bool = False,
) -> str:
    template = ch.recipient_item_template
    uses_recipients_json = "{{recipients_json}}" in ch.payload_template
    if template is None or not template.strip():
        if require_template or uses_recipients_json:
            raise ValueError("recipient_item_template is required when using recipients_json")
        return "[]"
    return render_recipient_items(template, emails)


# ── History ─────────────────────────────────────────────────────────────────

@router.get("/history", response_model=list[AlertHistoryResponse])
async def list_history(
    alert_type: str | None = Query(None),
    target: str | None = Query(None),
    from_date: datetime | None = Query(None),
    to_date: datetime | None = Query(None),
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
            id=h.id, channel_id=h.channel_id,
            alert_type=h.alert_type, target=h.target, message=h.message,
            recipients=json.loads(h.recipients) if h.recipients else None,
            sent_at=h.sent_at, success=h.success, error_detail=h.error_detail,
        )
        for h in rows
    ]


# ── Status ──────────────────────────────────────────────────────────────────

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
    alerts = _alert_state.get_all_statuses()
    return [
        AlertStatusResponse(target=a["target"], type=a["type"], status=a["status"], since=a["since"])
        for a in alerts
    ]
