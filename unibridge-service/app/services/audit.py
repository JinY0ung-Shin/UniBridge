from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app import metrics
from app.models import AuditLog

logger = logging.getLogger(__name__)


async def log_query(
    db: AsyncSession,
    *,
    user: str,
    database_alias: str,
    sql: str,
    params: dict[str, Any] | None = None,
    row_count: int | None = None,
    elapsed_ms: int | None = None,
    status: str,
    error_message: str | None = None,
) -> AuditLog:
    """Write an audit log entry for a query execution."""
    entry = AuditLog(
        user=user,
        database_alias=database_alias,
        sql=sql,
        params=json.dumps(params) if params else None,
        row_count=row_count,
        elapsed_ms=elapsed_ms,
        status=status,
        error_message=error_message,
    )
    db.add(entry)
    try:
        await db.commit()
    except Exception:
        metrics.record_audit_log_write(status="failure")
        raise

    metrics.record_audit_log_write(status="success")
    logger.info(
        "Audit: user=%s db=%s status=%s elapsed=%sms rows=%s",
        user,
        database_alias,
        status,
        elapsed_ms,
        row_count,
    )
    return entry
