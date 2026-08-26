"""Internal Alertmanager webhook receiver.

Prometheus evaluates the critical infra rules in ``prometheus/rules/*.yml`` and
routes the fired ones to Alertmanager, which POSTs them here. This endpoint is
the bridge between that infra-level alerting path and the app's existing
owner/admin email pipeline (:func:`app.services.alert_owner_dispatcher.dispatch_alert`),
so operators keep one recipient system instead of two.

Why it exists at all: the in-app alert checker
(:mod:`app.services.alert_checker`) runs *inside* unibridge-service, so it can
never report its own death — ``UniBridgeServiceDown`` / ``UniBridgeMetaDbDown``
are exactly the alerts it cannot raise. Prometheus + Alertmanager are separate
containers and can. (When the app is down the POST obviously fails too; the
alert stays visible in the Alertmanager UI and is redelivered on the next
``repeat_interval``, once the app is back. The service-down alerts also route to
an optional direct-SMTP receiver that does not involve the app at all — see
``alertmanager/alertmanager.yml``.)

Auth: a shared bearer token (``ALERTMANAGER_WEBHOOK_TOKEN``), never a user JWT —
Alertmanager is a machine. An empty setting disables the endpoint (503) so a
deployment without Alertmanager wired never exposes an unauthenticated dispatch
surface; the first rejected delivery logs a warning naming the missing setting,
because that 503 is otherwise indistinguishable from working alerting.
Reachable in-cluster as ``POST /internal/alertmanager`` and, through the UI
nginx ``/_api/`` rewrite, as ``POST /_api/internal/alertmanager``.

Recipients: infra alerts are not tied to a registered DB/S3/host, so they carry
``resource_type="infra"`` — deliberately *not* in
``alert_owner_dispatcher.ASSIGNEE_RESOURCE_TYPES``, which makes
``_resolve_recipients`` skip assignee lookup and notify the global admins only.
Same mechanism the ``upstream`` alerts already rely on.
"""
from __future__ import annotations

import hmac
import logging
import re
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["Internal"])

# Transition strings the rest of the alert pipeline speaks: AlertHistory
# .alert_type, the /admin/alerts/history filter and the UI badge all expect
# exactly "triggered" / "resolved" (see alert_checker._outbound_alert_type).
ALERT_TYPE_TRIGGERED = "triggered"
ALERT_TYPE_RESOLVED = "resolved"

# Monitoring-rule attribution recorded on every row this endpoint writes, so
# `GET /admin/alerts/history?rule_type=prometheus_alert` lists exactly the
# infra alerts that came in over this webhook. Must fit AlertHistory.rule_type
# (String(30)).
RULE_TYPE = "prometheus_alert"

# Not an assignee resource type — see the module docstring. Fits
# AlertHistory.resource_type (String(20)).
RESOURCE_TYPE = "infra"

# Rendered into the mail template as {{rule_name}}, alongside the checker's
# own labels ("DB 헬스체크", "라우트 에러율", …).
MONITOR_LABEL = "Prometheus 알림"

# Column ceilings in AlertHistory / the mail payload. SQLite does not enforce
# String(n), so truncate here rather than ship an over-long value.
_MAX_TARGET = 100
_MAX_DISPLAY_TARGET = 200
_MAX_SEVERITY = 20
_MAX_ANNOTATION = 500

# Upper bound on one batch. Alertmanager's own `max_alerts` already caps what
# it sends; this is the app-side floor under a misconfigured or hostile sender,
# since every alert becomes an outbound e-mail.
MAX_ALERTS_PER_REQUEST = 100

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# A stock deployment leaves ALERTMANAGER_WEBHOOK_TOKEN empty, so Alertmanager's
# deliveries 503 and nothing anywhere says why. Say it once: Alertmanager retries
# a failed group every few minutes forever, and logging per request would bury
# the logs it is trying to draw attention to. Reset in tests.
_disabled_warning_logged = False


def _clean(value: Any, limit: int) -> str:
    """Flatten one label/annotation into a single safe line.

    The mail channel renders its payload template by plain ``{{placeholder}}``
    substitution (``alert_sender.render_template``), so a value carrying a
    quote, a backslash or a newline would break the resulting JSON. Alert text
    reaches us from outside the app, so neutralise those here instead of
    trusting the sender.
    """
    if value is None:
        return ""
    text = _CONTROL_CHARS.sub(" ", str(value))
    text = text.replace('"', "'").replace("\\", "/")
    return " ".join(text.split())[:limit].strip()


