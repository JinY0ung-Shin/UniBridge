from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from sqlalchemy import select

from app.database import async_session
from app.models import AlertChannel, AlertHistory, AlertRule, AlertRuleChannel
from app.services.alert_sender import render_template, send_webhook
from app.services.alert_state import AlertStateManager

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 60  # seconds

# Route label cache: maps route_id → friendly label (name or uri).
# Refreshed lazily with a TTL to avoid hammering APISIX on every check.
_ROUTE_LABEL_CACHE: dict[str, str] = {}
_ROUTE_LABEL_CACHE_TS: float = 0.0
_ROUTE_LABEL_TTL = 300.0  # 5 minutes


async def _refresh_route_labels() -> None:
    """Refresh route_id → label cache from APISIX.

    Updates `_ROUTE_LABEL_CACHE_TS` on both success AND failure so that the
    TTL governs retry cadence; otherwise an APISIX outage would cause every
    `_get_route_label` call to re-enter this function.
    """
    global _ROUTE_LABEL_CACHE, _ROUTE_LABEL_CACHE_TS
    from app.services import apisix_client
    try:
        data = await apisix_client.list_resources("routes")
        new_cache: dict[str, str] = {}
        for item in data.get("items", []):
            rid = str(item.get("id") or "")
            if not rid:
                continue
            name = item.get("name")
            uri = item.get("uri")
            if not uri:
                uris = item.get("uris") or []
                uri = uris[0] if uris else None
            new_cache[rid] = name or uri or rid
        _ROUTE_LABEL_CACHE = new_cache
    except Exception as exc:
        logger.warning("Failed to refresh route labels: %s", exc)
    finally:
        _ROUTE_LABEL_CACHE_TS = time.monotonic()


async def _get_route_label(route_id: str) -> str:
    """Return friendly label for route_id, falling back to the id itself.

    Assumes single-caller per cycle (via `run_single_check`). If the checker
    ever becomes reentrant, wrap the refresh in an asyncio.Lock.
    """
    if time.monotonic() - _ROUTE_LABEL_CACHE_TS > _ROUTE_LABEL_TTL:
        await _refresh_route_labels()
    return _ROUTE_LABEL_CACHE.get(route_id, route_id)


async def _check_db_health() -> list[tuple[str, bool]]:
    """Check all registered DB connections. Returns [(alias, is_healthy)]."""
    from app.services.connection_manager import connection_manager
    results = []
    for alias in connection_manager.list_aliases():
        try:
            ok, _ = await connection_manager.test_connection(alias)
            results.append((alias, ok))
        except Exception as exc:
            logger.warning("DB health check failed for '%s': %s", alias, exc)
            results.append((alias, False))
    return results


async def _check_upstream_health() -> list[tuple[str, bool]]:
    """Check APISIX upstream health. Returns [(upstream_id, is_healthy)]."""
    from app.services import apisix_client
    results = []
    try:
        data = await apisix_client.list_resources("upstreams")
        for item in data.get("items", []):
            uid = item.get("id", "unknown")
            nodes = item.get("nodes", {})
            is_healthy = bool(nodes) and any(
                w > 0 for w in (nodes.values() if isinstance(nodes, dict) else [])
            )
            results.append((str(uid), is_healthy))
    except Exception as exc:
        logger.warning("Upstream health check failed: %s", exc)
    return results


async def _check_error_rate() -> list[tuple[str, float]]:
    """Check 5xx error rate from Prometheus. Returns [("global", rate_pct)]."""
    from app.services import prometheus_client
    try:
        result = await prometheus_client.instant_query(
            'sum(rate(apisix_http_status{code=~"5.."}[5m])) / sum(rate(apisix_http_status[5m])) * 100'
        )
        if result:
            val = float(result[0].get("value", [0, 0])[1])
            if val != val:  # NaN check
                val = 0.0
            return [("global", val)]
    except Exception as exc:
        logger.warning("Error rate check failed: %s", exc)
    return []


