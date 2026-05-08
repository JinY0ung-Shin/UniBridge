from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from sqlalchemy import select

from app import metrics
from app.database import async_session
from app.models import AlertChannel, AlertHistory, AlertRule, AlertRuleChannel, AlertSettings
from app.services.alert_owner_dispatcher import dispatch_owner_alert
from app.services.alert_sender import render_template, send_webhook
from app.services.alert_state import AlertStateManager, save_alert_state_to_db

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 60  # seconds
_monotonic = time.monotonic

# Route label cache: maps route_id → friendly label (name or uri).
# Refreshed lazily with a TTL to avoid hammering APISIX on every check.
_ROUTE_LABEL_CACHE: dict[str, str] = {}
_ROUTE_LABEL_CACHE_TS: float = 0.0
_ROUTE_LABEL_TTL = 300.0  # 5 minutes
_UPSTREAM_NAME_BY_ID: dict[str, str] = {}


async def _get_check_interval_seconds() -> int:
    try:
        async with async_session() as db:
            result = await db.execute(
                select(AlertSettings.check_interval_seconds).where(AlertSettings.id == 1)
            )
            interval = result.scalar_one_or_none()
    except Exception as exc:
        logger.warning("Failed to load alert check interval: %s", exc)
        return CHECK_INTERVAL
    if interval is None:
        return CHECK_INTERVAL
    return min(3600, max(30, int(interval)))


def _normalize_route_error_threshold_pct(value: float | int | None) -> float:
    if value is None:
        return 10.0
    return min(100.0, max(0.0, float(value)))


async def _load_route_error_default_threshold(db) -> float:
    result = await db.execute(
        select(AlertSettings.route_error_threshold_pct).where(AlertSettings.id == 1)
    )
    return _normalize_route_error_threshold_pct(result.scalar_one_or_none())


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
        _ROUTE_LABEL_CACHE_TS = _monotonic()


async def _get_route_label(route_id: str) -> str:
    """Return friendly label for route_id, falling back to the id itself.

    Assumes single-caller per cycle (via `run_single_check`). If the checker
    ever becomes reentrant, wrap the refresh in an asyncio.Lock.
    """
    if _monotonic() - _ROUTE_LABEL_CACHE_TS > _ROUTE_LABEL_TTL:
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
    global _UPSTREAM_NAME_BY_ID
    from app.services import apisix_client
    results = []
    try:
        data = await apisix_client.list_resources("upstreams")
        names: dict[str, str] = {}
        for item in data.get("items", []):
            uid = item.get("id", "unknown")
            uid_str = str(uid)
            name = item.get("name")
            if name:
                names[uid_str] = str(name)
            nodes = item.get("nodes", {})
            is_healthy = bool(nodes) and any(
                w > 0 for w in (nodes.values() if isinstance(nodes, dict) else [])
            )
            results.append((uid_str, is_healthy))
        _UPSTREAM_NAME_BY_ID = names
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


