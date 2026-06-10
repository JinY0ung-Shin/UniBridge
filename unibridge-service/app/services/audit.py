from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import metrics
from app.models import AdminAuditLog, AuditLog

logger = logging.getLogger(__name__)

# How many trailing characters of a secret to keep when masking, matching the
# gateway/api-key masking helpers (``"***" + value[-4:]``).
MASK_KEEP = 4

# Dict keys whose scalar value is always a secret, regardless of nesting depth.
_SENSITIVE_KEY_NAMES = frozenset(
    {
        "key",
        "secret",
        "password",
        "token",
        "authorization",
        "apikey",
        "api_key",
        "credential",
        "credentials",
        "header_value",  # service_keys[].header_value holds an upstream secret
        "access_key_id",  # S3 credentials
        "secret_access_key",
        "session_token",
    }
)


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

    session_factory = async_sessionmaker(db.bind, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as audit_db:
            audit_db.add(entry)
            await audit_db.commit()
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


def _mask_secret(value: Any) -> str:
    if isinstance(value, str) and len(value) > MASK_KEEP:
        return "***" + value[-MASK_KEEP:]
    return "***"


def _redact(value: Any, *, mask_all: bool = False, parent_key: str | None = None) -> Any:
    """Recursively redact secrets from a snapshot.

    A scalar is masked when it sits under a sensitive key name (see
    ``_SENSITIVE_KEY_NAMES``) or anywhere inside a proxy-rewrite ``headers.set``
    / ``headers.add`` map — APISIX stores injected service-key headers there
    with arbitrary (non-sensitive-looking) header names, so the whole subtree
    is treated as secret.
    """
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for k, v in value.items():
            key_l = k.lower() if isinstance(k, str) else k
            child_mask = (
                mask_all
                or (isinstance(key_l, str) and key_l in _SENSITIVE_KEY_NAMES)
                or (key_l in ("set", "add") and parent_key == "headers")
            )
            redacted[k] = _redact(v, mask_all=child_mask, parent_key=key_l)
        return redacted
    if isinstance(value, list):
        return [_redact(item, mask_all=mask_all, parent_key=parent_key) for item in value]
    if mask_all and value is not None:
        return _mask_secret(value)
    return value


def redact_snapshot(data: Any) -> Any:
    """Return a deep copy of ``data`` with secret values masked."""
    return _redact(data)


def _dump_snapshot(snapshot: dict[str, Any] | None) -> str | None:
    if snapshot is None:
        return None
    return json.dumps(redact_snapshot(snapshot), default=str, ensure_ascii=False)


async def log_admin_action(
    db: AsyncSession,
    *,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str,
    summary: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    status: str = "success",
    error_message: str | None = None,
) -> None:
    """Best-effort audit-log write for an administrative config change.

    Secrets in ``before``/``after`` are redacted before storage. The managed
    resource (APISIX route/upstream/consumer) has already been mutated by the
    time this is called, so a failure here is logged and swallowed rather than
    propagated — losing an audit row must not fail an action that already took
    effect.
    """
    try:
        entry = AdminAuditLog(
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            summary=summary,
            before=_dump_snapshot(before),
            after=_dump_snapshot(after),
            status=status,
            error_message=error_message,
        )
        session_factory = async_sessionmaker(
            db.bind, class_=AsyncSession, expire_on_commit=False
        )
        async with session_factory() as audit_db:
            audit_db.add(entry)
            await audit_db.commit()
    except Exception:
        metrics.record_audit_log_write(status="failure")
        logger.exception(
            "Failed to write admin audit log: actor=%s action=%s %s/%s",
            actor,
            action,
            resource_type,
            resource_id,
        )
        return

    metrics.record_audit_log_write(status="success")
    logger.info(
        "Admin audit: actor=%s action=%s resource=%s/%s status=%s",
        actor,
        action,
        resource_type,
        resource_id,
        status,
    )
