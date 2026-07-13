"""In-memory settings manager synced with SystemConfig DB table."""
from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings as app_settings
from app.models import SystemConfig

logger = logging.getLogger(__name__)


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

    async def load_from_db(self, db: AsyncSession) -> None:
        """Load settings from SystemConfig table, falling back to defaults."""
        result = await db.execute(select(SystemConfig))
        rows = {row.key: row.value for row in result.scalars().all()}

        if "rate_limit_per_minute" in rows:
            try:
                self.rate_limit_per_minute = int(rows["rate_limit_per_minute"])
            except ValueError:
                logger.warning("Invalid rate_limit_per_minute in DB, using default")

        if "max_concurrent_queries" in rows:
            try:
                self.max_concurrent_queries = int(rows["max_concurrent_queries"])
            except ValueError:
                logger.warning("Invalid max_concurrent_queries in DB, using default")

        if "default_row_limit" in rows:
            try:
                self.default_row_limit = int(rows["default_row_limit"])
            except ValueError:
                logger.warning("Invalid default_row_limit in DB, using default")

        if "query_route_timeout" in rows:
            try:
                self.query_route_timeout = int(rows["query_route_timeout"])
            except ValueError:
                logger.warning("Invalid query_route_timeout in DB, using default")

        if "gateway_route_timeout" in rows:
            try:
                self.gateway_route_timeout = int(rows["gateway_route_timeout"])
            except ValueError:
                logger.warning("Invalid gateway_route_timeout in DB, using default")

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
    ) -> None:
        """Update settings in memory and persist to DB."""
        updates: dict[str, str] = {}

        if rate_limit_per_minute is not None:
            self.rate_limit_per_minute = rate_limit_per_minute
            updates["rate_limit_per_minute"] = str(rate_limit_per_minute)

        if max_concurrent_queries is not None:
            self.max_concurrent_queries = max_concurrent_queries
            updates["max_concurrent_queries"] = str(max_concurrent_queries)

        if default_row_limit is not None:
            self.default_row_limit = default_row_limit
            updates["default_row_limit"] = str(default_row_limit)

        if query_route_timeout is not None:
            self.query_route_timeout = query_route_timeout
            updates["query_route_timeout"] = str(query_route_timeout)

        if gateway_route_timeout is not None:
            self.gateway_route_timeout = gateway_route_timeout
            updates["gateway_route_timeout"] = str(gateway_route_timeout)

        if blocked_sql_keywords is not None:
            self.blocked_sql_keywords = blocked_sql_keywords
            updates["blocked_sql_keywords"] = json.dumps(blocked_sql_keywords)

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
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "max_concurrent_queries": self.max_concurrent_queries,
            "default_row_limit": self.default_row_limit,
            "query_route_timeout": self.query_route_timeout,
            "gateway_route_timeout": self.gateway_route_timeout,
            "blocked_sql_keywords": self.blocked_sql_keywords,
        }


# Module-level singleton
settings_manager = SettingsManager()
