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
    MonitoredHost,
    NASConnection,
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
from app.services.audit import log_admin_action

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


RESOURCE_TYPES = {"db", "s3", "nas", "route", "server"}
APISIX_RESOURCE_TYPES = {
    "route": "routes",
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


def _resource_owner_snapshot(owner: ResourceOwner | None) -> dict[str, Any] | None:
    if owner is None:
        return None
    return {
        "emails": _parse_emails(owner.emails),
        "alerts_enabled": owner.alerts_enabled,
    }


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
            route_error_min_requests=20,
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
        route_error_min_requests=settings.route_error_min_requests,
        check_interval_seconds=settings.check_interval_seconds,
        trigger_after_failures=settings.trigger_after_failures,
        server_disk_warn_pct=settings.server_disk_warn_pct,
        server_disk_crit_pct=settings.server_disk_crit_pct,
        server_cpu_warn_pct=settings.server_cpu_warn_pct,
        server_mem_warn_pct=settings.server_mem_warn_pct,
        server_disk_forecast_hours=settings.server_disk_forecast_hours,
        repeat_alert_after_cycles=settings.repeat_alert_after_cycles,
        updated_at=settings.updated_at,
    )


def _settings_audit_snapshot(settings: AlertSettings) -> dict[str, Any]:
    return {
        "mail_channel_id": settings.mail_channel_id,
        "admin_emails": _parse_emails(settings.admin_emails),
        "route_error_threshold_pct": settings.route_error_threshold_pct,
        "route_error_min_requests": settings.route_error_min_requests,
        "check_interval_seconds": settings.check_interval_seconds,
        "trigger_after_failures": settings.trigger_after_failures,
        "server_disk_warn_pct": settings.server_disk_warn_pct,
        "server_disk_crit_pct": settings.server_disk_crit_pct,
        "server_cpu_warn_pct": settings.server_cpu_warn_pct,
        "server_mem_warn_pct": settings.server_mem_warn_pct,
        "server_disk_forecast_hours": settings.server_disk_forecast_hours,
        "repeat_alert_after_cycles": settings.repeat_alert_after_cycles,
    }


def _validate_settings_disk_thresholds(settings: AlertSettings) -> None:
    if settings.server_disk_warn_pct > settings.server_disk_crit_pct:
        raise HTTPException(
            status_code=422,
            detail="server_disk_warn_pct must be less than or equal to server_disk_crit_pct",
        )


