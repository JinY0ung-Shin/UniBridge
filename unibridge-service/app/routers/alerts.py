from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, require_permission
from app.database import get_db
from app.models import (
    AlertChannel,
    AlertHistory,
    AlertRule,
    AlertRuleChannel,
    AlertSettings,
    DBConnection,
    OwnerGroup,
    ResourceOwner,
    S3Connection,
)
from app.schemas import (
    AlertChannelCreate, AlertChannelResponse, AlertChannelUpdate,
    AlertHistoryResponse,
    OwnerGroupCreate, OwnerGroupResponse, OwnerGroupUpdate,
    ResourceOwnerResponse, ResourceOwnerUpsert,
    AlertRuleCreate, AlertRuleResponse, AlertRuleTestChannelResult,
    AlertRuleTestResponse, AlertRuleUpdate, AlertStatusResponse,
    AlertSettingsResponse, AlertSettingsUpdate,
    RuleChannelDetail,
)
from app.services import apisix_client
from app.services.alert_sender import render_recipient_items, render_template, send_webhook
from app.services.alert_state import delete_alert_states_for_rule

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/alerts", tags=["Alerts"])

RESOURCE_TYPES = {"db", "s3", "route", "upstream"}
APISIX_RESOURCE_TYPES = {
    "route": "routes",
    "upstream": "upstreams",
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
            route_error_threshold_pct=10.0,
            check_interval_seconds=60,
        )
        db.add(settings)
        if commit:
            await db.commit()
            await db.refresh(settings)
        else:
            await db.flush()
    return settings


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


async def _resource_exists(db: AsyncSession, resource_type: str, resource_id: str) -> bool:
    _validate_resource_type(resource_type)
    if resource_type == "db":
        result = await db.execute(select(DBConnection).where(DBConnection.alias == resource_id))
        return result.scalar_one_or_none() is not None
    if resource_type == "s3":
        result = await db.execute(select(S3Connection).where(S3Connection.alias == resource_id))
        return result.scalar_one_or_none() is not None

    items = await _load_apisix_resources(resource_type)
    return any(str(item.get("id")) == resource_id for item in items if item.get("id") is not None)


async def _list_resources_for_owners(db: AsyncSession) -> list[ResourceOwnerResponse]:
    owner_result = await db.execute(
        select(ResourceOwner, OwnerGroup)
        .join(OwnerGroup, ResourceOwner.owner_group_id == OwnerGroup.id)
    )
    owners = {
        (owner.resource_type, owner.resource_id): group
        for owner, group in owner_result.all()
    }

    rows: list[ResourceOwnerResponse] = []

    db_result = await db.execute(select(DBConnection.alias).order_by(DBConnection.alias))
    for alias in db_result.scalars().all():
        group = owners.get(("db", alias))
        rows.append(ResourceOwnerResponse(
            resource_type="db",
            resource_id=alias,
            display_name=alias,
            owner_group_id=group.id if group else None,
            owner_group_name=group.name if group else None,
        ))

    s3_result = await db.execute(select(S3Connection.alias).order_by(S3Connection.alias))
    for alias in s3_result.scalars().all():
        group = owners.get(("s3", alias))
        rows.append(ResourceOwnerResponse(
            resource_type="s3",
            resource_id=alias,
            display_name=alias,
            owner_group_id=group.id if group else None,
            owner_group_name=group.name if group else None,
        ))

    for resource_type in ("route", "upstream"):
        for item in await _load_apisix_resources(resource_type):
            raw_id = item.get("id")
            if raw_id is None:
                continue
            resource_id = str(raw_id)
            display_name = str(item.get("name") or item.get("uri") or resource_id)
            group = owners.get((resource_type, resource_id))
            rows.append(ResourceOwnerResponse(
                resource_type=resource_type,
                resource_id=resource_id,
                display_name=display_name,
                owner_group_id=group.id if group else None,
                owner_group_name=group.name if group else None,
            ))

    return rows


