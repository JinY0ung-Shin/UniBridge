# DB Security Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add table-level access control, rate limiting, concurrent query limits, and SQL keyword blacklist to the query-service.

**Architecture:** Four independent security features layered onto the existing query execution pipeline. Rate limiting and concurrent query control via FastAPI middleware; table access and SQL validation via service modules called from the query router. Runtime-configurable settings stored in a new `SystemConfig` model with in-memory cache.

**Tech Stack:** FastAPI, SQLAlchemy (async), asyncio.Semaphore, pytest, React + TypeScript + React Query

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `query-service/app/services/sql_validator.py` | SQL keyword blacklist checking |
| `query-service/app/services/table_access.py` | Table name extraction from SQL + whitelist validation |
| `query-service/app/services/settings_manager.py` | In-memory settings cache synced with SystemConfig DB table |
| `query-service/app/middleware/__init__.py` | Package init |
| `query-service/app/middleware/rate_limiter.py` | Rate limiting + concurrent query limiting middleware |
| `query-service/tests/test_sql_validator.py` | Tests for SQL validator |
| `query-service/tests/test_table_access.py` | Tests for table access service |
| `query-service/tests/test_rate_limiter.py` | Tests for rate limiter middleware |
| `query-service/tests/test_settings.py` | Tests for settings API + settings manager |
| `query-ui/src/pages/QuerySettings.tsx` | Admin page for rate limit / concurrent queries / blocked keywords |
| `query-ui/src/pages/QuerySettings.css` | Styles for QuerySettings page |

### Modified Files
| File | Changes |
|------|---------|
| `query-service/app/models.py` | Add `SystemConfig` model, add `allowed_tables` to `Permission` |
| `query-service/app/schemas.py` | Add `SystemConfigResponse`, `SystemConfigUpdate`, update `PermissionCreate`/`PermissionResponse` with `allowed_tables` |
| `query-service/app/config.py` | Add `RATE_LIMIT_PER_MINUTE`, `MAX_CONCURRENT_QUERIES` defaults |
| `query-service/app/main.py` | Register rate limiter middleware, load SystemConfig at startup |
| `query-service/app/routers/admin.py` | Add `GET /admin/query/databases/{alias}/tables`, `GET/PUT /admin/query/settings`, validate allowed_tables on permission upsert |
| `query-service/app/routers/query.py` | Add sql_validator + table_access checks before query execution |
| `query-service/app/auth.py` | Add `"query.settings.read"`, `"query.settings.write"` to `ALL_PERMISSIONS` |
| `query-service/app/database.py` | Add `"query.settings.read"`, `"query.settings.write"` to admin seed permissions |
| `query-ui/src/api/client.ts` | Add `getDbTables`, `getQuerySettings`, `updateQuerySettings` functions, update `Permission` type |
| `query-ui/src/pages/Permissions.tsx` | Add allowed_tables multi-select per permission row |
| `query-ui/src/components/Layout.tsx` | Add Query Settings nav item |
| `query-ui/src/App.tsx` | Add `/query-settings` route |
| `query-ui/src/locales/en.json` | Add i18n keys for new features |
| `query-ui/src/locales/ko.json` | Add i18n keys for new features |

---

### Task 1: Models + Config foundation

**Files:**
- Modify: `query-service/app/models.py:37-48` (Permission model)
- Modify: `query-service/app/models.py` (add SystemConfig after AuditLog)
- Modify: `query-service/app/config.py:11-12` (add new defaults)
- Modify: `query-service/app/schemas.py`

- [ ] **Step 1: Add `allowed_tables` to Permission model**

In `query-service/app/models.py`, add to the `Permission` class after `allow_delete`:

```python
allowed_tables = Column(Text, nullable=True)  # JSON array: ["users", "orders"], null = all
```

- [ ] **Step 2: Add SystemConfig model**

In `query-service/app/models.py`, add after the `AuditLog` class:

```python
class SystemConfig(Base):
    __tablename__ = "system_config"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
```

- [ ] **Step 3: Add config defaults**

In `query-service/app/config.py`, add after `DEFAULT_ROW_LIMIT`:

```python
RATE_LIMIT_PER_MINUTE: int = 60
MAX_CONCURRENT_QUERIES: int = 5
```

- [ ] **Step 4: Update Pydantic schemas**

In `query-service/app/schemas.py`, update `PermissionCreate` to add:

```python
allowed_tables: list[str] | None = None
```

Update `PermissionResponse` to add:

```python
allowed_tables: list[str] | None = None
```

Add new schemas after the Health section:

```python
# ── System Config ───────────────────────────────────────────────────────────

class SystemConfigResponse(BaseModel):
    rate_limit_per_minute: int
    max_concurrent_queries: int
    blocked_sql_keywords: list[str]


class SystemConfigUpdate(BaseModel):
    rate_limit_per_minute: int | None = Field(None, ge=1, le=1000)
    max_concurrent_queries: int | None = Field(None, ge=1, le=100)
    blocked_sql_keywords: list[str] | None = None
```

- [ ] **Step 5: Verify models load without errors**

Run: `cd /home/jinyoung/apihub/query-service && python -c "from app.models import SystemConfig, Permission; print('OK')"`

Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add query-service/app/models.py query-service/app/config.py query-service/app/schemas.py
git commit -m "feat: add SystemConfig model, allowed_tables field, and config defaults"
```

---

### Task 2: SQL Validator service

**Files:**
- Create: `query-service/app/services/sql_validator.py`
- Create: `query-service/tests/test_sql_validator.py`

- [ ] **Step 1: Write tests for SQL validator**

Create `query-service/tests/test_sql_validator.py`:

```python
"""Tests for SQL keyword blacklist validator."""
from __future__ import annotations

import pytest

from app.services.sql_validator import validate_sql