def _verify_token(authorization: str | None) -> None:
    """Gate the endpoint on the shared bearer token.

    Read from ``settings`` at call time (not import time) so a runtime change —
    and the tests' monkeypatch — takes effect.
    """
    expected = settings.ALERTMANAGER_WEBHOOK_TOKEN
    if not expected:
        global _disabled_warning_logged
        if not _disabled_warning_logged:
            _disabled_warning_logged = True
            logger.warning(
                "Alertmanager posted to /internal/alertmanager but "
                "ALERTMANAGER_WEBHOOK_TOKEN is empty: Prometheus alert forwarding "
                "is disabled and every delivery is rejected with 503. Set it to "
                "the same value the Alertmanager config renders. Logged once per "
                "process."
            )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Alertmanager webhook not configured",
        )

    scheme, _, credentials = (authorization or "").partition(" ")
    credentials = credentials.strip()
    if scheme.lower() != "bearer" or not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not hmac.compare_digest(credentials.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _translate(alert: dict[str, Any]) -> dict[str, Any]:
    """Turn one Alertmanager alert object into ``dispatch_alert`` kwargs."""
    labels = alert.get("labels")
    annotations = alert.get("annotations")
    labels = labels if isinstance(labels, dict) else {}
    annotations = annotations if isinstance(annotations, dict) else {}

    alertname = _clean(labels.get("alertname"), _MAX_TARGET) or "UnnamedAlert"
    severity = _clean(labels.get("severity"), _MAX_SEVERITY) or None
    instance = _clean(labels.get("instance") or labels.get("job"), 80)
    summary = _clean(annotations.get("summary"), _MAX_ANNOTATION)
    description = _clean(annotations.get("description"), _MAX_ANNOTATION)

    # Only the exact string "resolved" clears an alert; anything else (firing,
    # missing, unknown) notifies, because dropping a real alert is worse than
    # one redundant e-mail.
    resolved = alert.get("status") == "resolved"
    alert_type = ALERT_TYPE_RESOLVED if resolved else ALERT_TYPE_TRIGGERED

    display_target = f"{alertname} ({instance})" if instance else alertname
    head = f"Prometheus alert '{alertname}'"
    if severity:
        head += f" ({severity})"
    head += " resolved." if resolved else " is firing."
    # Keep summary + description, but never repeat an identical description.
    parts = [summary] if summary else []
    if description and description != summary:
        parts.append(description)
    detail = " — ".join(parts)
    message = f"{head} {detail}".strip() if detail else head

    return {
        "resource_type": RESOURCE_TYPE,
        "resource_id": alertname,
        "alert_type": alert_type,
        "rule_type": RULE_TYPE,
        "target": alertname,
        "message": message,
        "display_target": display_target[:_MAX_DISPLAY_TARGET],
        "monitor_label": MONITOR_LABEL,
        "severity": severity,
        "target_description": description or None,
    }


@router.post("/alertmanager", include_in_schema=False)
async def receive_alertmanager_webhook(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, int]:
    """Fan an Alertmanager batch out through the owner/admin e-mail pipeline.

    Best-effort per alert, like the rest of the alert pipeline: one bad or
    undeliverable alert is logged and skipped, never aborting the batch —
    Alertmanager would otherwise retry the whole group and re-send the ones
    that already went out.
    """
    _verify_token(authorization)

    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 — any parse failure is a bad request
        logger.warning("Alertmanager webhook: malformed JSON body: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must be valid JSON",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must be a JSON object",
        )
    alerts = payload.get("alerts")
    if not isinstance(alerts, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payload must contain an 'alerts' array",
        )

    truncated = payload.get("truncatedAlerts")
    if isinstance(truncated, int) and truncated > 0:
        # Alertmanager hit its own `max_alerts` cap — these never reach us.
        logger.warning(
            "Alertmanager webhook: sender truncated %d alert(s) from this batch",
            truncated,
        )

    received = len(alerts)
    if received > MAX_ALERTS_PER_REQUEST:
        logger.error(
            "Alertmanager webhook: batch of %d alerts exceeds the %d cap — "
            "dispatching the first %d only",
            received, MAX_ALERTS_PER_REQUEST, MAX_ALERTS_PER_REQUEST,
        )
        alerts = alerts[:MAX_ALERTS_PER_REQUEST]

    from app.services.alert_owner_dispatcher import dispatch_alert

    dispatched = 0
    for raw in alerts:
        if not isinstance(raw, dict):
            logger.warning("Alertmanager webhook: skipping non-object alert entry")
            continue
        try:
            kwargs = _translate(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Alertmanager webhook: could not translate alert: %s", exc)
            continue
        try:
            await dispatch_alert(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Alertmanager webhook: dispatch failed for %s: %s",
                kwargs.get("target"), exc,
            )
            continue
        dispatched += 1

    logger.info(
        "Alertmanager webhook: received=%d dispatched=%d", received, dispatched,
    )
    return {"received": received, "dispatched": dispatched}
