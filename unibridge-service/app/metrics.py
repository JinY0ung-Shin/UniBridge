from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram
from sqlalchemy import text

logger = logging.getLogger(__name__)


@dataclass
class MetricsRecorder:
    query_duration: Histogram
    query_rows_returned: Histogram
    alert_dispatch_total: Counter
    audit_log_write_total: Counter
    connection_pool_in_use: Gauge
    meta_db_up: Gauge

    def record_query(
        self,
        *,
        db_alias: str,
        db_type: str,
        status: str,
        duration_seconds: float,
        row_count: int | None = None,
    ) -> None:
        self.query_duration.labels(
            db_alias=db_alias,
            db_type=db_type,
            status=status,
        ).observe(max(duration_seconds, 0.0))

        if row_count is not None:
            self.query_rows_returned.labels(
                db_alias=db_alias,
                db_type=db_type,
            ).observe(max(row_count, 0))

    def record_alert_dispatch(
        self,
        *,
        rule_id: int | str,
        channel_type: str,
        status: str,
    ) -> None:
        self.alert_dispatch_total.labels(
            rule_id=str(rule_id),
            channel_type=channel_type,
            status=status,
        ).inc()

    def record_audit_log_write(self, *, status: str) -> None:
        self.audit_log_write_total.labels(status=status).inc()

    def set_connection_pool_in_use(self, *, db_alias: str, in_use: int | float) -> None:
        self.connection_pool_in_use.labels(db_alias=db_alias).set(max(float(in_use), 0.0))

    def set_meta_db_up(self, is_up: bool) -> None:
        self.meta_db_up.set(1 if is_up else 0)


def create_metrics(
    *, registry: CollectorRegistry = REGISTRY,
) -> MetricsRecorder:
    return MetricsRecorder(
        query_duration=Histogram(
            "unibridge_query_duration_seconds",
            "Database query execution duration.",
            ["db_alias", "db_type", "status"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
            registry=registry,
        ),
        query_rows_returned=Histogram(
            "unibridge_query_rows_returned",
            "Rows returned by successful database queries.",
            ["db_alias", "db_type"],
            buckets=(0, 1, 5, 10, 25, 50, 100, 250, 500, 1000, 5000, 10000),
            registry=registry,
        ),
        alert_dispatch_total=Counter(
            "unibridge_alert_dispatch_total",
            "Alert dispatch attempts by rule, channel type, and status.",
            ["rule_id", "channel_type", "status"],
            registry=registry,
        ),
        audit_log_write_total=Counter(
            "unibridge_audit_log_write_total",
            "Audit log write attempts by status.",
            ["status"],
            registry=registry,
        ),
        connection_pool_in_use=Gauge(
            "unibridge_connection_pool_in_use",
            "Current checked-out SQLAlchemy connections by database alias.",
            ["db_alias"],
            registry=registry,
        ),
        meta_db_up=Gauge(
            "unibridge_meta_db_up",
            "Metadata database health status, 1 for up and 0 for down.",
            registry=registry,
        ),
    )


recorder = create_metrics()


def record_query(
    *,
    db_alias: str,
    db_type: str,
    status: str,
    duration_seconds: float,
    row_count: int | None = None,
) -> None:
    recorder.record_query(
        db_alias=db_alias,
        db_type=db_type,
        status=status,
        duration_seconds=duration_seconds,
        row_count=row_count,
    )


def record_alert_dispatch(
    *,
    rule_id: int | str,
    channel_type: str,
    status: str,
) -> None:
    recorder.record_alert_dispatch(
        rule_id=rule_id,
        channel_type=channel_type,
        status=status,
    )


def record_audit_log_write(*, status: str) -> None:
    recorder.record_audit_log_write(status=status)


def set_connection_pool_in_use(*, db_alias: str, in_use: int | float) -> None:
    recorder.set_connection_pool_in_use(db_alias=db_alias, in_use=in_use)


def set_meta_db_up(is_up: bool) -> None:
    recorder.set_meta_db_up(is_up)


async def monitor_meta_db_health(*, interval_seconds: int = 15) -> None:
    from app.database import async_session

    while True:
        try:
            async with async_session() as db:
                await db.execute(text("SELECT 1"))
            set_meta_db_up(True)
        except Exception:
            logger.exception("Metadata database health probe failed")
            set_meta_db_up(False)

        await asyncio.sleep(interval_seconds)