class TestValidateSql:
    """Test blocked keyword detection."""

    def test_allows_normal_select(self):
        errors = validate_sql("SELECT * FROM users")
        assert errors is None

    def test_allows_normal_insert(self):
        errors = validate_sql("INSERT INTO users (name) VALUES ('test')")
        assert errors is None

    def test_blocks_grant(self):
        errors = validate_sql("GRANT SELECT ON users TO public")
        assert errors is not None
        assert "GRANT" in errors

    def test_blocks_revoke(self):
        errors = validate_sql("REVOKE ALL ON users FROM public")
        assert errors is not None
        assert "REVOKE" in errors

    def test_blocks_create_user(self):
        errors = validate_sql("CREATE USER hacker WITH PASSWORD 'test'")
        assert errors is not None
        assert "CREATE USER" in errors

    def test_blocks_drop_user(self):
        errors = validate_sql("DROP USER admin")
        assert errors is not None
        assert "DROP USER" in errors

    def test_blocks_alter_user(self):
        errors = validate_sql("ALTER USER admin WITH SUPERUSER")
        assert errors is not None
        assert "ALTER USER" in errors

    def test_blocks_shutdown(self):
        errors = validate_sql("SHUTDOWN")
        assert errors is not None
        assert "SHUTDOWN" in errors

    def test_blocks_create_login(self):
        errors = validate_sql("CREATE LOGIN hacker WITH PASSWORD = 'test'")
        assert errors is not None
        assert "CREATE LOGIN" in errors

    def test_blocks_drop_login(self):
        errors = validate_sql("DROP LOGIN hacker")
        assert errors is not None
        assert "DROP LOGIN" in errors

    def test_blocks_backup(self):
        errors = validate_sql("BACKUP DATABASE mydb TO DISK = '/tmp/backup'")
        assert errors is not None
        assert "BACKUP" in errors

    def test_blocks_restore(self):
        errors = validate_sql("RESTORE DATABASE mydb FROM DISK = '/tmp/backup'")
        assert errors is not None
        assert "RESTORE" in errors

    def test_ignores_keyword_in_string(self):
        errors = validate_sql("SELECT * FROM users WHERE name = 'GRANT me access'")
        assert errors is None

    def test_ignores_keyword_in_comment(self):
        errors = validate_sql("SELECT * FROM users -- GRANT stuff\nWHERE id = 1")
        assert errors is None

    def test_ignores_keyword_in_block_comment(self):
        errors = validate_sql("SELECT * /* SHUTDOWN */ FROM users")
        assert errors is None

    def test_blocks_kill(self):
        errors = validate_sql("KILL 1234")
        assert errors is not None
        assert "KILL" in errors

    def test_custom_blocked_keywords(self):
        errors = validate_sql("VACUUM users", extra_blocked=["VACUUM"])
        assert errors is not None
        assert "VACUUM" in errors

    def test_custom_keyword_allows_normal(self):
        errors = validate_sql("SELECT * FROM users", extra_blocked=["VACUUM"])
        assert errors is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/jinyoung/apihub/query-service && python -m pytest tests/test_sql_validator.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.sql_validator'`

- [ ] **Step 3: Implement SQL validator**

Create `query-service/app/services/sql_validator.py`:

```python
"""SQL keyword blacklist validation.

Detects dangerous SQL keywords (GRANT, REVOKE, SHUTDOWN, etc.)
while ignoring keywords inside string literals and comments.
"""
from __future__ import annotations

import re

from app.services.query_executor import _strip_strings_and_comments

# Default blocked keywords — these are always blocked regardless of config.
# Single-word keywords use word-boundary matching.
# Multi-word keywords are matched as consecutive tokens.
_SINGLE_KEYWORDS = [
    "GRANT", "REVOKE", "SHUTDOWN", "KILL", "BACKUP", "RESTORE",
]

_MULTI_KEYWORDS = [
    "CREATE USER", "DROP USER", "ALTER USER",
    "CREATE LOGIN", "DROP LOGIN", "ALTER LOGIN",
]

_SINGLE_RE = re.compile(
    r"\b(" + "|".join(_SINGLE_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

_MULTI_RE = re.compile(
    r"\b(" + "|".join(kw.replace(" ", r"\s+") for kw in _MULTI_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def validate_sql(
    sql: str,
    extra_blocked: list[str] | None = None,
) -> str | None:
    """Check SQL for blocked keywords.

    Returns an error message string if a blocked keyword is found,
    or None if the SQL is clean.
    """
    cleaned = _strip_strings_and_comments(sql)

    # Check multi-word keywords first (more specific)
    match = _MULTI_RE.search(cleaned)
    if match:
        return f"Blocked SQL keyword: {match.group(0).upper()}"

    # Check single keywords
    match = _SINGLE_RE.search(cleaned)
    if match:
        return f"Blocked SQL keyword: {match.group(0).upper()}"

    # Check extra blocked keywords from config
    if extra_blocked:
        extra_pattern = re.compile(
            r"\b(" + "|".join(re.escape(kw) for kw in extra_blocked) + r")\b",
            re.IGNORECASE,
        )
        match = extra_pattern.search(cleaned)
        if match:
            return f"Blocked SQL keyword: {match.group(0).upper()}"

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/jinyoung/apihub/query-service && python -m pytest tests/test_sql_validator.py -v`

Expected: All 17 tests PASS

- [ ] **Step 5: Commit**

```bash
git add query-service/app/services/sql_validator.py query-service/tests/test_sql_validator.py
git commit -m "feat: add SQL keyword blacklist validator with tests"
```

---

### Task 3: Table Access service

**Files:**
- Create: `query-service/app/services/table_access.py`
- Create: `query-service/tests/test_table_access.py`

- [ ] **Step 1: Write tests for table name extraction**

Create `query-service/tests/test_table_access.py`:

```python
"""Tests for table-level access control."""
from __future__ import annotations

import pytest

from app.services.table_access import extract_tables, check_table_access


class TestExtractTables:
    """Test table name extraction from SQL."""

    def test_simple_select(self):
        tables = extract_tables("SELECT * FROM users")
        assert tables == {"users"}

    def test_select_with_schema(self):
        tables = extract_tables("SELECT * FROM public.users")
        assert tables == {"users"}

    def test_select_with_mssql_brackets(self):
        tables = extract_tables("SELECT * FROM [dbo].[users]")
        assert tables == {"users"}

    def test_join(self):
        tables = extract_tables("SELECT u.name FROM users u JOIN orders o ON u.id = o.user_id")
        assert tables == {"users", "orders"}

    def test_left_join(self):
        tables = extract_tables("SELECT * FROM users LEFT JOIN orders ON users.id = orders.user_id")
        assert tables == {"users", "orders"}

    def test_multiple_joins(self):
        tables = extract_tables(
            "SELECT * FROM users u "
            "INNER JOIN orders o ON u.id = o.user_id "
            "LEFT JOIN products p ON o.product_id = p.id"
        )
        assert tables == {"users", "orders", "products"}

    def test_insert_into(self):
        tables = extract_tables("INSERT INTO users (name) VALUES ('test')")
        assert tables == {"users"}

    def test_update(self):
        tables = extract_tables("UPDATE users SET name = 'test' WHERE id = 1")
        assert tables == {"users"}

    def test_delete_from(self):
        tables = extract_tables("DELETE FROM users WHERE id = 1")
        assert tables == {"users"}

    def test_subquery(self):
        tables = extract_tables(
            "SELECT * FROM users WHERE id IN (SELECT user_id FROM orders)"
        )
        assert tables == {"users", "orders"}

    def test_cte(self):
        tables = extract_tables(
            "WITH active AS (SELECT * FROM users WHERE active = true) "
            "SELECT * FROM active JOIN orders ON active.id = orders.user_id"
        )
        # CTE alias "active" should not be in the set; real tables should
        assert "users" in tables
        assert "orders" in tables

    def test_ignores_table_in_string(self):
        tables = extract_tables("SELECT * FROM users WHERE name = 'FROM orders'")
        assert tables == {"users"}

    def test_ignores_table_in_comment(self):
        tables = extract_tables("SELECT * FROM users -- FROM orders\nWHERE id = 1")
        assert tables == {"users"}

    def test_case_insensitive(self):
        tables = extract_tables("select * from Users")
        assert tables == {"users"}

    def test_cross_join(self):
        tables = extract_tables("SELECT * FROM users CROSS JOIN orders")
        assert tables == {"users", "orders"}

    def test_comma_separated_from(self):
        tables = extract_tables("SELECT * FROM users, orders WHERE users.id = orders.user_id")
        assert tables == {"users", "orders"}


class TestCheckTableAccess:
    """Test whitelist checking."""

    def test_all_allowed_returns_none(self):
        result = check_table_access({"users", "orders"}, ["users", "orders", "products"])
        assert result is None

    def test_denied_returns_error(self):
        result = check_table_access({"users", "secrets"}, ["users", "orders"])
        assert result is not None
        assert "secrets" in result

    def test_empty_allowed_denies_all(self):
        result = check_table_access({"users"}, [])
        assert result is not None

    def test_empty_tables_allowed(self):
        result = check_table_access(set(), ["users"])
        assert result is None

    def test_none_allowed_tables_allows_all(self):
        """When allowed_tables is None, all tables are allowed."""
        result = check_table_access({"users", "anything"}, None)
        assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/jinyoung/apihub/query-service && python -m pytest tests/test_table_access.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.table_access'`