async def _check_route_error_rate() -> list[tuple[str, float]]:
    """Check 5xx error rate per APISIX route.

    Returns [(route_id, rate_pct), ...] for every route that has traffic
    in the last 5 minutes. Routes with 0 errors are included with rate=0
    so that resolved transitions are detected correctly.
    """
    from app.services import prometheus_client
    try:
        total_results = await prometheus_client.instant_query(
            'sum by (route) (rate(apisix_http_status[5m]))'
        )
        if not total_results:
            return []
        err_results = await prometheus_client.instant_query(
            'sum by (route) (rate(apisix_http_status{code=~"5.."}[5m]))'
        )
    except Exception as exc:
        logger.warning("Route error rate check failed: %s", exc)
        return []

    err_map: dict[str, float] = {}
    for item in err_results:
        rid = item.get("metric", {}).get("route")
        if not rid:
            continue
        try:
            val = float(item.get("value", [0, 0])[1])
        except (TypeError, ValueError):
            continue
        if val != val:  # NaN
            continue
        err_map[rid] = val

    route_rates: list[tuple[str, float]] = []
    for item in total_results:
        rid = item.get("metric", {}).get("route")
        if not rid:
            continue
        try:
            total = float(item.get("value", [0, 0])[1])
        except (TypeError, ValueError):
            continue
        if total <= 0 or total != total:  # skip no-traffic / NaN
            continue
        err = err_map.get(rid, 0.0)
        pct = (err / total) * 100
        if pct != pct:
            pct = 0.0
        route_rates.append((str(rid), pct))
    return route_rates


async def _dispatch_alert(
    *,
    rule_type: str,
    alert_type: str,
    target: str,
    message: str,
    rule_id: int | None = None,
    display_target: str | None = None,
    rate: float | None = None,
    threshold: float | None = None,
) -> None:
    """Find matching rules and send alerts through mapped channels.

    If rule_id is given, only dispatch for that specific rule.
    Otherwise, dispatch for all matching enabled rules.

    display_target is what gets rendered into {{target_name}}; defaults
    to target when omitted. rate/threshold are rendered into the
    corresponding placeholders (empty string when None).
    """
    display = display_target if display_target is not None else target
    rate_str = f"{rate:.1f}" if rate is not None else ""
    threshold_str = f"{threshold:.1f}" if threshold is not None else ""

    async with async_session() as db:
        if rule_id is not None:
            q = select(AlertRule).where(
                AlertRule.id == rule_id,
                AlertRule.enabled.is_(True),
            )
        else:
            q = select(AlertRule).where(
                AlertRule.enabled.is_(True),
                AlertRule.type == rule_type,
                AlertRule.target.in_([target, "*"]),
            )
        result = await db.execute(q)
        rules = result.scalars().all()

        now = datetime.now(timezone.utc).isoformat()

        for rule in rules:
            rc_result = await db.execute(
                select(AlertRuleChannel).where(AlertRuleChannel.rule_id == rule.id)
            )
            mappings = rc_result.scalars().all()

            for mapping in mappings:
                ch_result = await db.execute(
                    select(AlertChannel).where(
                        AlertChannel.id == mapping.channel_id,
                        AlertChannel.enabled.is_(True),
                    )
                )
                channel = ch_result.scalar_one_or_none()
                if channel is None:
                    continue

                recipients_list = json.loads(mapping.recipients)
                recipients_str = ", ".join(recipients_list)

                status_label = "장애 발생" if alert_type == "triggered" else "정상 복구"
                payload = render_template(
                    channel.payload_template,
                    alert_type=alert_type,
                    target_name=display,
                    status=status_label,
                    message=message,
                    timestamp=now,
                    recipients=recipients_str,
                    rate=rate_str,
                    threshold=threshold_str,
                    rule_name=rule.name,
                )
                headers = json.loads(channel.headers) if channel.headers else None
                ok, err = await send_webhook(url=channel.webhook_url, payload=payload, headers=headers)

                history = AlertHistory(
                    rule_id=rule.id, channel_id=channel.id,
                    alert_type=alert_type, target=target, message=message,
                    recipients=mapping.recipients,
                    success=ok, error_detail=err,
                )
                db.add(history)

            await db.commit()


