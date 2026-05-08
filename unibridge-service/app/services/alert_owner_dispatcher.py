from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import metrics
from app.database import async_session
from app.models import (
    AlertChannel,
    AlertHistory,
    AlertSettings,
    OwnerGroup,
    ResourceOwner,
)
from app.services.alert_sender import render_recipient_items, render_template, send_webhook

logger = logging.getLogger(__name__)

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
    """Dispatch an alert to the resource owner group or configured fallback."""
    history = AlertHistory(
        rule_id=rule_id,
        channel_id=None,
        owner_group_id=None,
        resource_type=resource_type,
        alert_type=alert_type,
        target=target,
        message=message,
        recipients=None,
        success=False,
        error_detail=None,
    )

    send_args: dict[str, Any] | None = None
    try:
        async with async_session() as db:
            settings = await _load_settings(db)
            if settings is None or settings.mail_channel_id is None:
                history.error_detail = "Mail channel not configured"
                return

            history.channel_id = settings.mail_channel_id
            channel = await _load_enabled_mail_channel(db, settings.mail_channel_id)
            if channel is None:
                history.error_detail = "Mail channel missing or disabled"
                return

            group = await _resolve_owner_group(
                db,
                settings=settings,
                resource_type=resource_type,
                resource_id=resource_id,
            )
            if group is None:
                history.error_detail = "No owner group configured for resource"
                return

            history.owner_group_id = group.id
            channel_template = channel.payload_template
            channel_recipient_item_template = channel.recipient_item_template
            channel_headers = channel.headers
            channel_webhook_url = channel.webhook_url
            if channel_recipient_item_template is None or not channel_recipient_item_template.strip():
                history.error_detail = "recipient_item_template is required for owner mail channel"
                return
            emails = _parse_owner_emails(group.emails)
            history.recipients = json.dumps(emails, ensure_ascii=False)
            if not emails:
                history.error_detail = "Owner group has no recipient emails"
                return

        try:
            recipients_json = render_recipient_items(
                channel_recipient_item_template,
                emails,
            )
            payload = _render_payload(
                channel_template,
                alert_type=alert_type,
                display_target=display_target if display_target is not None else target,
                message=message,
                emails=emails,
                recipients_json=recipients_json,
                rate=rate,
                threshold=threshold,
                rule_name=rule_name,
            )
            send_args = {
                "url": channel_webhook_url,
                "payload": payload,
                "headers": _parse_headers(channel_headers),
            }
        except Exception as exc:
            history.error_detail = str(exc)
            return

        try:
            ok, err = await send_webhook(**send_args)
            history.success = ok
            history.error_detail = err
        except Exception as exc:
            logger.warning("Owner alert webhook dispatch failed: %s", exc)
            history.success = False
            history.error_detail = str(exc)
    except Exception as exc:
        logger.warning("Owner alert dispatch failed before webhook send: %s", exc)
        history.success = False
        history.error_detail = str(exc)
    finally:
        try:
            metrics.record_alert_dispatch(
                rule_id=history.rule_id if history.rule_id is not None else "owner",
                channel_type="webhook",
                status="success" if history.success else "failure",
            )
        except Exception as exc:
            logger.warning("Owner alert metric recording failed: %s", exc)
        await _record_history(history)


async def _record_history(history: AlertHistory) -> None:
    try:
        async with async_session() as db:
            db.add(history)
            await db.commit()
    except Exception as exc:
        logger.warning("Owner alert history recording failed: %s", exc)


async def _load_settings(db: AsyncSession) -> AlertSettings | None:
    result = await db.execute(select(AlertSettings).where(AlertSettings.id == 1))
    return result.scalar_one_or_none()


async def _load_enabled_mail_channel(
    db: AsyncSession,
    channel_id: int,
) -> AlertChannel | None:
    result = await db.execute(
        select(AlertChannel).where(
            AlertChannel.id == channel_id,
            AlertChannel.enabled.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def _resolve_owner_group(
    db: AsyncSession,
    *,
    settings: AlertSettings,
    resource_type: str,
    resource_id: str,
) -> OwnerGroup | None:
    resource_owner_result = await db.execute(
        select(ResourceOwner).where(
            ResourceOwner.resource_type == resource_type,
            ResourceOwner.resource_id == resource_id,
        )
    )
    resource_owner = resource_owner_result.scalar_one_or_none()
    if resource_owner is not None:
        group = await _load_group(db, resource_owner.owner_group_id)
        if group is not None and group.enabled:
            return group

    if settings.fallback_owner_group_id is not None:
        group = await _load_group(db, settings.fallback_owner_group_id)
        if group is not None and group.enabled:
            return group

    return None


async def _load_group(db: AsyncSession, group_id: int) -> OwnerGroup | None:
    result = await db.execute(select(OwnerGroup).where(OwnerGroup.id == group_id))
    return result.scalar_one_or_none()


def _parse_owner_emails(emails_json: str) -> list[str]:
    try:
        parsed: Any = json.loads(emails_json)
    except json.JSONDecodeError as exc:
        raise ValueError("Owner group emails must be valid JSON") from exc

    if not isinstance(parsed, list) or not all(isinstance(email, str) for email in parsed):
        raise ValueError("Owner group emails must be a JSON array of strings")
    return parsed


def _parse_headers(headers_json: str | None) -> dict[str, str] | None:
    if not headers_json:
        return None
    parsed: Any = json.loads(headers_json)
    if not isinstance(parsed, dict):
        raise ValueError("Alert channel headers must be a JSON object")
    return {str(key): str(value) for key, value in parsed.items()}


def _render_payload(
    payload_template: str,
    *,
    alert_type: str,
    display_target: str,
    message: str,
    emails: list[str],
    recipients_json: str,
    rate: float | None,
    threshold: float | None,
    rule_name: str,
) -> str:
    status_label = "장애 발생" if alert_type == "triggered" else "정상 복구"
    return render_template(
        payload_template,
        alert_type=alert_type,
        target_name=display_target,
        status=status_label,
        message=message,
        timestamp=datetime.now(timezone.utc).isoformat(),
        recipients=", ".join(emails),
        recipients_json=recipients_json,
        rate=f"{rate:.1f}" if rate is not None else "",
        threshold=f"{threshold:.1f}" if threshold is not None else "",
        rule_name=rule_name,
    )