async def _check_route_error_rate() -> list[tuple[str, float]] | None:
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
        return None

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
    match_targets: list[str] | None = None,
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
    deliveries: list[dict] = []

    async with async_session() as db:
        if rule_id is not None:
            q = select(AlertRule).where(
                AlertRule.id == rule_id,
                AlertRule.enabled.is_(True),
            )
        else:
            targets = match_targets if match_targets is not None else [target, "*"]
            q = select(AlertRule).where(
                AlertRule.enabled.is_(True),
                AlertRule.type == rule_type,
                AlertRule.target.in_(targets),
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
                deliveries.append(
                    {
                        "rule_id": rule.id,
                        "channel_id": channel.id,
                        "webhook_url": channel.webhook_url,
                        "payload": payload,
                        "headers": headers,
                        "recipients": mapping.recipients,
                    }
                )

    histories: list[AlertHistory] = []
    dispatch_metrics: list[dict[str, str | int]] = []
    for delivery in deliveries:
        ok, err = await send_webhook(
            url=delivery["webhook_url"],
            payload=delivery["payload"],
            headers=delivery["headers"],
        )
        histories.append(
            AlertHistory(
                rule_id=delivery["rule_id"],
                channel_id=delivery["channel_id"],
                alert_type=alert_type,
                target=target,
                message=message,
                recipients=delivery["recipients"],
                success=ok,
                error_detail=err,
            )
        )
        dispatch_metrics.append(
            {
                "rule_id": delivery["rule_id"],
                "channel_type": "webhook",
                "status": "success" if ok else "failure",
            }
        )

    if histories:
        async with async_session() as db:
            for history in histories:
                db.add(history)
            await db.commit()

    for dispatch_metric in dispatch_metrics:
        metrics.record_alert_dispatch(**dispatch_metric)


async def _persist_state_safely(
    state: AlertStateManager,
    alert_type: str,
    target: str,
) -> None:
    try:
        async with async_session() as db:
            await save_alert_state_to_db(db, state, alert_type, target)
    except Exception as exc:
        logger.warning("Failed to persist alert state %s/%s: %s", alert_type, target, exc)


def _route_state_target(route_id: str, rule_id: int) -> str:
    return f"{route_id}:rule_{rule_id}"


def _parse_route_state_target(value: str) -> tuple[str, int] | None:
    marker = ":rule_"
    if marker not in value:
        return None
    route_id, rule_id_text = value.rsplit(marker, 1)
    if not route_id:
        return None
    try:
        return route_id, int(rule_id_text)
    except ValueError:
        return None


async def _evaluate_route_error_rule(
    state: AlertStateManager,
    *,
    route_id: str,
    rate: float,
    rule: AlertRule,
    display_target: str | None = None,
    default_threshold: float = 10.0,
) -> None:
    if display_target is None:
        label = await _get_route_label(route_id)
        display = f"{label} ({route_id})" if label != route_id else route_id
    else:
        display = display_target

    threshold = rule.threshold if rule.threshold is not None else default_threshold
    is_healthy = rate < threshold
    state_target = _route_state_target(route_id, rule.id)
    transition = state.update(
        "route_error_rate", state_target, is_healthy=is_healthy,
        display_target=display,
    )
    await _persist_state_safely(state, "route_error_rate", state_target)
    if transition:
        msg = (
            f"Route '{display}' 5xx error rate is "
            f"{rate:.1f}% (threshold: {threshold}%)."
        )
        await dispatch_owner_alert(
            resource_type="route", resource_id=route_id,
            alert_type=transition, target=route_id, message=msg,
            rule_id=rule.id,
            display_target=display,
            rate=rate, threshold=threshold,
            rule_name=rule.name,
        )


async def run_single_check(state: AlertStateManager) -> None:
    """Execute one round of all health checks."""
    # 1. DB health
    db_results = await _check_db_health()
    for alias, is_healthy in db_results:
        transition = state.update("db_health", alias, is_healthy=is_healthy)
        await _persist_state_safely(state, "db_health", alias)
        if transition:
            msg = f"Database '{alias}' connection {'restored' if transition == 'resolved' else 'failed'}."
            await dispatch_owner_alert(
                resource_type="db", resource_id=alias,
                alert_type=transition, target=alias, message=msg,
                display_target=alias,
            )

    # 2. Upstream health
    upstream_results = await _check_upstream_health()
    for uid, is_healthy in upstream_results:
        upstream_name = _UPSTREAM_NAME_BY_ID.get(uid)
        display = f"{upstream_name} ({uid})" if upstream_name and upstream_name != uid else uid
        transition = state.update(
            "upstream_health", uid, is_healthy=is_healthy,
            display_target=display,
        )
        await _persist_state_safely(state, "upstream_health", uid)
        if transition:
            msg = f"Upstream '{display}' {'recovered' if transition == 'resolved' else 'is down'}."
            await dispatch_owner_alert(
                resource_type="upstream", resource_id=uid,
                alert_type=transition, target=uid, message=msg,
                display_target=display,
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
            await _persist_state_safely(state, "error_rate", state_target)
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
    if route_results is not None:
        active_route_alerts = state.get_entries(alert_type="route_error_rate", status="alert")
        if not route_results and not active_route_alerts:
            return

        async with async_session() as db:
            rq = select(AlertRule).where(
                AlertRule.enabled.is_(True),
                AlertRule.type == "route_error_rate",
            )
            result = await db.execute(rq)
            all_route_rules = result.scalars().all()
            route_default_threshold = (
                await _load_route_error_default_threshold(db)
                if any(rule.threshold is None for rule in all_route_rules)
                else 10.0
            )

        rules_by_id = {rule.id: rule for rule in all_route_rules}
        processed_state_targets: set[str] = set()
        route_rate_by_id = dict(route_results)

        for route_id, rate in route_results:
            matching_rules = [
                r for r in all_route_rules
                if r.target == route_id or r.target == "*"
            ]
            for rule in matching_rules:
                state_target = _route_state_target(route_id, rule.id)
                processed_state_targets.add(state_target)
                await _evaluate_route_error_rule(
                    state,
                    route_id=route_id,
                    rate=rate,
                    rule=rule,
                    default_threshold=route_default_threshold,
                )

        for entry in active_route_alerts:
            state_target = entry["target"]
            if state_target in processed_state_targets:
                continue
            parsed = _parse_route_state_target(state_target)
            if parsed is None:
                continue
            route_id, rule_id = parsed
            if route_id in route_rate_by_id:
                continue
            rule = rules_by_id.get(rule_id)
            if rule is None:
                continue
            await _evaluate_route_error_rule(
                state,
                route_id=route_id,
                rate=0.0,
                rule=rule,
                display_target=entry.get("display_target"),
                default_threshold=route_default_threshold,
            )


async def start_checker(state: AlertStateManager) -> asyncio.Task:
    """Start the periodic health check loop as a background task."""
    async def _loop():
        logger.info("Alert checker started")
        while True:
            cycle_start = _monotonic()
            check_interval = await _get_check_interval_seconds()
            try:
                await run_single_check(state)
            except Exception:
                logger.exception("Alert checker cycle failed")
            elapsed = _monotonic() - cycle_start
            await asyncio.sleep(max(0.0, check_interval - elapsed))

    return asyncio.create_task(_loop())
