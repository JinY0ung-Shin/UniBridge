"""Server (host) monitoring via node_exporter metrics in Prometheus.

Two responsibilities:

1. **Service discovery** — render the :class:`~app.models.MonitoredHost`
   registry into a Prometheus file-based service-discovery (``file_sd``) JSON
   file, so Prometheus scrapes each registered host's node_exporter without a
   config reload.
2. **Evaluation** — query Prometheus for host signals (reachability, disk,
   disk-fill forecast, CPU, memory) grouped by the ``host`` label and turn them
   into :class:`HostSignal` results. The alert checker feeds these through the
   shared :class:`~app.services.alert_state.AlertStateManager` /
   ``dispatch_alert`` pipeline, exactly like the DB/NAS/route signals.

All Prometheus access goes through :mod:`app.services.prometheus_client`, which
raises on transport errors; :func:`evaluate_hosts` treats any query failure as
"skip this cycle" rather than risk a false mass-``server_down`` storm when
Prometheus itself is briefly unreachable.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any, Iterable

from app.config import settings
from app.services import prometheus_client

logger = logging.getLogger(__name__)

# Pseudo-filesystems that should never count toward host disk-capacity alerts.
_FS_EXCLUDE = "tmpfs|overlay|squashfs|ramfs|devtmpfs"

# All alert_type strings produced for host signals (keyed by host name).
SERVER_ALERT_TYPES = (
    "server_down",
    "server_disk",
    "server_disk_forecast",
    "server_cpu",
    "server_mem",
)


def _job() -> str:
    return settings.NODE_EXPORTER_JOB


def _mountpoint_values(raw: str | None = None) -> tuple[str, ...]:
    if raw is None:
        raw = settings.NODE_EXPORTER_DISK_MOUNTPOINTS or ""
    return tuple(m.strip() for m in raw.split(",") if m.strip())


def _regex_label_literal(value: str) -> str:
    return _escape_label(re.escape(value))


def _mountpoint_selector(raw: str | None = None) -> str:
    """Optional ``,mountpoint=~"^(...)$"`` label fragment restricting disk
    metrics to a mountpoint whitelist.

    Empty config → empty string → no filter, i.e. every real filesystem is
    considered (the historical behavior). Mountpoint values are regex-escaped
    so paths with metacharacters cannot alter the selector.
    """
    mounts = _mountpoint_values(raw)
    if not mounts:
        return ""
    alt = "|".join(_regex_label_literal(m) for m in mounts)
    return f',mountpoint=~"^({alt})$"'


def _host_selector(host_names: Iterable[str] | None = None) -> str:
    if not host_names:
        return ""
    names = [str(name) for name in host_names if str(name)]
    if not names:
        return ""
    if len(names) == 1:
        return f',host="{_escape_label(names[0])}"'
    alt = "|".join(_regex_label_literal(name) for name in names)
    return f',host=~"^({alt})$"'


def _disk_mountpoint_groups(hosts: Iterable[Any]) -> dict[tuple[str, ...], list[str]]:
    """Group hosts by their effective disk mountpoint whitelist.

    ``MonitoredHost.disk_mountpoints`` overrides the global env. Hosts without a
    per-host value inherit ``NODE_EXPORTER_DISK_MOUNTPOINTS``.
    """
    groups: dict[tuple[str, ...], list[str]] = {}
    for host in hosts:
        name = str(getattr(host, "name", "") or "")
        if not name:
            continue
        raw = getattr(host, "disk_mountpoints", None)
        effective_raw = raw if raw else (settings.NODE_EXPORTER_DISK_MOUNTPOINTS or "")
        groups.setdefault(_mountpoint_values(effective_raw), []).append(name)
    return groups


def _has_disk_mountpoint_override(hosts: Iterable[Any]) -> bool:
    return any(bool(getattr(host, "disk_mountpoints", None)) for host in hosts)


# ── File-based service discovery ─────────────────────────────────────────────


def build_targets(hosts: Iterable[Any]) -> list[dict[str, Any]]:
    """Render enabled MonitoredHosts into Prometheus file_sd entries.

    Each entry carries a ``host`` label (the friendly name, used as the alert
    target) plus any user-defined labels stored as JSON on the host.
    """
    entries: list[dict[str, Any]] = []
    for host in hosts:
        if not getattr(host, "enabled", True):
            continue
        labels: dict[str, str] = {"host": host.name}
        if host.labels:
            try:
                extra = json.loads(host.labels)
            except (json.JSONDecodeError, TypeError):
                extra = None
            if isinstance(extra, dict):
                for key, value in extra.items():
                    # Never let a user label clobber the identifying host label.
                    if str(key) == "host":
                        continue
                    labels[str(key)] = str(value)
        entries.append({"targets": [host.address], "labels": labels})
    return entries


def _write_json_atomic(path: str, data: Any) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        # mkstemp creates the file 0600; Prometheus scrapes file_sd as a
        # different UID (nobody) and must be able to read it. Without this the
        # targets file is silently unreadable -> zero "nodes" targets.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


async def write_targets_file(hosts: Iterable[Any], path: str | None = None) -> None:
    """Write the file_sd targets file atomically. Raises on I/O failure."""
    target_path = path or settings.PROMETHEUS_FILE_SD_PATH
    entries = build_targets(hosts)
    await asyncio.to_thread(_write_json_atomic, target_path, entries)


async def sync_targets_from_db(db) -> None:
    """Reload the host registry and rewrite the file_sd targets (best-effort).

    The database is the source of truth; a write failure here is logged but not
    fatal — the next successful sync (a later CRUD op or boot reconcile) repairs
    it. Callers should not depend on this raising.
    """
    from sqlalchemy import select

    from app.models import MonitoredHost

    result = await db.execute(select(MonitoredHost))
    hosts = result.scalars().all()
    try:
        await write_targets_file(hosts)
    except Exception as exc:  # noqa: BLE001 — best-effort reconcile
        logger.warning(
            "Failed to write Prometheus file_sd targets to %s: %s",
            settings.PROMETHEUS_FILE_SD_PATH,
            exc,
        )


# ── Threshold inputs ─────────────────────────────────────────────────────────


@dataclass
class ServerThresholds:
    """Global host thresholds (from AlertSettings); per-host columns override."""

    disk_warn_pct: float = 80.0
    disk_crit_pct: float = 90.0
    cpu_warn_pct: float = 90.0
    mem_warn_pct: float = 90.0
    forecast_hours: float = 24.0


@dataclass
class HostSignal:
    """One evaluated host signal, ready for the alert-state pipeline."""

    alert_type: str
    target: str          # host name (== ResourceOwner resource_id)
    display: str
    is_healthy: bool
    severity: str | None
    value: float | None
    threshold: float | None
    message: str
    monitor_label: str


# ── PromQL ───────────────────────────────────────────────────────────────────


def _q_up() -> str:
    return f'up{{job="{_job()}"}}'


def _q_disk_pct(
    mountpoints_raw: str | None = None,
    host_names: Iterable[str] | None = None,
) -> str:
    j = _job()
    hs = _host_selector(host_names)
    mp = _mountpoint_selector(mountpoints_raw)
    return (
        f'max by (host) (100 * (1 - '
        f'node_filesystem_avail_bytes{{job="{j}",fstype!~"{_FS_EXCLUDE}"{hs}{mp}}} / '
        f'node_filesystem_size_bytes{{job="{j}",fstype!~"{_FS_EXCLUDE}"{hs}{mp}}}))'
    )


def _q_disk_forecast(
    horizon_seconds: float,
    mountpoints_raw: str | None = None,
    host_names: Iterable[str] | None = None,
) -> str:
    j = _job()
    hs = _host_selector(host_names)
    mp = _mountpoint_selector(mountpoints_raw)
    return (
        f'min by (host) (predict_linear('
        f'node_filesystem_avail_bytes{{job="{j}",fstype!~"{_FS_EXCLUDE}"{hs}{mp}}}[6h], '
        f'{int(horizon_seconds)}))'
    )


def _q_disk_pct_for_hosts(hosts: Iterable[Any]) -> str:
    host_list = list(hosts)
    if not _has_disk_mountpoint_override(host_list):
        return _q_disk_pct()
    return " or ".join(
        f"({_q_disk_pct(','.join(mountpoints), host_names)})"
        for mountpoints, host_names in _disk_mountpoint_groups(host_list).items()
    )


def _q_disk_forecast_for_hosts(hosts: Iterable[Any], horizon_seconds: float) -> str:
    host_list = list(hosts)
    if not _has_disk_mountpoint_override(host_list):
        return _q_disk_forecast(horizon_seconds)
    return " or ".join(
        f"({_q_disk_forecast(horizon_seconds, ','.join(mountpoints), host_names)})"
        for mountpoints, host_names in _disk_mountpoint_groups(host_list).items()
    )


def _q_cpu_pct() -> str:
    return (
        f'100 * (1 - avg by (host) (rate('
        f'node_cpu_seconds_total{{job="{_job()}",mode="idle"}}[5m])))'
    )


def _q_mem_pct() -> str:
    j = _job()
    return (
        f'100 * (1 - node_memory_MemAvailable_bytes{{job="{j}"}} / '
        f'node_memory_MemTotal_bytes{{job="{j}"}})'
    )


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def metric_query(
    metric: str,
    host_name: str,
    *,
    disk_mountpoints: str | None = None,
) -> str | None:
    """PromQL for a single host's time series (used by the metrics dashboard)."""
    j = _job()
    sel = f'job="{j}",host="{_escape_label(host_name)}"'
    if metric == "cpu":
        return f'100 * (1 - avg by (host) (rate(node_cpu_seconds_total{{{sel},mode="idle"}}[5m])))'
    if metric == "mem":
        return (
            f'100 * (1 - node_memory_MemAvailable_bytes{{{sel}}} / '
            f'node_memory_MemTotal_bytes{{{sel}}})'
        )
    if metric == "disk":
        mp = _mountpoint_selector(disk_mountpoints)
        return (
            f'max by (host) (100 * (1 - '
            f'node_filesystem_avail_bytes{{{sel},fstype!~"{_FS_EXCLUDE}"{mp}}} / '
            f'node_filesystem_size_bytes{{{sel},fstype!~"{_FS_EXCLUDE}"{mp}}}))'
        )
    return None