- [ ] **Step 3: Implement table access service**

Create `query-service/app/services/table_access.py`:

```python
"""Table-level access control.

Extracts table names from SQL and validates them against a whitelist.
"""
from __future__ import annotations

import re

from app.services.query_executor import _strip_strings_and_comments

# Matches table references after FROM, JOIN, INTO, UPDATE keywords.
# Handles optional schema prefix: schema.table, [schema].[table]
# Also handles comma-separated tables in FROM clause.
_TABLE_RE = re.compile(
    r"""
    \b(?:FROM|JOIN|INTO|UPDATE)\s+      # keyword
    (?:\[?\w+\]?\.)?\s*                  # optional schema prefix
    (\[?\w+\]?)                          # table name (capture group 1)
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Matches additional comma-separated tables in FROM clause:
# FROM users, orders, products
_COMMA_TABLE_RE = re.compile(
    r"""
    \bFROM\s+                            # FROM keyword
    (
        (?:\[?\w+\]?\.)?\[?\w+\]?        # first table
        (?:\s*,\s*(?:\[?\w+\]?\.)?\[?\w+\]?)*  # comma-separated tables
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_INDIVIDUAL_TABLE_RE = re.compile(
    r"(?:\[?\w+\]?\.)?\s*(\[?\w+\]?)",
    re.IGNORECASE,
)


def _clean_name(name: str) -> str:
    """Remove brackets and lowercase a table name."""
    return name.strip("[]").lower()


def extract_tables(sql: str) -> set[str]:
    """Extract table names referenced in a SQL statement.

    Strips string literals and comments first to avoid false positives.
    Returns lowercase table names.
    """
    cleaned = _strip_strings_and_comments(sql)
    tables: set[str] = set()

    # 1. Extract FROM/JOIN/INTO/UPDATE tables
    for match in _TABLE_RE.finditer(cleaned):
        tables.add(_clean_name(match.group(1)))

    # 2. Handle comma-separated FROM: FROM users, orders
    for match in _COMMA_TABLE_RE.finditer(cleaned):
        table_list = match.group(1)
        for part in table_list.split(","):
            part = part.strip()
            if part:
                inner = _INDIVIDUAL_TABLE_RE.match(part)
                if inner:
                    tables.add(_clean_name(inner.group(1)))

    return tables


def check_table_access(
    referenced_tables: set[str],
    allowed_tables: list[str] | None,
) -> str | None:
    """Check if all referenced tables are in the whitelist.

    Args:
        referenced_tables: Set of table names found in the SQL.
        allowed_tables: List of allowed table names, or None to allow all.

    Returns:
        Error message string if access is denied, or None if allowed.
    """
    if allowed_tables is None:
        return None

    allowed_set = {t.lower() for t in allowed_tables}
    denied = referenced_tables - allowed_set
    if denied:
        denied_list = ", ".join(sorted(denied))
        return f"Access denied to table(s): {denied_list}"
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/jinyoung/apihub/query-service && python -m pytest tests/test_table_access.py -v`

Expected: All 17 tests PASS

- [ ] **Step 5: Commit**

```bash
git add query-service/app/services/table_access.py query-service/tests/test_table_access.py
git commit -m "feat: add table-level access control service with tests"
```

---

### Task 4: Settings Manager service

**Files:**
- Create: `query-service/app/services/settings_manager.py`
- Create: `query-service/tests/test_settings.py`

- [ ] **Step 1: Write tests for settings manager**

Create `query-service/tests/test_settings.py`:

```python
"""Tests for settings manager."""
from __future__ import annotations

import json
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base, SystemConfig
from app.services.settings_manager import SettingsManager


@pytest.fixture
async def settings_engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def settings_db(settings_engine):
    session_factory = async_sessionmaker(
        settings_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as session:
        yield session


@pytest.fixture
def manager():
    return SettingsManager()


class TestSettingsManager:
    async def test_defaults_when_empty(self, manager):
        """Should return config defaults when no DB overrides exist."""
        assert manager.rate_limit_per_minute == 60
        assert manager.max_concurrent_queries == 5
        assert manager.blocked_sql_keywords == []

    async def test_load_from_db(self, manager, settings_db):
        settings_db.add(SystemConfig(key="rate_limit_per_minute", value="100"))
        settings_db.add(SystemConfig(key="max_concurrent_queries", value="10"))
        settings_db.add(SystemConfig(
            key="blocked_sql_keywords",
            value=json.dumps(["VACUUM", "ANALYZE"]),
        ))
        await settings_db.commit()

        await manager.load_from_db(settings_db)

        assert manager.rate_limit_per_minute == 100
        assert manager.max_concurrent_queries == 10
        assert manager.blocked_sql_keywords == ["VACUUM", "ANALYZE"]

    async def test_update_setting(self, manager, settings_db):
        await manager.update(settings_db, rate_limit_per_minute=200)
        assert manager.rate_limit_per_minute == 200

        # Verify persisted to DB
        from sqlalchemy import select
        result = await settings_db.execute(
            select(SystemConfig).where(SystemConfig.key == "rate_limit_per_minute")
        )
        row = result.scalar_one()
        assert row.value == "200"

    async def test_update_blocked_keywords(self, manager, settings_db):
        await manager.update(settings_db, blocked_sql_keywords=["VACUUM"])
        assert manager.blocked_sql_keywords == ["VACUUM"]

    async def test_partial_update(self, manager, settings_db):
        """Updating one field should not affect others."""
        original_concurrent = manager.max_concurrent_queries
        await manager.update(settings_db, rate_limit_per_minute=999)
        assert manager.max_concurrent_queries == original_concurrent

    async def test_get_all(self, manager):
        result = manager.get_all()
        assert "rate_limit_per_minute" in result
        assert "max_concurrent_queries" in result
        assert "blocked_sql_keywords" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/jinyoung/apihub/query-service && python -m pytest tests/test_settings.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.settings_manager'`