# ── Settings ────────────────────────────────────────────────────────────────

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
    if body.mail_channel_id is not None:
        ch = await db.get(AlertChannel, body.mail_channel_id)
        if ch is None:
            raise HTTPException(status_code=422, detail="Mail channel not found")
    if body.fallback_owner_group_id is not None:
        from app.models import OwnerGroup

        group = await db.get(OwnerGroup, body.fallback_owner_group_id)
        if group is None:
            raise HTTPException(status_code=422, detail="Fallback owner group not found")

    settings = await _get_or_create_alert_settings(db, commit=False)
    if body.mail_channel_id is not None:
        settings.mail_channel_id = body.mail_channel_id
    if body.fallback_owner_group_id is not None:
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


# ── Owner Groups ────────────────────────────────────────────────────────────

def _build_owner_group_response(group: OwnerGroup) -> OwnerGroupResponse:
    return OwnerGroupResponse(
        id=group.id,
        name=group.name,
        emails=json.loads(group.emails),
        enabled=group.enabled,
        created_at=group.created_at,
        updated_at=group.updated_at,
    )


@router.get("/owner-groups", response_model=list[OwnerGroupResponse])
async def list_owner_groups(
    _user: CurrentUser = Depends(require_permission("alerts.read")),
    db: AsyncSession = Depends(get_db),
) -> list[OwnerGroupResponse]:
    result = await db.execute(select(OwnerGroup).order_by(OwnerGroup.name))
    groups = result.scalars().all()
    return [_build_owner_group_response(group) for group in groups]


@router.post("/owner-groups", response_model=OwnerGroupResponse, status_code=201)
async def create_owner_group(
    body: OwnerGroupCreate,
    _user: CurrentUser = Depends(require_permission("alerts.write")),
    db: AsyncSession = Depends(get_db),
) -> OwnerGroupResponse:
    group = OwnerGroup(
        name=body.name,
        emails=json.dumps(body.emails),
        enabled=body.enabled,
    )
    db.add(group)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Owner group name '{body.name}' already exists")
    await db.refresh(group)
    return _build_owner_group_response(group)


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
    return _build_owner_group_response(group)


