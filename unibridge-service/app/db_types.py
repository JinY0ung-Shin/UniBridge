"""Shared SQLAlchemy column types."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import DateTime, TypeDecorator


class UtcDateTime(TypeDecorator):
    """Timezone-aware datetime column that always stores/returns UTC.

    - Write: naive input is treated as UTC; aware input is normalized to UTC.
    - Read: naive DB value (legacy rows) is tagged as UTC; aware value is
      normalized to UTC.
    - Works on SQLite (TEXT) and PostgreSQL (timestamptz).
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self, value: Optional[datetime], dialect: Dialect
    ) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(
        self, value: Optional[datetime], dialect: Dialect
    ) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def utcnow() -> datetime:
    """Current moment as a UTC-aware datetime. Use as column default."""
    return datetime.now(timezone.utc)


__all__ = ["UtcDateTime", "utcnow"]
