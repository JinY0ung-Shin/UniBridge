"""Shared SQLAlchemy column types and the UTC helpers that go with them."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import DateTime, TypeDecorator

# Korea Standard Time as a fixed UTC+9 offset — Korea has no DST, so this needs
# no zoneinfo lookup and therefore no tzdata in the container image.
KST = timezone(timedelta(hours=9))


def as_utc(value: datetime) -> datetime:
    """Normalize to UTC, reading a naive value as UTC (SQLite reads back naive)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso_utc(moment: datetime) -> str:
    """``moment`` as second-precision ISO 8601 in UTC with a ``Z`` suffix.

    An aware value is converted; a naive one is read as local time, as
    :meth:`datetime.astimezone` does.
    """
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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
        return None if value is None else as_utc(value)

    def process_result_value(
        self, value: Optional[datetime], dialect: Dialect
    ) -> Optional[datetime]:
        return None if value is None else as_utc(value)


def utcnow() -> datetime:
    """Current moment as a UTC-aware datetime. Use as column default."""
    return datetime.now(timezone.utc)


__all__ = ["KST", "UtcDateTime", "as_utc", "iso_utc", "utcnow"]
