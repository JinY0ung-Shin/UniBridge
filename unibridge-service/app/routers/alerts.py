from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete as sa_delete, select
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