- [ ] **Step 3: Implement settings manager**

Create `query-service/app/services/settings_manager.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/jinyoung/apihub/query-service && python -m pytest tests/test_settings.py -v`

Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add query-service/app/services/settings_manager.py query-service/tests/test_settings.py
git commit -m "feat: add settings manager with DB persistence and tests"
```

---

### Task 5: Rate Limiter middleware

**Files:**
- Create: `query-service/app/middleware/__init__.py`
- Create: `query-service/app/middleware/rate_limiter.py`
- Create: `query-service/tests/test_rate_limiter.py`

- [ ] **Step 1: Write tests for rate limiter**

Create `query-service/tests/test_rate_limiter.py`:

```python
"""Tests for rate limiting and concurrent query limiting."""
from __future__ import annotations

import asyncio
import pytest
import time

from app.middleware.rate_limiter import RateLimiter


@pytest.fixture
def limiter():
    return RateLimiter(rate_limit=5, max_concurrent=2)


class TestRateLimiter:
    def test_allows_under_limit(self, limiter):
        for _ in range(5):
            allowed, msg = limiter.check_rate_limit("user1")
            assert allowed is True

    def test_blocks_over_limit(self, limiter):
        for _ in range(5):
            limiter.check_rate_limit("user1")
        allowed, msg = limiter.check_rate_limit("user1")
        assert allowed is False
        assert "rate limit" in msg.lower()

    def test_separate_users(self, limiter):
        for _ in range(5):
            limiter.check_rate_limit("user1")
        # user2 should still be allowed
        allowed, _ = limiter.check_rate_limit("user2")
        assert allowed is True

    def test_concurrent_acquire_release(self, limiter):
        assert limiter.try_acquire("user1") is True
        assert limiter.try_acquire("user1") is True
        # Third should fail (max_concurrent=2)
        assert limiter.try_acquire("user1") is False
        # Release one, then it should work
        limiter.release("user1")
        assert limiter.try_acquire("user1") is True

    def test_concurrent_separate_users(self, limiter):
        assert limiter.try_acquire("user1") is True
        assert limiter.try_acquire("user1") is True
        # user2 should have their own limit
        assert limiter.try_acquire("user2") is True

    def test_expired_entries_cleaned(self, limiter):
        # Manually insert old timestamps
        old_time = time.time() - 120  # 2 minutes ago
        limiter._requests["user1"] = [old_time] * 5
        # Should be cleaned up on next check
        allowed, _ = limiter.check_rate_limit("user1")
        assert allowed is True

    def test_update_limits(self, limiter):
        limiter.update_limits(rate_limit=2, max_concurrent=1)
        limiter.check_rate_limit("user1")
        limiter.check_rate_limit("user1")
        allowed, _ = limiter.check_rate_limit("user1")
        assert allowed is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/jinyoung/apihub/query-service && python -m pytest tests/test_rate_limiter.py -v`

Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Create middleware package**

Create `query-service/app/middleware/__init__.py`:

```python
```

- [ ] **Step 4: Implement rate limiter**

Create `query-service/app/middleware/rate_limiter.py`:

```python
"""Rate limiting and concurrent query limiting middleware.

Applied only to /query/execute. Identifies users by decoding the JWT
from the Authorization header.
"""
from __future__ import annotations

import logging
import math
import threading
import time

from fastapi import Request, Response
from jose import jwt, JWTError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import settings as app_settings

logger = logging.getLogger(__name__)


class RateLimiter:
    """In-memory sliding window rate limiter + concurrent query tracker."""

    def __init__(self, rate_limit: int = 60, max_concurrent: int = 5) -> None:
        self._rate_limit = rate_limit
        self._max_concurrent = max_concurrent
        self._requests: dict[str, list[float]] = {}
        self._concurrent: dict[str, int] = {}
        self._lock = threading.Lock()

    def update_limits(self, rate_limit: int | None = None, max_concurrent: int | None = None) -> None:
        if rate_limit is not None:
            self._rate_limit = rate_limit
        if max_concurrent is not None:
            self._max_concurrent = max_concurrent

    def check_rate_limit(self, username: str) -> tuple[bool, str]:
        """Check if the user is within rate limits.

        Returns (allowed, message).
        """
        now = time.time()
        window_start = now - 60.0

        with self._lock:
            timestamps = self._requests.get(username, [])
            # Clean expired entries
            timestamps = [ts for ts in timestamps if ts > window_start]

            if len(timestamps) >= self._rate_limit:
                oldest = min(timestamps)
                retry_after = math.ceil(oldest + 60.0 - now)
                self._requests[username] = timestamps
                return False, f"Rate limit exceeded ({self._rate_limit}/min). Retry after {retry_after}s"

            timestamps.append(now)
            self._requests[username] = timestamps
            return True, ""

    def try_acquire(self, username: str) -> bool:
        """Try to acquire a concurrent query slot."""
        with self._lock:
            current = self._concurrent.get(username, 0)
            if current >= self._max_concurrent:
                return False
            self._concurrent[username] = current + 1
            return True

    def release(self, username: str) -> None:
        """Release a concurrent query slot."""
        with self._lock:
            current = self._concurrent.get(username, 0)
            if current > 0:
                self._concurrent[username] = current - 1


# Module-level singleton
rate_limiter = RateLimiter()


