from __future__ import annotations

import json
import logging

import httpx

from app.services.webhook_security import (
    mask_webhook_url,
    redact_webhook_url,
    validate_webhook_url,
)

logger = logging.getLogger(__name__)

SEND_TIMEOUT = 10.0


def render_recipient_items(template: str, emails: list[str]) -> str:
    """Render one JSON object per email and return a JSON array string."""
    if not emails:
        return "[]"

    quoted_email_placeholder = '"{{email}}"'
    if quoted_email_placeholder not in template:
        if "{{email}}" in template:
            raise ValueError(
                "recipient_item_template {{email}} placeholder must be a JSON string value"
            )
        raise ValueError(
            'recipient_item_template must include "{{email}}" as a JSON string value'
        )

    items: list[dict] = []
    for email in emails:
        rendered = template.replace(
            quoted_email_placeholder,
            json.dumps(email, ensure_ascii=False),
        )
        try:
            parsed = json.loads(rendered)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"recipient_item_template rendered invalid JSON for {email}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ValueError("recipient_item_template must render to a JSON object")
        items.append(parsed)
    return json.dumps(items, ensure_ascii=False)


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
    severity: str = "",
    target_description: str = "",
) -> str:
    """Replace {{placeholders}} in the template string.

    Supported placeholders:
      {{alert_type}}, {{target_name}}, {{status}}, {{message}},
      {{timestamp}}, {{recipients}}, {{recipients_json}}, {{rate}},
      {{threshold}}, {{rule_name}}, {{severity}}, {{target_description}}
    Unknown placeholders are left untouched.
    """
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
        "{{severity}}": severity,
        "{{target_description}}": target_description,
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
    """POST payload to webhook URL. Returns (success, error_detail).

    The detail is stored on the alert history row and shown to ``alerts.read``
    holders, and client errors quote the URL they failed on — so both the detail
    and the log line carry the masked URL, never the token in its path or query.
    """
    send_headers = {"Content-Type": "application/json"}
    if headers:
        send_headers.update(headers)
    try:
        validate_webhook_url(url)
        async with httpx.AsyncClient(timeout=SEND_TIMEOUT) as client:
            resp = await client.post(url, content=payload, headers=send_headers)
            resp.raise_for_status()
        return True, None
    except Exception as exc:
        reason = redact_webhook_url(str(exc), url)
        detail = f"{type(exc).__name__}: {reason}" if reason else type(exc).__name__
        logger.warning("Webhook send failed to %s: %s", mask_webhook_url(url), detail)
        return False, detail
