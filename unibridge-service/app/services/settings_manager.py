"""In-memory settings manager synced with SystemConfig DB table."""
from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings as app_settings
from app.models import SystemConfig

logger = logging.getLogger(__name__)

# Integer settings, stored in SystemConfig under their attribute names.
_INT_KEYS = (
    "rate_limit_per_minute",
    "max_concurrent_queries",
    "default_row_limit",
    "query_route_timeout",
    "gateway_route_timeout",
)
# Log retention in days: integers that may not be negative.
_RETENTION_KEYS = (
    "audit_log_retention_days",
    "admin_audit_log_retention_days",
    "alert_history_retention_days",
)


class SettingsManager:
    """Manages runtime-configurable settings with DB persistence."""

    def __init__(self) -> None:
        self.rate_limit_per_minute: int = app_settings.RATE_LIMIT_PER_MINUTE
        self.max_concurrent_queries: int = app_settings.MAX_CONCURRENT_QUERIES
        self.default_row_limit: int = app_settings.DEFAULT_ROW_LIMIT
        # Gateway (APISIX) read/send timeout for the query route, in seconds.
        # Seeded from env; overridable at runtime via the settings UI, which also
        # live-patches the APISIX route.
        self.query_route_timeout: int = app_settings.APISIX_QUERY_ROUTE_TIMEOUT
        # Default gateway-route read/send timeout (seconds) for user-registered
        # routes that don't override it. Changing this re-applies to existing
        # non-override routes (see admin settings endpoint).
        self.gateway_route_timeout: int = app_settings.APISIX_GATEWAY_ROUTE_TIMEOUT
        self.blocked_sql_keywords: list[str] = []
        # Log retention, in days. 0 = disabled (keep forever), which is the
        # default so an upgrade never silently starts deleting history.
        self.audit_log_retention_days: int = 0
        self.admin_audit_log_retention_days: int = 0
        self.alert_history_retention_days: int = 0

    async def load_from_db(self, db: AsyncSession) -> None:
        """Load settings from SystemConfig table, falling back to defaults."""
        result = await db.execute(select(SystemConfig))
        rows = {row.key: row.value for row in result.scalars().all()}

        for key in (*_INT_KEYS, *_RETENTION_KEYS):
            if key not in rows:
                continue
            try:
                value = int(rows[key])
            except ValueError:
                logger.warning("Invalid %s in DB, using default", key)
                continue
            if key in _RETENTION_KEYS and value < 0:
                logger.warning("Negative %s in DB, using default", key)
                continue
            setattr(self, key, value)

        if "blocked_sql_keywords" in rows:
            try:
                parsed_keywords = json.loads(rows["blocked_sql_keywords"])
                if not isinstance(parsed_keywords, list) or not all(
                    isinstance(keyword, str) for keyword in parsed_keywords
                ):
                    raise TypeError("blocked_sql_keywords must be a list of strings")
                self.blocked_sql_keywords = parsed_keywords
            except (json.JSONDecodeError, TypeError):
                logger.warning("Invalid blocked_sql_keywords in DB, using default")

        logger.info(
            "Settings loaded: rate_limit=%d/min, max_concurrent=%d, default_row_limit=%d, "
            "query_route_timeout=%ds, blocked_keywords=%d",
            self.rate_limit_per_minute,
            self.max_concurrent_queries,
            self.default_row_limit,
            self.query_route_timeout,
            len(self.blocked_sql_keywords),
        )

    async def update(
        self,
        db: AsyncSession,
        rate_limit_per_minute: int | None = None,
        max_concurrent_queries: int | None = None,
        default_row_limit: int | None = None,
        query_route_timeout: int | None = None,
        gateway_route_timeout: int | None = None,
        blocked_sql_keywords: list[str] | None = None,
        audit_log_retention_days: int | None = None,
        admin_audit_log_retention_days: int | None = None,
        alert_history_retention_days: int | None = None,
    ) -> None:
        """Update settings in memory and persist to DB."""
        updates: dict[str, str] = {}

        for key, value in (
            ("rate_limit_per_minute", rate_limit_per_minute),
            ("max_concurrent_queries", max_concurrent_queries),
            ("default_row_limit", default_row_limit),
            ("query_route_timeout", query_route_timeout),
            ("gateway_route_timeout", gateway_route_timeout),
        ):
            if value is not None:
                setattr(self, key, value)
                updates[key] = str(value)

        if blocked_sql_keywords is not None:
            self.blocked_sql_keywords = blocked_sql_keywords
            updates["blocked_sql_keywords"] = json.dumps(blocked_sql_keywords)

        for key, value in (
            ("audit_log_retention_days", audit_log_retention_days),
            ("admin_audit_log_retention_days", admin_audit_log_retention_days),
            ("alert_history_retention_days", alert_history_retention_days),
        ):
            if value is None:
                continue
            if value < 0:
                raise ValueError(f"{key} must be >= 0")
            setattr(self, key, value)
            updates[key] = str(value)

        for key, value in updates.items():
            existing = await db.execute(
                select(SystemConfig).where(SystemConfig.key == key)
            )
            row = existing.scalar_one_or_none()
            if row is None:
                db.add(SystemConfig(key=key, value=value))
            else:
                row.value = value

        await db.commit()

    def get_all(self) -> dict:
        """Return all settings as a dict."""
        return {
            key: getattr(self, key)
            for key in (*_INT_KEYS, "blocked_sql_keywords", *_RETENTION_KEYS)
        }


# Module-level singleton
settings_manager = SettingsManager()