def _extract_username(request: Request) -> str | None:
    """Extract username from JWT in Authorization header."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return None

    token = auth[7:]
    try:
        # Try dev HS256 mode first
        payload = jwt.decode(
            token,
            app_settings.JWT_SECRET,
            algorithms=[app_settings.JWT_ALGORITHM],
            options={"verify_exp": False},
        )
        return payload.get("sub")
    except JWTError:
        try:
            # Fallback: decode without verification to get username
            payload = jwt.get_unverified_claims(token)
            return payload.get("sub") or payload.get("preferred_username")
        except Exception:
            return None


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Middleware that enforces rate limiting and concurrent query limits.

    Only applies to POST /query/execute (and paths beginning with that).
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # Only apply to query execution endpoint
        if request.method != "POST" or not request.url.path.rstrip("/").endswith("/query/execute"):
            return await call_next(request)

        username = _extract_username(request)
        if username is None:
            # No valid token — let the auth dependency handle rejection
            return await call_next(request)

        # 1. Check rate limit
        allowed, msg = rate_limiter.check_rate_limit(username)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": msg},
                headers={"Retry-After": str(60)},
            )

        # 2. Check concurrent query limit
        if not rate_limiter.try_acquire(username):
            return JSONResponse(
                status_code=429,
                content={"detail": f"Too many concurrent queries (max {rate_limiter._max_concurrent})"},
            )

        try:
            response = await call_next(request)
            return response
        finally:
            rate_limiter.release(username)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/jinyoung/apihub/query-service && python -m pytest tests/test_rate_limiter.py -v`

Expected: All 7 tests PASS

- [ ] **Step 6: Commit**

```bash
git add query-service/app/middleware/ query-service/tests/test_rate_limiter.py
git commit -m "feat: add rate limiting and concurrent query middleware with tests"
```

---

### Task 6: Admin API — tables + settings endpoints

**Files:**
- Modify: `query-service/app/routers/admin.py`
- Modify: `query-service/app/auth.py:26-42` (ALL_PERMISSIONS)
- Modify: `query-service/app/database.py:44-47` (admin seed)

- [ ] **Step 1: Add new permissions to ALL_PERMISSIONS**

In `query-service/app/auth.py`, add to the `ALL_PERMISSIONS` list after `"query.execute"`:

```python
"query.settings.read",
"query.settings.write",
```

- [ ] **Step 2: Add new permissions to admin role seed**

In `query-service/app/database.py`, the admin role already gets `ALL_PERMISSIONS` so it will automatically include the new ones. No change needed here.

- [ ] **Step 3: Add tables endpoint to admin router**

In `query-service/app/routers/admin.py`, add the following imports at the top (merge with existing imports):

```python
import json
from app.services.connection_manager import connection_manager, encrypt_password
from app.services.settings_manager import settings_manager
from app.services.table_access import check_table_access
```

Add this endpoint after the `test_connection` endpoint (after line 251):

```python
@router.get("/admin/query/databases/{alias}/tables")
async def list_tables(
    alias: str,
    _admin: CurrentUser = Depends(require_permission("query.databases.read")),
) -> list[str]:
    """List all table names in a registered database."""
    try:
        engine = connection_manager.get_engine(alias)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Database alias '{alias}' is not registered",
        )

    db_type = connection_manager.get_db_type(alias)

    if db_type == "mssql":
        sql = "SELECT TABLE_SCHEMA + '.' + TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_SCHEMA, TABLE_NAME"
    else:
        sql = "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public' ORDER BY tablename"

    from sqlalchemy import text
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(sql))
            return [row[0] for row in result.fetchall()]
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list tables: {exc}",
        )
```

- [ ] **Step 4: Update permission upsert to validate allowed_tables**

In `query-service/app/routers/admin.py`, modify the `upsert_permission` function. Replace the existing function with:

```python
@router.put("/admin/query/permissions", response_model=PermissionResponse)
async def upsert_permission(
    body: PermissionCreate,
    _admin: CurrentUser = Depends(require_permission("query.permissions.write")),
    db: AsyncSession = Depends(get_db),
) -> PermissionResponse:
    """Create or update a permission entry (upsert by role + db_alias)."""
    # Validate allowed_tables against actual DB tables
    if body.allowed_tables is not None and len(body.allowed_tables) > 0:
        try:
            engine = connection_manager.get_engine(body.db_alias)
            db_type = connection_manager.get_db_type(body.db_alias)

            if db_type == "mssql":
                sql = "SELECT TABLE_SCHEMA + '.' + TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE'"
            else:
                sql = "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public'"

            from sqlalchemy import text
            async with engine.connect() as conn:
                result = await conn.execute(text(sql))
                actual_tables = {row[0].lower() for row in result.fetchall()}

            requested = {t.lower() for t in body.allowed_tables}
            invalid = requested - actual_tables
            if invalid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Tables not found in database '{body.db_alias}': {', '.join(sorted(invalid))}",
                )
        except KeyError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Database alias '{body.db_alias}' is not registered or not connected",
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to validate tables: {exc}",
            )

    result = await db.execute(
        select(Permission).where(
            Permission.role == body.role,
            Permission.db_alias == body.db_alias,
        )
    )
    perm = result.scalar_one_or_none()

    allowed_tables_json = json.dumps(body.allowed_tables) if body.allowed_tables is not None else None

    if perm is None:
        perm = Permission(
            role=body.role,
            db_alias=body.db_alias,
            allow_select=body.allow_select,
            allow_insert=body.allow_insert,
            allow_update=body.allow_update,
            allow_delete=body.allow_delete,
            allowed_tables=allowed_tables_json,
        )
        db.add(perm)
    else:
        perm.allow_select = body.allow_select
        perm.allow_insert = body.allow_insert
        perm.allow_update = body.allow_update
        perm.allow_delete = body.allow_delete
        perm.allowed_tables = allowed_tables_json

    await db.commit()
    await db.refresh(perm)

    resp = PermissionResponse.model_validate(perm)
    resp.allowed_tables = json.loads(perm.allowed_tables) if perm.allowed_tables else None
    return resp
```

- [ ] **Step 5: Update list_permissions to deserialize allowed_tables**

In the `list_permissions` function, update the return to deserialize JSON:

```python
@router.get("/admin/query/permissions", response_model=list[PermissionResponse])
async def list_permissions(
    _admin: CurrentUser = Depends(require_permission("query.permissions.read")),
    db: AsyncSession = Depends(get_db),
) -> list[PermissionResponse]:
    """List all permission entries."""
    result = await db.execute(select(Permission))
    perms = []
    for p in result.scalars().all():
        resp = PermissionResponse.model_validate(p)
        resp.allowed_tables = json.loads(p.allowed_tables) if p.allowed_tables else None
        perms.append(resp)
    return perms
```

- [ ] **Step 6: Add settings endpoints**

In `query-service/app/routers/admin.py`, add after the audit logs section:

```python
# ── System Settings ─────────────────────────────────────────────────────────


@router.get("/admin/query/settings", response_model=SystemConfigResponse)
async def get_settings(
    _admin: CurrentUser = Depends(require_permission("query.settings.read")),
) -> SystemConfigResponse:
    """Get current system settings."""
    from app.schemas import SystemConfigResponse
    data = settings_manager.get_all()
    return SystemConfigResponse(**data)


@router.put("/admin/query/settings", response_model=SystemConfigResponse)
async def update_settings(
    body: SystemConfigUpdate,
    _admin: CurrentUser = Depends(require_permission("query.settings.write")),
    db: AsyncSession = Depends(get_db),
) -> SystemConfigResponse:
    """Update system settings."""
    from app.schemas import SystemConfigResponse
    from app.middleware.rate_limiter import rate_limiter

    await settings_manager.update(
        db,
        rate_limit_per_minute=body.rate_limit_per_minute,
        max_concurrent_queries=body.max_concurrent_queries,
        blocked_sql_keywords=body.blocked_sql_keywords,
    )

    # Sync rate limiter with new settings
    rate_limiter.update_limits(
        rate_limit=body.rate_limit_per_minute,
        max_concurrent=body.max_concurrent_queries,
    )

    data = settings_manager.get_all()
    return SystemConfigResponse(**data)
```

- [ ] **Step 7: Add schema imports to admin.py**

Add `SystemConfigUpdate` to the imports from `app.schemas`:

```python
from app.schemas import (
    AuditLogResponse,
    DBConnectionCreate,
    DBConnectionResponse,
    DBConnectionUpdate,
    PermissionCreate,
    PermissionResponse,
    SystemConfigResponse,
    SystemConfigUpdate,
)
```

- [ ] **Step 8: Run existing admin tests to check no regressions**

Run: `cd /home/jinyoung/apihub/query-service && python -m pytest tests/test_admin.py -v`

Expected: All existing tests PASS

- [ ] **Step 9: Commit**

```bash
git add query-service/app/auth.py query-service/app/routers/admin.py
git commit -m "feat: add tables listing, settings API, and allowed_tables validation"
```

---

### Task 7: Query route integration

**Files:**
- Modify: `query-service/app/routers/query.py`

- [ ] **Step 1: Add sql_validator and table_access checks**

In `query-service/app/routers/query.py`, add imports:

```python
import json
from app.services.sql_validator import validate_sql
from app.services.table_access import extract_tables, check_table_access
from app.services.settings_manager import settings_manager
```

In the `execute` function, add these checks after the permission check block (after line 67, before `# 3. Execute the query`):

```python
    # 2b. SQL keyword blacklist check
    blocked_error = validate_sql(req.sql, extra_blocked=settings_manager.blocked_sql_keywords)
    if blocked_error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=blocked_error,
        )

    # 2c. Table-level access check (only for non-admin users)
    if "query.databases.write" not in user_perms and perm is not None:
        allowed_tables_raw = perm.allowed_tables
        allowed_tables = json.loads(allowed_tables_raw) if allowed_tables_raw else None
        if allowed_tables is not None:
            referenced = extract_tables(req.sql)
            table_error = check_table_access(referenced, allowed_tables)
            if table_error:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=table_error,
                )
```

Note: The `perm` variable is already set in the permission check block above. For admin users (who have `query.databases.write`), both the permission check and table access check are bypassed.

- [ ] **Step 2: Adjust variable scoping**

The `perm` variable is currently only set inside the `if "query.databases.write" not in user_perms:` block. Initialize it before:

At the top of the `execute` function, after `statement_type = detect_statement_type(req.sql)` (line 42), add:

```python
    perm = None
```

Then inside the existing permission block, the `perm` assignment already happens at line 51. This makes `perm` available for the table access check.

- [ ] **Step 3: Run query tests to check no regressions**

Run: `cd /home/jinyoung/apihub/query-service && python -m pytest tests/test_query_roles.py -v`

Expected: All existing tests PASS

- [ ] **Step 4: Commit**

```bash
git add query-service/app/routers/query.py
git commit -m "feat: integrate SQL validator and table access checks into query execution"
```

---

### Task 8: Startup integration (main.py)

**Files:**
- Modify: `query-service/app/main.py`

- [ ] **Step 1: Register middleware and load settings at startup**

In `query-service/app/main.py`, add import:

```python
from app.middleware.rate_limiter import RateLimitMiddleware, rate_limiter
from app.services.settings_manager import settings_manager
```

In the `lifespan` function, after loading database connections (after line 38, before `yield`), add:

```python
    logger.info("Loading system settings...")
    async for db in get_db():
        await settings_manager.load_from_db(db)
        # Sync rate limiter with loaded settings
        rate_limiter.update_limits(
            rate_limit=settings_manager.rate_limit_per_minute,
            max_concurrent=settings_manager.max_concurrent_queries,
        )
        break
```

After `app.add_middleware(SecurityHeadersMiddleware)` (line 73), add:

```python
app.add_middleware(RateLimitMiddleware)
```

- [ ] **Step 2: Run full test suite**

Run: `cd /home/jinyoung/apihub/query-service && python -m pytest -v`

Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add query-service/app/main.py
git commit -m "feat: register rate limiter middleware and load settings at startup"
```

---

### Task 9: UI — API client updates

**Files:**
- Modify: `query-ui/src/api/client.ts`

- [ ] **Step 1: Update Permission type and add new API functions**

In `query-ui/src/api/client.ts`, update the `Permission` interface:

```typescript
export interface Permission {
  id?: number;
  role: string;
  db_alias: string;
  allow_select: boolean;
  allow_insert: boolean;
  allow_update: boolean;
  allow_delete: boolean;
  allowed_tables?: string[] | null;
}
```

Add new types after `AuditLogParams`:

```typescript
export interface QuerySettings {
  rate_limit_per_minute: number;
  max_concurrent_queries: number;
  blocked_sql_keywords: string[];
}

export interface QuerySettingsUpdate {
  rate_limit_per_minute?: number;
  max_concurrent_queries?: number;
  blocked_sql_keywords?: string[];
}
```

Add new API functions after the `deletePermission` function:

```typescript
export async function getDbTables(alias: string): Promise<string[]> {
  const { data } = await client.get(`/admin/query/databases/${alias}/tables`);
  return data;
}
```

Add after the Audit Logs section:

```typescript
/* ── Admin: Query Settings ── */

export async function getQuerySettings(): Promise<QuerySettings> {
  const { data } = await client.get('/admin/query/settings');
  return data;
}

export async function updateQuerySettings(body: QuerySettingsUpdate): Promise<QuerySettings> {
  const { data } = await client.put('/admin/query/settings', body);
  return data;
}
```

- [ ] **Step 2: Commit**

```bash
git add query-ui/src/api/client.ts
git commit -m "feat: add API client functions for tables, settings, and allowed_tables"
```

---

### Task 10: UI — Permissions page (allowed_tables multi-select)

**Files:**
- Modify: `query-ui/src/pages/Permissions.tsx`

- [ ] **Step 1: Add table selection to Permissions page**

Replace the entire `query-ui/src/pages/Permissions.tsx` with the updated version that includes:

1. A "Tables" column in the permissions table
2. When clicking "Edit Tables", a dropdown with checkboxes fetched from `getDbTables`
3. Saving updates via the existing `updatePermission` mutation

In `query-ui/src/pages/Permissions.tsx`, add `getDbTables` to imports:

```typescript
import {
  getPermissions,
  getAdminDatabases,
  getDbTables,
  updatePermission,
  deletePermission,
  type Permission,
} from '../api/client';
```

Add state for table editing after existing state declarations:

```typescript
const [editingTablesFor, setEditingTablesFor] = useState<string | null>(null); // "role:db_alias"
const [availableTables, setAvailableTables] = useState<string[]>([]);
const [selectedTables, setSelectedTables] = useState<string[]>([]);
const [tablesLoading, setTablesLoading] = useState(false);
```

Add a function to handle opening the table editor:

```typescript
async function handleEditTables(perm: Permission) {
  const key = `${perm.role}:${perm.db_alias}`;
  setEditingTablesFor(key);
  setSelectedTables(perm.allowed_tables ?? []);
  setTablesLoading(true);
  try {
    const tables = await getDbTables(perm.db_alias);
    setAvailableTables(tables);
  } catch {
    setAvailableTables([]);
  } finally {
    setTablesLoading(false);
  }
}

function handleToggleTable(table: string) {
  setSelectedTables((prev) =>
    prev.includes(table) ? prev.filter((t) => t !== table) : [...prev, table]
  );
}

function handleSaveTables(perm: Permission) {
  updateMut.mutate({
    ...perm,
    allowed_tables: selectedTables.length > 0 ? selectedTables : null,
  });
  setEditingTablesFor(null);
}

function handleCancelTables() {
  setEditingTablesFor(null);
}
```

Add a "Tables" column header after the OPERATIONS headers in the table:

```html
<th>{t('permissions.allowedTables')}</th>
```

Add a table cell for each permission row, after the OPERATIONS checkboxes and before the actions column:

```html
<td>
  {editingTablesFor === `${perm.role}:${perm.db_alias}` ? (
    <div className="table-selector">
      {tablesLoading ? (
        <span>{t('common.loading')}</span>
      ) : (
        <>
          <div className="table-checkboxes">
            {availableTables.map((table) => (
              <label key={table} className="table-checkbox-label">
                <input
                  type="checkbox"
                  checked={selectedTables.includes(table)}
                  onChange={() => handleToggleTable(table)}
                />
                <span>{table}</span>
              </label>
            ))}
            {availableTables.length === 0 && (
              <span className="hint">{t('permissions.noTablesFound')}</span>
            )}
          </div>
          <div className="table-selector-actions">
            <button className="btn btn-sm btn-primary" onClick={() => handleSaveTables(perm)}>
              {t('common.save')}
            </button>
            <button className="btn btn-sm" onClick={handleCancelTables}>
              {t('common.cancel')}
            </button>
          </div>
        </>
      )}
    </div>
  ) : (
    <div className="table-display">
      {perm.allowed_tables && perm.allowed_tables.length > 0 ? (
        <span className="table-tags">
          {perm.allowed_tables.map((t) => (
            <span key={t} className="table-tag">{t}</span>
          ))}
        </span>
      ) : (
        <span className="hint">{t('permissions.allTables')}</span>
      )}
      <button
        className="btn btn-sm btn-link"
        onClick={() => handleEditTables(perm)}
      >
        {t('common.edit')}
      </button>
    </div>
  )}
</td>
```

- [ ] **Step 2: Add CSS for table selector**

In `query-ui/src/pages/Permissions.css`, add:

```css
.table-selector {
  min-width: 200px;
}

.table-checkboxes {
  max-height: 200px;
  overflow-y: auto;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  padding: 8px;
  margin-bottom: 8px;
}

.table-checkbox-label {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 0;
  font-size: 13px;
  cursor: pointer;
}

.table-selector-actions {
  display: flex;
  gap: 6px;
}

.table-display {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.table-tags {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.table-tag {
  background: var(--color-bg-secondary, #f0f0f0);
  border-radius: 4px;
  padding: 2px 8px;
  font-size: 12px;
  font-family: monospace;
}
```

- [ ] **Step 3: Commit**

```bash
git add query-ui/src/pages/Permissions.tsx query-ui/src/pages/Permissions.css
git commit -m "feat: add table-level access control UI to Permissions page"
```

---

### Task 11: UI — Query Settings page

**Files:**
- Create: `query-ui/src/pages/QuerySettings.tsx`
- Create: `query-ui/src/pages/QuerySettings.css`
- Modify: `query-ui/src/App.tsx`
- Modify: `query-ui/src/components/Layout.tsx`

- [ ] **Step 1: Create QuerySettings page**

Create `query-ui/src/pages/QuerySettings.tsx`:

```tsx
import { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { getQuerySettings, updateQuerySettings, type QuerySettings } from '../api/client';
import './QuerySettings.css';

function QuerySettingsPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const settingsQuery = useQuery({
    queryKey: ['query-settings'],
    queryFn: getQuerySettings,
  });

  const [rateLimit, setRateLimit] = useState(60);
  const [maxConcurrent, setMaxConcurrent] = useState(5);
  const [blockedKeywords, setBlockedKeywords] = useState('');

  useEffect(() => {
    if (settingsQuery.data) {
      setRateLimit(settingsQuery.data.rate_limit_per_minute);
      setMaxConcurrent(settingsQuery.data.max_concurrent_queries);
      setBlockedKeywords(settingsQuery.data.blocked_sql_keywords.join(', '));
    }
  }, [settingsQuery.data]);

  const updateMut = useMutation({
    mutationFn: updateQuerySettings,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['query-settings'] });
    },
  });

  function handleSave() {
    const keywords = blockedKeywords
      .split(',')
      .map((k) => k.trim().toUpperCase())
      .filter((k) => k.length > 0);

    updateMut.mutate({
      rate_limit_per_minute: rateLimit,
      max_concurrent_queries: maxConcurrent,
      blocked_sql_keywords: keywords,
    });
  }

  return (
    <div className="query-settings">
      <div className="page-header">
        <h1>{t('querySettings.title')}</h1>
        <p className="page-subtitle">{t('querySettings.subtitle')}</p>
      </div>

      {settingsQuery.isLoading && <div className="loading-message">{t('common.loading')}</div>}

      {settingsQuery.isError && (
        <div className="error-banner">{t('querySettings.loadFailed')}</div>
      )}

      {settingsQuery.data && (
        <div className="settings-form">
          <div className="settings-card">
            <h3>{t('querySettings.rateLimiting')}</h3>
            <div className="form-group">
              <label>{t('querySettings.rateLimit')}</label>
              <input
                type="number"
                min={1}
                max={1000}
                value={rateLimit}
                onChange={(e) => setRateLimit(Number(e.target.value))}
              />
              <span className="form-hint">{t('querySettings.rateLimitHint')}</span>
            </div>
            <div className="form-group">
              <label>{t('querySettings.maxConcurrent')}</label>
              <input
                type="number"
                min={1}
                max={100}
                value={maxConcurrent}
                onChange={(e) => setMaxConcurrent(Number(e.target.value))}
              />
              <span className="form-hint">{t('querySettings.maxConcurrentHint')}</span>
            </div>
          </div>

          <div className="settings-card">
            <h3>{t('querySettings.sqlBlacklist')}</h3>
            <div className="form-group">
              <label>{t('querySettings.blockedKeywords')}</label>
              <input
                type="text"
                value={blockedKeywords}
                onChange={(e) => setBlockedKeywords(e.target.value)}
                placeholder="VACUUM, ANALYZE, ..."
              />
              <span className="form-hint">{t('querySettings.blockedKeywordsHint')}</span>
            </div>
          </div>

          <button
            className="btn btn-primary"
            onClick={handleSave}
            disabled={updateMut.isPending}
          >
            {updateMut.isPending ? t('common.saving') : t('common.save')}
          </button>

          {updateMut.isSuccess && (
            <span className="save-success">{t('querySettings.saved')}</span>
          )}
          {updateMut.isError && (
            <span className="save-error">{t('querySettings.saveFailed')}</span>
          )}
        </div>
      )}
    </div>
  );
}

export default QuerySettingsPage;
```

- [ ] **Step 2: Create QuerySettings CSS**

Create `query-ui/src/pages/QuerySettings.css`:

```css
.query-settings {
  max-width: 640px;
}

.settings-form {
  display: flex;
  flex-direction: column;
  gap: 24px;
}

.settings-card {
  background: var(--color-bg-primary, #fff);
  border: 1px solid var(--color-border, #e0e0e0);
  border-radius: 8px;
  padding: 20px;
}

.settings-card h3 {
  margin: 0 0 16px 0;
  font-size: 15px;
  font-weight: 600;
}

.form-group {
  margin-bottom: 16px;
}

.form-group label {
  display: block;
  font-size: 13px;
  font-weight: 500;
  margin-bottom: 4px;
}

.form-group input {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid var(--color-border, #d0d0d0);
  border-radius: 6px;
  font-size: 14px;
}

.form-hint {
  display: block;
  font-size: 12px;
  color: var(--color-text-secondary, #888);
  margin-top: 4px;
}

.save-success {
  color: var(--color-success, #22c55e);
  font-size: 13px;
  margin-left: 12px;
}

.save-error {
  color: var(--color-error, #ef4444);
  font-size: 13px;
  margin-left: 12px;
}
```

- [ ] **Step 3: Add route to App.tsx**

In `query-ui/src/App.tsx`, add import:

```typescript
import QuerySettings from './pages/QuerySettings';
```

Add route after the `/query` route:

```html
<Route path="/query-settings" element={<ProtectedRoute permission="query.settings.read"><QuerySettings /></ProtectedRoute>} />
```

- [ ] **Step 4: Add nav item to Layout.tsx**

In `query-ui/src/components/Layout.tsx`, add to the `navItems` array after the query playground entry:

```typescript
{ to: '/query-settings', labelKey: 'nav.querySettings', icon: 'Query Settings', section: 'data', permission: 'query.settings.read' },
```

Add the corresponding SVG icon in the nav rendering section (after the Query Playground icon block):

```html
{item.icon === 'Query Settings' && (
  <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
    <path d="M6.5 1h5l.4 2.1a6 6 0 011.3.7L15.3 3l2 2.6-1.7 1.4a6 6 0 010 1.4l1.7 1.6-2 2.6-2.1-.8a6 6 0 01-1.3.7L11.5 15h-5l-.4-2.1a6 6 0 01-1.3-.7L2.7 13 .7 10.4l1.7-1.4a6 6 0 010-1.4L.7 5.6l2-2.6 2.1.8a6 6 0 011.3-.7L6.5 1z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
    <circle cx="9" cy="8" r="2.5" stroke="currentColor" strokeWidth="1.2" />
  </svg>
)}
```

- [ ] **Step 5: Commit**

```bash
git add query-ui/src/pages/QuerySettings.tsx query-ui/src/pages/QuerySettings.css query-ui/src/App.tsx query-ui/src/components/Layout.tsx
git commit -m "feat: add Query Settings admin page for rate limits and SQL blacklist"
```

---

### Task 12: i18n translations

**Files:**
- Modify: `query-ui/src/locales/en.json`
- Modify: `query-ui/src/locales/ko.json`

- [ ] **Step 1: Add English translations**

In `query-ui/src/locales/en.json`, add to `nav`:

```json
"querySettings": "Query Settings"
```

Add new section:

```json
"querySettings": {
  "title": "Query Settings",
  "subtitle": "Configure query execution limits and SQL restrictions",
  "loadFailed": "Failed to load settings.",
  "rateLimiting": "Rate Limiting",
  "rateLimit": "Requests per minute (per user)",
  "rateLimitHint": "Maximum number of query requests a single user can make per minute",
  "maxConcurrent": "Max concurrent queries (per user)",
  "maxConcurrentHint": "Maximum number of queries a single user can run simultaneously",
  "sqlBlacklist": "SQL Keyword Blacklist",
  "blockedKeywords": "Additional blocked keywords",
  "blockedKeywordsHint": "Comma-separated list of SQL keywords to block (e.g. VACUUM, ANALYZE). Built-in blocks: GRANT, REVOKE, SHUTDOWN, KILL, etc.",
  "saved": "Settings saved.",
  "saveFailed": "Failed to save settings."
}
```

Add to `permissions`:

```json
"allowedTables": "Allowed Tables",
"allTables": "All tables",
"noTablesFound": "No tables found"
```

- [ ] **Step 2: Add Korean translations**

In `query-ui/src/locales/ko.json`, add to `nav`:

```json
"querySettings": "쿼리 설정"
```

Add new section:

```json
"querySettings": {
  "title": "쿼리 설정",
  "subtitle": "쿼리 실행 제한 및 SQL 제약 조건 설정",
  "loadFailed": "설정을 불러오지 못했습니다.",
  "rateLimiting": "요청 제한",
  "rateLimit": "분당 요청 수 (사용자별)",
  "rateLimitHint": "한 사용자가 분당 실행할 수 있는 최대 쿼리 요청 수",
  "maxConcurrent": "최대 동시 쿼리 수 (사용자별)",
  "maxConcurrentHint": "한 사용자가 동시에 실행할 수 있는 최대 쿼리 수",
  "sqlBlacklist": "SQL 키워드 블랙리스트",
  "blockedKeywords": "추가 차단 키워드",
  "blockedKeywordsHint": "차단할 SQL 키워드를 쉼표로 구분 (예: VACUUM, ANALYZE). 기본 차단: GRANT, REVOKE, SHUTDOWN, KILL 등",
  "saved": "설정이 저장되었습니다.",
  "saveFailed": "설정 저장에 실패했습니다."
}
```

Add to `permissions`:

```json
"allowedTables": "허용 테이블",
"allTables": "전체 허용",
"noTablesFound": "테이블을 찾을 수 없습니다"
```

- [ ] **Step 3: Commit**

```bash
git add query-ui/src/locales/en.json query-ui/src/locales/ko.json
git commit -m "feat: add i18n translations for query settings and table access control"
```

---

### Task 13: Final integration test

- [ ] **Step 1: Run full backend test suite**

Run: `cd /home/jinyoung/apihub/query-service && python -m pytest -v`

Expected: All tests PASS

- [ ] **Step 2: Run frontend build check**

Run: `cd /home/jinyoung/apihub/query-ui && npx tsc --noEmit`

Expected: No type errors

- [ ] **Step 3: Run frontend tests**

Run: `cd /home/jinyoung/apihub/query-ui && npm test -- --run`

Expected: All tests PASS

- [ ] **Step 4: Final commit if any fixes needed**

```bash
git add -A
git commit -m "fix: resolve integration issues from DB security hardening"
```