@router.delete("/owner-groups/{group_id}", status_code=204, response_model=None)
async def delete_owner_group(
    group_id: int,
    _user: CurrentUser = Depends(require_permission("alerts.write")),
    db: AsyncSession = Depends(get_db),
) -> None:
    group = await db.get(OwnerGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Owner group not found")
    settings_result = await db.execute(
        select(AlertSettings).where(AlertSettings.fallback_owner_group_id == group_id)
    )
    if settings_result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Owner group is configured as the fallback owner group")
    resource_owner_result = await db.execute(
        select(ResourceOwner).where(ResourceOwner.owner_group_id == group_id)
    )
    if resource_owner_result.scalars().first() is not None:
        raise HTTPException(status_code=409, detail="Owner group is assigned to a resource owner")
    await db.delete(group)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Owner group is in use")


# ── Resource Owners ─────────────────────────────────────────────────────────

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

    group = await db.get(OwnerGroup, body.owner_group_id)
    if group is None:
        raise HTTPException(status_code=422, detail="Owner group not found")
    group_id = group.id
    group_name = group.name

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
        owner = ResourceOwner(
            resource_type=resource_type,
            resource_id=resource_id,
            owner_group_id=group_id,
        )
        db.add(owner)
    else:
        owner.owner_group_id = group_id
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
        owner.owner_group_id = group_id
        await db.commit()

    return ResourceOwnerResponse(
        resource_type=resource_type,
        resource_id=resource_id,
        display_name=resource_id,
        owner_group_id=group_id,
        owner_group_name=group_name,
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
            recipient_item_template=ch.recipient_item_template,
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
    headers = json.loads(ch.headers) if ch.headers else None
    ok, err = await send_webhook(url=ch.webhook_url, payload=payload, headers=headers)
    return {"success": ok, "error": err}


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
    old_type = rule.type
    old_target = rule.target
    old_enabled = rule.enabled
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
    clear_rule_state = (
        (body.type is not None and body.type != old_type)
        or (body.target is not None and body.target != old_target)
        or (body.enabled is False and old_enabled)
    )
    if clear_rule_state:
        await delete_alert_states_for_rule(db, rule.id)
    await db.commit()
    await db.refresh(rule)
    if clear_rule_state and _alert_state is not None:
        _alert_state.clear_rule_states(rule.id)
    return await _build_rule_response(db, rule)


@router.delete("/rules/{rule_id}", status_code=204, response_model=None)
async def delete_rule(
    rule_id: int,
    _user: CurrentUser = Depends(require_permission("alerts.write")),
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(select(AlertRule).where(AlertRule.id == rule_id))
    rule = result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    await delete_alert_states_for_rule(db, rule.id)
    await db.delete(rule)
    await db.commit()
    if _alert_state is not None:
        _alert_state.clear_rule_states(rule.id)


@router.post("/rules/{rule_id}/test", response_model=AlertRuleTestResponse)
async def test_rule(
    rule_id: int,
    _user: CurrentUser = Depends(require_permission("alerts.write")),
    db: AsyncSession = Depends(get_db),
) -> AlertRuleTestResponse:
    rule_result = await db.execute(select(AlertRule).where(AlertRule.id == rule_id))
    rule = rule_result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")

    mapping_result = await db.execute(
        select(AlertRuleChannel).where(AlertRuleChannel.rule_id == rule.id)
    )
    mappings = mapping_result.scalars().all()

    now = datetime.now(timezone.utc).isoformat()
    threshold_str = str(rule.threshold) if rule.threshold is not None else ""
    results: list[AlertRuleTestChannelResult] = []

    for mapping in mappings:
        recipients_list: list[str] = json.loads(mapping.recipients)
        ch_result = await db.execute(
            select(AlertChannel).where(AlertChannel.id == mapping.channel_id)
        )
        ch = ch_result.scalar_one_or_none()

        if ch is None:
            results.append(AlertRuleTestChannelResult(
                channel_id=mapping.channel_id,
                channel_name="deleted",
                recipients=recipients_list,
                skipped=True,
                success=None,
                error="channel deleted",
            ))
            continue
        if not ch.enabled:
            results.append(AlertRuleTestChannelResult(
                channel_id=ch.id,
                channel_name=ch.name,
                recipients=recipients_list,
                skipped=True,
                success=None,
                error="channel disabled",
            ))
            continue

        try:
            recipients_json = render_recipient_items(
                ch.recipient_item_template or '{"email":"{{email}}"}',
                recipients_list,
            )
        except ValueError as exc:
            results.append(AlertRuleTestChannelResult(
                channel_id=ch.id,
                channel_name=ch.name,
                recipients=recipients_list,
                skipped=True,
                success=None,
                error=str(exc),
            ))
            continue

        payload = render_template(
            ch.payload_template,
            alert_type="test",
            target_name=rule.target,
            status="테스트",
            message=f"[TEST] {rule.name} 규칙의 테스트 알림입니다.",
            timestamp=now,
            recipients=", ".join(recipients_list),
            recipients_json=recipients_json,
            rate=threshold_str,
            threshold=threshold_str,
            rule_name=rule.name,
        )
        headers = json.loads(ch.headers) if ch.headers else None
        ok, err = await send_webhook(url=ch.webhook_url, payload=payload, headers=headers)
        results.append(AlertRuleTestChannelResult(
            channel_id=ch.id,
            channel_name=ch.name,
            recipients=recipients_list,
            skipped=False,
            success=ok,
            error=err,
        ))

    return AlertRuleTestResponse(results=results)


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
            id=h.id, rule_id=h.rule_id, channel_id=h.channel_id,
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
