"""Monitored-server (host) registry + live status and metrics.

CRUD over :class:`~app.models.MonitoredHost`. Each mutation rewrites the
Prometheus file_sd targets so the scrape set tracks the registry, and clears
any alert state for a removed/renamed host. Live status and per-host metric
time series are read straight from Prometheus.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, require_permission
from app.database import get_db
from app.models import AlertSettings, MonitoredHost
from app.schemas import (
    MonitoredHostCreate,
    MonitoredHostResponse,
    MonitoredHostUpdate,
    ServerMetricPoint,
    ServerMetricSeries,
)
from app.services import prometheus_client, server_monitor
from app.services.alert_state import delete_alert_state
from app.services.audit import log_admin_action

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/servers", tags=["Servers"])

_METRICS = {"cpu", "mem", "disk"}


def _reject_invalid_disk_thresholds(warn: float, crit: float) -> None:
    if warn > crit:
        raise HTTPException(
            status_code=422,
            detail="disk_warn_pct must be less than or equal to disk_crit_pct",
        )


async def _global_disk_thresholds(db: AsyncSession) -> tuple[float, float]:
    result = await db.execute(select(AlertSettings).where(AlertSettings.id == 1))
    settings = result.scalar_one_or_none()
    if settings is None:
        return 80.0, 90.0
    return float(settings.server_disk_warn_pct), float(settings.server_disk_crit_pct)


async def _validate_effective_disk_thresholds(
    db: AsyncSession,
    *,
    warn: float | None,
    crit: float | None,
) -> None:
    global_warn, global_crit = await _global_disk_thresholds(db)
    effective_warn = float(warn) if warn is not None else global_warn
    effective_crit = float(crit) if crit is not None else global_crit
    _reject_invalid_disk_thresholds(effective_warn, effective_crit)


def _parse_labels(labels_json: str | None) -> dict[str, str] | None:
    if not labels_json:
        return None
    try:
        parsed = json.loads(labels_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return {str(k): str(v) for k, v in parsed.items()}


def _host_response(host: MonitoredHost, status: str | None = None) -> MonitoredHostResponse:
    return MonitoredHostResponse(
        id=host.id,
        name=host.name,
        address=host.address,
        enabled=host.enabled,
        description=host.description or "",
        labels=_parse_labels(host.labels),
        disk_mountpoints=host.disk_mountpoints,
        disk_warn_pct=host.disk_warn_pct,
        disk_crit_pct=host.disk_crit_pct,
        cpu_warn_pct=host.cpu_warn_pct,
        mem_warn_pct=host.mem_warn_pct,
        status=status,
        created_at=host.created_at,
        updated_at=host.updated_at,
    )


def _audit_snapshot(host: MonitoredHost) -> dict[str, Any]:
    return {
        "name": host.name,
        "address": host.address,
        "enabled": host.enabled,
        "description": host.description or "",
        "labels": _parse_labels(host.labels),
        "disk_mountpoints": host.disk_mountpoints,
        "disk_warn_pct": host.disk_warn_pct,
        "disk_crit_pct": host.disk_crit_pct,
        "cpu_warn_pct": host.cpu_warn_pct,
        "mem_warn_pct": host.mem_warn_pct,
    }


async def _clear_host_alert_state(db: AsyncSession, host_name: str) -> None:
    """Drop in-memory + persisted alert state for every signal of a host."""
    from app.routers.alerts import get_alert_state

    state = get_alert_state()
    for alert_type in server_monitor.SERVER_ALERT_TYPES:
        if state is not None:
            state.discard(alert_type, host_name)
        await delete_alert_state(db, alert_type, host_name)


@router.get("", response_model=list[MonitoredHostResponse])
async def list_servers(
    _user: CurrentUser = Depends(require_permission("servers.read")),
    db: AsyncSession = Depends(get_db),
) -> list[MonitoredHostResponse]:
    result = await db.execute(select(MonitoredHost).order_by(MonitoredHost.name))
    hosts = result.scalars().all()
    up_map = await server_monitor.host_up_map()  # None if Prometheus unreachable
    rows: list[MonitoredHostResponse] = []
    for host in hosts:
        if not host.enabled:
            status = "disabled"
        elif up_map is None:
            status = "unknown"
        elif host.name in up_map:
            status = "up" if up_map[host.name] else "down"
        else:
            status = "unknown"
        rows.append(_host_response(host, status))
    return rows


@router.post("", response_model=MonitoredHostResponse, status_code=201)
async def create_server(
    body: MonitoredHostCreate,
    _user: CurrentUser = Depends(require_permission("servers.write")),
    db: AsyncSession = Depends(get_db),
) -> MonitoredHostResponse:
    await _validate_effective_disk_thresholds(
        db,
        warn=body.disk_warn_pct,
        crit=body.disk_crit_pct,
    )
    host = MonitoredHost(
        name=body.name,
        address=body.address,
        enabled=body.enabled,
        description=body.description,
        labels=json.dumps(body.labels, ensure_ascii=False) if body.labels else None,
        disk_mountpoints=body.disk_mountpoints,
        disk_warn_pct=body.disk_warn_pct,
        disk_crit_pct=body.disk_crit_pct,
        cpu_warn_pct=body.cpu_warn_pct,
        mem_warn_pct=body.mem_warn_pct,
    )
    db.add(host)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Server '{body.name}' already exists")
    await db.refresh(host)
    await server_monitor.sync_targets_from_db(db)
    await log_admin_action(
        db, actor=_user.username, action="create",
        resource_type="monitored_host", resource_id=host.name, summary=host.address,
        before=None, after=_audit_snapshot(host),
    )
    return _host_response(host)


@router.put("/{host_id}", response_model=MonitoredHostResponse)
async def update_server(
    host_id: int,
    body: MonitoredHostUpdate,
    _user: CurrentUser = Depends(require_permission("servers.write")),
    db: AsyncSession = Depends(get_db),
) -> MonitoredHostResponse:
    host = await db.get(MonitoredHost, host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="Server not found")
    before = _audit_snapshot(host)
    next_disk_warn = body.disk_warn_pct if "disk_warn_pct" in body.model_fields_set else host.disk_warn_pct
    next_disk_crit = body.disk_crit_pct if "disk_crit_pct" in body.model_fields_set else host.disk_crit_pct
    await _validate_effective_disk_thresholds(
        db,
        warn=next_disk_warn,
        crit=next_disk_crit,
    )
    if body.address is not None:
        host.address = body.address
    if body.enabled is not None:
        host.enabled = body.enabled
    if body.description is not None:
        host.description = body.description
    if "labels" in body.model_fields_set:
        host.labels = json.dumps(body.labels, ensure_ascii=False) if body.labels else None
    if "disk_mountpoints" in body.model_fields_set:
        host.disk_mountpoints = body.disk_mountpoints
    for field in ("disk_warn_pct", "disk_crit_pct", "cpu_warn_pct", "mem_warn_pct"):
        if field in body.model_fields_set:
            setattr(host, field, getattr(body, field))
    await db.commit()
    await db.refresh(host)
    # A disabled host is dropped from the scrape set; clear its alert state so it
    # doesn't linger as a stale "down" while unscraped.
    if not host.enabled:
        await _clear_host_alert_state(db, host.name)
        await db.commit()
    await server_monitor.sync_targets_from_db(db)
    await log_admin_action(
        db, actor=_user.username, action="update",
        resource_type="monitored_host", resource_id=host.name, summary=host.address,
        before=before, after=_audit_snapshot(host),
    )
    return _host_response(host)


@router.delete("/{host_id}", status_code=204, response_model=None)
async def delete_server(
    host_id: int,
    _user: CurrentUser = Depends(require_permission("servers.write")),
    db: AsyncSession = Depends(get_db),
) -> None:
    host = await db.get(MonitoredHost, host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="Server not found")
    before = _audit_snapshot(host)
    host_name = host.name
    await _clear_host_alert_state(db, host_name)
    await db.delete(host)
    await db.commit()
    await server_monitor.sync_targets_from_db(db)
    await log_admin_action(
        db, actor=_user.username, action="delete",
        resource_type="monitored_host", resource_id=host_name, summary=before["address"],
        before=before, after=None,
    )


@router.post("/{host_id}/test")
async def test_server(
    host_id: int,
    _user: CurrentUser = Depends(require_permission("servers.read")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    host = await db.get(MonitoredHost, host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="Server not found")
    up_map = await server_monitor.host_up_map()
    if up_map is None:
        return {"status": "unknown", "detail": "Prometheus unreachable"}
    if host.name not in up_map:
        return {"status": "unknown", "detail": "No scrape data yet for this host"}
    return {"status": "up" if up_map[host.name] else "down", "detail": None}


@router.get("/{host_id}/metrics", response_model=list[ServerMetricSeries])
async def server_metrics(
    host_id: int,
    duration: str = Query("1h"),
    step: str = Query("60s"),
    _user: CurrentUser = Depends(require_permission("servers.read")),
    db: AsyncSession = Depends(get_db),
) -> list[ServerMetricSeries]:
    host = await db.get(MonitoredHost, host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="Server not found")

    series: list[ServerMetricSeries] = []
    for metric in ("cpu", "mem", "disk"):
        query = server_monitor.metric_query(metric, host.name, disk_mountpoints=host.disk_mountpoints)
        if query is None:
            continue
        try:
            results = await prometheus_client.range_query(query, duration=duration, step=step)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Server metric query failed (%s/%s): %s", host.name, metric, exc)
            results = []
        points: list[ServerMetricPoint] = []
        if results:
            for ts, raw in results[0].get("values", []):
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    value = None
                if value is not None and value != value:  # NaN
                    value = None
                points.append(ServerMetricPoint(t=float(ts), v=value))
        series.append(ServerMetricSeries(metric=metric, points=points))
    return series
