from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx

from app.schemas import _is_internal_ip, _normalize_hostname, _validate_webhook_url

logger = logging.getLogger(__name__)

SEND_TIMEOUT = 10.0


async def _validate_resolved_webhook_destination(url: str) -> None:
    """Validate the final DNS answers immediately before sending."""
    _validate_webhook_url(url)
    parsed = urlparse(url)
    hostname = parsed.hostname
    if hostname is None:
        raise ValueError("webhook_url must include a hostname")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    normalized_hostname = _normalize_hostname(hostname)
    infos = await asyncio.to_thread(
        socket.getaddrinfo,
        normalized_hostname,
        port,
        type=socket.SOCK_STREAM,
    )
    for info in infos:
        sockaddr = info[4]
        resolved_ip = ipaddress.ip_address(sockaddr[0])
        if _is_internal_ip(resolved_ip):
            raise ValueError("webhook_url resolved to private/internal address")


def render_template(
    template: str,
    *,
    alert_type: str,
    target_name: str,
    status: str,
    message: str,
    timestamp: str,
    recipients: str,
    rate: str = "",
    threshold: str = "",
    rule_name: str = "",
) -> str:
    """Replace {{placeholders}} in the template string.

    Supported placeholders:
      {{alert_type}}, {{target_name}}, {{status}}, {{message}},
      {{timestamp}}, {{recipients}}, {{rate}}, {{threshold}}, {{rule_name}}
    Unknown placeholders are left untouched.
    """
    replacements = {
        "{{alert_type}}": alert_type,
        "{{target_name}}": target_name,
        "{{status}}": status,
        "{{message}}": message,
        "{{timestamp}}": timestamp,
        "{{recipients}}": recipients,
        "{{rate}}": rate,
        "{{threshold}}": threshold,
        "{{rule_name}}": rule_name,
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
        await _validate_resolved_webhook_destination(url)
        async with httpx.AsyncClient(timeout=SEND_TIMEOUT) as client:
            resp = await client.post(url, content=payload, headers=send_headers)
            resp.raise_for_status()
        return True, None
    except Exception as exc:
        logger.warning("Webhook send failed to %s: %s", url, exc)
        return False, str(exc)