async def run_single_check(state: AlertStateManager) -> None:
    """Execute one round of all health checks."""
    # 1. DB health
    db_results = await _check_db_health()
    for alias, is_healthy in db_results:
        transition = state.update("db_health", alias, is_healthy=is_healthy)
        if transition:
            msg = f"Database '{alias}' connection {'restored' if transition == 'resolved' else 'failed'}."
            await _dispatch_alert(
                rule_type="db_health", alert_type=transition,
                target=alias, message=msg,
            )

    # 2. Upstream health
    upstream_results = await _check_upstream_health()
    for uid, is_healthy in upstream_results:
        transition = state.update("upstream_health", uid, is_healthy=is_healthy)
        if transition:
            msg = f"Upstream '{uid}' {'recovered' if transition == 'resolved' else 'is down'}."
            await _dispatch_alert(
                rule_type="upstream_health", alert_type=transition,
                target=uid, message=msg,
            )

    # 3. Error rate (global)
    error_results = await _check_error_rate()
    for target_name, rate in error_results:
        async with async_session() as db:
            q = select(AlertRule).where(
                AlertRule.enabled.is_(True),
                AlertRule.type == "error_rate",
                AlertRule.target.in_([target_name, "*"]),
            )
            result = await db.execute(q)
            rules = result.scalars().all()

        for rule in rules:
            # Honor explicit threshold=0 (user wants aggressive alerting).
            # Use `is not None` instead of `or` so 0.0 is not treated as falsy.
            threshold = rule.threshold if rule.threshold is not None else 10.0
            is_healthy = rate < threshold
            # Use rule ID in state key so multiple rules with different thresholds don't collide
            state_target = f"{target_name}:rule_{rule.id}"
            transition = state.update(
                "error_rate", state_target, is_healthy=is_healthy,
                display_target=target_name,
            )
            if transition:
                msg = f"5xx error rate is {rate:.1f}% (threshold: {threshold}%)."
                await _dispatch_alert(
                    rule_type="error_rate", alert_type=transition,
                    target=target_name, message=msg,
                    rule_id=rule.id,
                    rate=rate, threshold=threshold,
                )

    # 4. Route-level error rate
    route_results = await _check_route_error_rate()
    if route_results:
        async with async_session() as db:
            rq = select(AlertRule).where(
                AlertRule.enabled.is_(True),
                AlertRule.type == "route_error_rate",
            )
            result = await db.execute(rq)
            all_route_rules = result.scalars().all()

        for route_id, rate in route_results:
            matching_rules = [
                r for r in all_route_rules
                if r.target == route_id or r.target == "*"
            ]
            if not matching_rules:
                continue
            label = await _get_route_label(route_id)
            display = f"{label} ({route_id})" if label != route_id else route_id

            for rule in matching_rules:
                threshold = rule.threshold if rule.threshold is not None else 10.0
                is_healthy = rate < threshold
                state_target = f"{route_id}:rule_{rule.id}"
                transition = state.update(
                    "route_error_rate", state_target, is_healthy=is_healthy,
                    display_target=display,
                )
                if transition:
                    msg = (
                        f"Route '{label}' ({route_id}) 5xx error rate is "
                        f"{rate:.1f}% (threshold: {threshold}%)."
                    )
                    await _dispatch_alert(
                        rule_type="route_error_rate", alert_type=transition,
                        target=route_id, message=msg,
                        rule_id=rule.id,
                        display_target=display,
                        rate=rate, threshold=threshold,
                    )


async def start_checker(state: AlertStateManager) -> asyncio.Task:
    """Start the periodic health check loop as a background task."""
    async def _loop():
        logger.info("Alert checker started (interval=%ds)", CHECK_INTERVAL)
        while True:
            try:
                await run_single_check(state)
            except Exception:
                logger.exception("Alert checker cycle failed")
            await asyncio.sleep(CHECK_INTERVAL)

    return asyncio.create_task(_loop())