async def host_up_map() -> dict[str, bool] | None:
    """Return {host_name: is_up} from Prometheus, or None if it is unreachable."""
    try:
        results = await prometheus_client.instant_query(_q_up())
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not query host up status: %s", exc)
        return None
    return {host: value >= 1.0 for host, value in _map_by_host(results).items()}


def _map_by_host(results: list[dict[str, Any]]) -> dict[str, float]:
    """Collapse instant-query results into {host_label: value}, dropping NaN."""
    out: dict[str, float] = {}
    for item in results:
        host = item.get("metric", {}).get("host")
        if not host:
            continue
        try:
            value = float(item.get("value", [0, "nan"])[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isnan(value):
            continue
        out[str(host)] = value
    return out


def _effective(override: float | None, default: float) -> float:
    return float(override) if override is not None else float(default)


# ── Evaluation ───────────────────────────────────────────────────────────────


async def evaluate_hosts(
    hosts: list[Any],
    thresholds: ServerThresholds,
) -> list[HostSignal]:
    """Query Prometheus once per signal and build per-host results.

    Returns an empty list (skip this cycle) if Prometheus is unreachable, so a
    transient outage never produces a false ``server_down`` for every host.
    """
    enabled = [h for h in hosts if getattr(h, "enabled", True)]
    if not enabled:
        return []

    forecast_on = thresholds.forecast_hours and thresholds.forecast_hours > 0
    try:
        up_map = _map_by_host(await prometheus_client.instant_query(_q_up()))
        disk_map = _map_by_host(await prometheus_client.instant_query(_q_disk_pct_for_hosts(enabled)))
        cpu_map = _map_by_host(await prometheus_client.instant_query(_q_cpu_pct()))
        mem_map = _map_by_host(await prometheus_client.instant_query(_q_mem_pct()))
        forecast_map: dict[str, float] = {}
        if forecast_on:
            forecast_map = _map_by_host(
                await prometheus_client.instant_query(
                    _q_disk_forecast_for_hosts(enabled, thresholds.forecast_hours * 3600.0)
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Server health check skipped (Prometheus query failed): %s", exc)
        return []

    signals: list[HostSignal] = []
    for host in enabled:
        name = host.name
        display = name

        # 1. Reachability. Absence from the up map == unreachable/not scraped.
        is_up = up_map.get(name, 0.0) >= 1.0
        signals.append(HostSignal(
            alert_type="server_down",
            target=name,
            display=display,
            is_healthy=is_up,
            severity="critical" if not is_up else None,
            value=None,
            threshold=None,
            message=(
                f"Server '{display}' is reachable again."
                if is_up else
                f"Server '{display}' is unreachable (node_exporter scrape is down)."
            ),
            monitor_label="서버 상태",
        ))
        # When a host is down its other series are stale/absent — skip them so a
        # single outage doesn't fan out into disk/cpu/mem alerts too.
        if not is_up:
            continue

        # 2. Disk usage (worst filesystem), two-level severity.
        disk_pct = disk_map.get(name)
        if disk_pct is not None:
            warn = _effective(host.disk_warn_pct, thresholds.disk_warn_pct)
            crit = _effective(host.disk_crit_pct, thresholds.disk_crit_pct)
            alert_threshold = min(warn, crit)
            if warn > crit:
                logger.warning(
                    "Invalid disk thresholds for host %s: warn %.1f > crit %.1f; "
                    "using %.1f for alert health",
                    name,
                    warn,
                    crit,
                    alert_threshold,
                )
            if disk_pct >= crit:
                severity: str | None = "critical"
            elif disk_pct >= warn:
                severity = "warning"
            else:
                severity = None
            signals.append(HostSignal(
                alert_type="server_disk",
                target=name,
                display=display,
                is_healthy=disk_pct < alert_threshold,
                severity=severity,
                value=disk_pct,
                threshold=alert_threshold,
                message=(
                    f"Server '{display}' disk usage is {disk_pct:.1f}% "
                    f"(warn {warn:.0f}% / crit {crit:.0f}%)."
                ),
                monitor_label="서버 디스크 사용률",
            ))

            # 3. Disk-fill forecast — proactive "will fill within N hours".
            if forecast_on:
                predicted = forecast_map.get(name)
                if predicted is not None:
                    will_fill = predicted < 0
                    signals.append(HostSignal(
                        alert_type="server_disk_forecast",
                        target=name,
                        display=display,
                        is_healthy=not will_fill,
                        severity="warning" if will_fill else None,
                        value=None,
                        threshold=thresholds.forecast_hours,
                        message=(
                            f"Server '{display}' disk is projected to fill within "
                            f"{thresholds.forecast_hours:.0f}h at the current rate."
                            if will_fill else
                            f"Server '{display}' disk fill projection cleared."
                        ),
                        monitor_label="서버 디스크 예측",
                    ))

        # 4. CPU utilisation.
        cpu_pct = cpu_map.get(name)
        if cpu_pct is not None:
            warn = _effective(host.cpu_warn_pct, thresholds.cpu_warn_pct)
            signals.append(HostSignal(
                alert_type="server_cpu",
                target=name,
                display=display,
                is_healthy=cpu_pct < warn,
                severity="warning" if cpu_pct >= warn else None,
                value=cpu_pct,
                threshold=warn,
                message=f"Server '{display}' CPU usage is {cpu_pct:.1f}% (threshold {warn:.0f}%).",
                monitor_label="서버 CPU 사용률",
            ))

        # 5. Memory utilisation.
        mem_pct = mem_map.get(name)
        if mem_pct is not None:
            warn = _effective(host.mem_warn_pct, thresholds.mem_warn_pct)
            signals.append(HostSignal(
                alert_type="server_mem",
                target=name,
                display=display,
                is_healthy=mem_pct < warn,
                severity="warning" if mem_pct >= warn else None,
                value=mem_pct,
                threshold=warn,
                message=f"Server '{display}' memory usage is {mem_pct:.1f}% (threshold {warn:.0f}%).",
                monitor_label="서버 메모리 사용률",
            ))

    return signals