def _channel_audit_snapshot(ch: AlertChannel) -> dict[str, Any]:
    """Audit snapshot of an alert channel. Webhook URLs can embed tokens in
    their path (e.g. Slack/Teams hooks) and headers carry auth secrets, so the
    URL is reduced to scheme://host and every header value is masked."""
    try:
        headers = json.loads(ch.headers) if ch.headers else None
    except json.JSONDecodeError:
        headers = None
    return {
        "name": ch.name,
        "webhook_url": _mask_webhook_url(ch.webhook_url),
        "payload_template": ch.payload_template,
        "recipient_item_template": ch.recipient_item_template,
        "headers": {k: "***" for k in headers} if isinstance(headers, dict) else None,
        "enabled": ch.enabled,
    }


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
    if resource_type == "nas":
        result = await db.execute(select(NASConnection.alias).where(NASConnection.alias == resource_id))
        return result.scalar_one_or_none()
    if resource_type == "server":
        result = await db.execute(select(MonitoredHost.name).where(MonitoredHost.name == resource_id))
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
        (owner.resource_type, owner.resource_id): owner
        for owner in owner_result.scalars().all()
    }

    rows: list[ResourceOwnerResponse] = []

    db_result = await db.execute(select(DBConnection.alias).order_by(DBConnection.alias))
    for alias in db_result.scalars().all():
        owner = owners.get(("db", alias))
        rows.append(ResourceOwnerResponse(
            resource_type="db",
            resource_id=alias,
            display_name=alias,
            emails=_parse_emails(owner.emails) if owner is not None else [],
            alerts_enabled=owner.alerts_enabled if owner is not None else True,
        ))

    s3_result = await db.execute(select(S3Connection.alias).order_by(S3Connection.alias))
    for alias in s3_result.scalars().all():
        owner = owners.get(("s3", alias))
        rows.append(ResourceOwnerResponse(
            resource_type="s3",
            resource_id=alias,
            display_name=alias,
            emails=_parse_emails(owner.emails) if owner is not None else [],
            alerts_enabled=owner.alerts_enabled if owner is not None else True,
        ))

    nas_result = await db.execute(select(NASConnection.alias).order_by(NASConnection.alias))
    for alias in nas_result.scalars().all():
        owner = owners.get(("nas", alias))
        rows.append(ResourceOwnerResponse(
            resource_type="nas",
            resource_id=alias,
            display_name=alias,
            emails=_parse_emails(owner.emails) if owner is not None else [],
            alerts_enabled=owner.alerts_enabled if owner is not None else True,
        ))

    server_result = await db.execute(select(MonitoredHost.name).order_by(MonitoredHost.name))
    for name in server_result.scalars().all():
        owner = owners.get(("server", name))
        rows.append(ResourceOwnerResponse(
            resource_type="server",
            resource_id=name,
            display_name=name,
            emails=_parse_emails(owner.emails) if owner is not None else [],
            alerts_enabled=owner.alerts_enabled if owner is not None else True,
        ))

    for resource_type in ("route",):
        for item in await _load_apisix_resources(resource_type):
            raw_id = item.get("id")
            if raw_id is None:
                continue
            resource_id = str(raw_id)
            display_name = _apisix_resource_display_name(item, resource_id)
            owner = owners.get((resource_type, resource_id))
            rows.append(ResourceOwnerResponse(
                resource_type=resource_type,
                resource_id=resource_id,
                display_name=display_name,
                emails=_parse_emails(owner.emails) if owner is not None else [],
                alerts_enabled=owner.alerts_enabled if owner is not None else True,
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
    before_snapshot = _settings_audit_snapshot(settings)
    if "mail_channel_id" in body.model_fields_set:
        settings.mail_channel_id = body.mail_channel_id
    if body.admin_emails is not None:
        settings.admin_emails = json.dumps(body.admin_emails, ensure_ascii=False)
    if body.route_error_threshold_pct is not None:
        settings.route_error_threshold_pct = body.route_error_threshold_pct
    if body.route_error_min_requests is not None:
        settings.route_error_min_requests = body.route_error_min_requests
    if body.check_interval_seconds is not None:
        settings.check_interval_seconds = body.check_interval_seconds
    if body.trigger_after_failures is not None:
        settings.trigger_after_failures = body.trigger_after_failures
    if body.server_disk_warn_pct is not None:
        settings.server_disk_warn_pct = body.server_disk_warn_pct
    if body.server_disk_crit_pct is not None:
        settings.server_disk_crit_pct = body.server_disk_crit_pct
    if body.server_cpu_warn_pct is not None:
        settings.server_cpu_warn_pct = body.server_cpu_warn_pct
    if body.server_mem_warn_pct is not None:
        settings.server_mem_warn_pct = body.server_mem_warn_pct
    if body.server_disk_forecast_hours is not None:
        settings.server_disk_forecast_hours = body.server_disk_forecast_hours
    if body.repeat_alert_after_cycles is not None:
        settings.repeat_alert_after_cycles = body.repeat_alert_after_cycles
    _validate_settings_disk_thresholds(settings)
    await db.commit()
    await db.refresh(settings)

    await log_admin_action(
        db,
        actor=_user.username,
        action="update",
        resource_type="alert_settings",
        resource_id="global",
        summary=None,
        before=before_snapshot,
        after=_settings_audit_snapshot(settings),
    )
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

    result = await db.execute(
        select(ResourceOwner).where(
            ResourceOwner.resource_type == resource_type,
            ResourceOwner.resource_id == resource_id,
        )
    )
    owner = result.scalar_one_or_none()
    current_snapshot = _resource_owner_snapshot(owner)
    before_emails = current_snapshot["emails"] if current_snapshot is not None else None
    current_emails = before_emails if before_emails is not None else []
    current_alerts_enabled = owner.alerts_enabled if owner is not None else True
    emails = body.emails if body.emails is not None else current_emails
    alerts_enabled = body.alerts_enabled if body.alerts_enabled is not None else current_alerts_enabled

    if not emails and alerts_enabled:
        # Empty assignee list with notifications enabled is the default state,
        # so there is no row to persist.
        if owner is not None:
            await db.delete(owner)
            await db.commit()
            await log_admin_action(
                db,
                actor=_user.username,
                action="delete",
                resource_type="resource_owner",
                resource_id=f"{resource_type}/{resource_id}",
                summary=display_name,
                before=current_snapshot,
                after=None,
            )
        return ResourceOwnerResponse(
            resource_type=resource_type,
            resource_id=resource_id,
            display_name=display_name,
            emails=[],
            alerts_enabled=True,
        )

    emails_json = json.dumps(emails, ensure_ascii=False)
    if owner is None:
        owner = ResourceOwner(
            resource_type=resource_type,
            resource_id=resource_id,
            emails=emails_json,
            alerts_enabled=alerts_enabled,
        )
        db.add(owner)
    else:
        owner.emails = emails_json
        owner.alerts_enabled = alerts_enabled
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
        owner.alerts_enabled = alerts_enabled
        await db.commit()

    await log_admin_action(
        db,
        actor=_user.username,
        action="update" if current_snapshot is not None else "create",
        resource_type="resource_owner",
        resource_id=f"{resource_type}/{resource_id}",
        summary=display_name,
        before=current_snapshot,
        after={"emails": emails, "alerts_enabled": alerts_enabled},
    )
    return ResourceOwnerResponse(
        resource_type=resource_type,
        resource_id=resource_id,
        display_name=display_name,
        emails=emails,
        alerts_enabled=alerts_enabled,
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
    result = await db.execute(
        select(ResourceOwner).where(
            ResourceOwner.resource_type == resource_type,
            ResourceOwner.resource_id == resource_id,
        )
    )
    owner = result.scalar_one_or_none()
    before_snapshot = _resource_owner_snapshot(owner)
    await db.execute(
        sa_delete(ResourceOwner).where(
            ResourceOwner.resource_type == resource_type,
            ResourceOwner.resource_id == resource_id,
        )
    )
    await db.commit()

    if owner is not None:
        await log_admin_action(
            db,
            actor=_user.username,
            action="delete",
            resource_type="resource_owner",
            resource_id=f"{resource_type}/{resource_id}",
            summary=None,
            before=before_snapshot,
            after=None,
        )


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

    await log_admin_action(
        db,
        actor=_user.username,
        action="create",
        resource_type="alert_channel",
        resource_id=str(ch.id),
        summary=ch.name,
        before=None,
        after=_channel_audit_snapshot(ch),
    )
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
    before_snapshot = _channel_audit_snapshot(ch)
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

    await log_admin_action(
        db,
        actor=_user.username,
        action="update",
        resource_type="alert_channel",
        resource_id=str(ch.id),
        summary=ch.name,
        before=before_snapshot,
        after=_channel_audit_snapshot(ch),
    )
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
    before_snapshot = _channel_audit_snapshot(ch)
    try:
        await db.delete(ch)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Channel is still referenced")

    await log_admin_action(
        db,
        actor=_user.username,
        action="delete",
        resource_type="alert_channel",
        resource_id=str(channel_id),
        summary=before_snapshot["name"],
        before=before_snapshot,
        after=None,
    )


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
            alert_type=h.alert_type, target=h.target, display_target=h.display_target,
            severity=h.severity, message=h.message,
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


def get_alert_state():
    """Return the live AlertStateManager (or None before startup wiring)."""
    return _alert_state


@router.get("/status", response_model=list[AlertStatusResponse])
async def alert_status(
    _user: CurrentUser = Depends(require_permission("alerts.read")),
) -> list[AlertStatusResponse]:
    if _alert_state is None:
        return []
    alerts = _alert_state.get_all_statuses()
    return [
        AlertStatusResponse(
            target=a["target"], type=a["type"], status=a["status"],
            since=a["since"], severity=a.get("severity"),
        )
        for a in alerts
    ]
