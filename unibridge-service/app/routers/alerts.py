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
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Channel name '{body.name}' already exists")
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
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Channel name '{body.name}' already exists")
    await db.refresh(ch)
    return AlertChannelResponse(
        id=ch.id, name=ch.name, webhook_url=ch.webhook_url,
        payload_template=ch.payload_template,
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
    # Validate no duplicate channel mappings
    ch_ids = [ch_map.channel_id for ch_map in body.channels]
    if len(ch_ids) != len(set(ch_ids)):
        raise HTTPException(status_code=400, detail="Duplicate channel mappings are not allowed")
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
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Duplicate channel mapping conflict")
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
        ch_ids = [ch_map.channel_id for ch_map in body.channels]
        if len(ch_ids) != len(set(ch_ids)):
            raise HTTPException(status_code=400, detail="Duplicate channel mappings are not allowed")
        await db.execute(sa_delete(AlertRuleChannel).where(AlertRuleChannel.rule_id == rule.id))
        for ch_map in body.channels:
            db.add(AlertRuleChannel(
                rule_id=rule.id, channel_id=ch_map.channel_id,
                recipients=json.dumps(ch_map.recipients),
            ))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Duplicate channel mapping conflict")
    await db.refresh(rule)
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
    await db.delete(rule)
    await db.commit()


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
    alerts = _alert_state.get_all_alerts()
    return [
        AlertStatusResponse(target=a["target"], type=a["type"], status=a["status"], since=a["since"])
        for a in alerts
    ]
