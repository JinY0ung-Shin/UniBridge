from __future__ import annotations

import logging

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
