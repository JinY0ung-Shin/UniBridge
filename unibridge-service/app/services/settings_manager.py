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

        if "blocked_sql_keywords" in rows:
            try:
                self.blocked_sql_keywords = json.loads(rows["blocked_sql_keywords"])
            except (json.JSONDecodeError, TypeError):
                logger.warning("Invalid blocked_sql_keywords in DB, using default")

        logger.info(
            "Settings loaded: rate_limit=%d/min, max_concurrent=%d, blocked_keywords=%d",
            self.rate_limit_per_minute,
            self.max_concurrent_queries,
            len(self.blocked_sql_keywords),
        )

    async def update(
        self,
        db: AsyncSession,
        rate_limit_per_minute: int | None = None,
        max_concurrent_queries: int | None = None,
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
            "blocked_sql_keywords": self.blocked_sql_keywords,
        }


# Module-level singleton
settings_manager = SettingsManager()
