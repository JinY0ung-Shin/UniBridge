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
    ResourceOwner,
)
from app.services.alert_sender import render_recipient_items, render_template, send_webhook

logger = logging.getLogger(__name__)

ASSIGNEE_RESOURCE_TYPES = {"db", "s3", "nas", "route", "server", "service"}


async def dispatch_alert(
    *,
    resource_type: str,
    resource_id: str,
    alert_type: str,
    target: str,
    message: str,
    display_target: str | None = None,
    rate: float | None = None,
    threshold: float | None = None,
    monitor_label: str = "",
    severity: str | None = None,
) -> None:
    """Send an alert to the resource's assignees (담당자) plus the global admins (관리자).

    Recipients are the union of supported resource assignee emails and the
    global admin emails. Admins receive every alert; a resource with no
    assignees still notifies the admins. With no recipients at all, nothing is
    sent. Upstream alerts intentionally skip assignee routing and notify only
    admins because upstreams are route internals in the UI.
    """
    history = AlertHistory(
        channel_id=None,
        resource_type=resource_type,
        alert_type=alert_type,
        target=target,
        display_target=display_target,
        severity=severity,
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

            channel_template = channel.payload_template
            channel_recipient_item_template = channel.recipient_item_template
            channel_headers = channel.headers
            channel_webhook_url = channel.webhook_url
            if channel_recipient_item_template is None or not channel_recipient_item_template.strip():
                history.error_detail = "recipient_item_template is required for mail channel"
                return

            alerts_enabled, emails = await _resolve_recipients(
                db,
                resource_type=resource_type,
                resource_id=resource_id,
                admin_emails_json=settings.admin_emails,
            )
            if not alerts_enabled:
                history.success = None
                history.error_detail = "Alerts disabled for resource"
                return
            history.recipients = json.dumps(emails, ensure_ascii=False)
            if not emails:
                history.error_detail = "No assignees or admins configured for resource"
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
                monitor_label=monitor_label,
                severity=severity,
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
            logger.warning("Alert webhook dispatch failed: %s", exc)
            history.success = False
            history.error_detail = str(exc)
    except Exception as exc:
        logger.warning("Alert dispatch failed before webhook send: %s", exc)
        history.success = False
        history.error_detail = str(exc)
    finally:
        try:
            metrics.record_alert_dispatch(
                rule_id=resource_type or "alert",
                channel_type="webhook",
                status="success" if history.success else "skipped" if history.success is None else "failure",
            )
        except Exception as exc:
            logger.warning("Alert metric recording failed: %s", exc)
        await _record_history(history)


async def _record_history(history: AlertHistory) -> None:
    try:
        async with async_session() as db:
            db.add(history)
            await db.commit()
    except Exception as exc:
        logger.warning("Alert history recording failed: %s", exc)


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


async def _resolve_recipients(
    db: AsyncSession,
    *,
    resource_type: str,
    resource_id: str,
    admin_emails_json: str | None,
) -> tuple[bool, list[str]]:
    """Union of the resource's assignee emails and the global admin emails.

    Assignees come first, then admins, with duplicates removed (case-preserving,
    first occurrence wins).
    """
    assignees: list[str] = []
    if resource_type in ASSIGNEE_RESOURCE_TYPES:
        result = await db.execute(
            select(ResourceOwner.emails, ResourceOwner.alerts_enabled).where(
                ResourceOwner.resource_type == resource_type,
                ResourceOwner.resource_id == resource_id,
            )
        )
        owner_row = result.one_or_none()
        if owner_row is not None:
            owner_emails_json, alerts_enabled = owner_row
            if not alerts_enabled:
                return False, []
            assignees = _parse_emails(owner_emails_json)

    admins = _parse_emails(admin_emails_json)

    seen: set[str] = set()
    recipients: list[str] = []
    for email in [*assignees, *admins]:
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        recipients.append(email)
    return True, recipients


def _parse_emails(emails_json: str | None) -> list[str]:
    if not emails_json:
        return []
    try:
        parsed: Any = json.loads(emails_json)
    except json.JSONDecodeError as exc:
        raise ValueError("emails must be valid JSON") from exc

    if not isinstance(parsed, list) or not all(isinstance(email, str) for email in parsed):
        raise ValueError("emails must be a JSON array of strings")
    return [email for email in (e.strip() for e in parsed) if email]


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
    monitor_label: str,
    severity: str | None = None,
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
        rule_name=monitor_label,
        severity=severity or "",
    )
