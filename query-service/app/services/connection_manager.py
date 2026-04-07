from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote_plus

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import settings
from app.models import DBConnection

logger = logging.getLogger(__name__)


def _get_fernet() -> Fernet:
    """Return a Fernet instance using the configured encryption key."""
    key = settings.ENCRYPTION_KEY
    # Pad or hash the key to make a valid Fernet key if needed
    if len(key) != 44:
        import base64
        import hashlib

        digest = hashlib.sha256(key.encode()).digest()
        key = base64.urlsafe_b64encode(digest).decode()
    return Fernet(key.encode())


def encrypt_password(plain: str) -> str:
    """Encrypt a plain-text password."""
    f = _get_fernet()
    return f.encrypt(plain.encode()).decode()


def decrypt_password(encrypted: str) -> str:
    """Decrypt an encrypted password."""
    f = _get_fernet()
    try:
        return f.decrypt(encrypted.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Failed to decrypt password. Check ENCRYPTION_KEY.") from exc


def _build_url(conn: DBConnection, password: str) -> str:
    """Build an async SQLAlchemy connection URL."""
    user = quote_plus(conn.username)
    pwd = quote_plus(password)

    if conn.db_type == "postgres":
        return f"postgresql+asyncpg://{user}:{pwd}@{conn.host}:{conn.port}/{conn.database}"
    elif conn.db_type == "mssql":
        return (
            f"mssql+aioodbc://{user}:{pwd}@{conn.host}:{conn.port}/{conn.database}"
            f"?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes"
        )
    else:
        raise ValueError(f"Unsupported db_type: {conn.db_type}")


class ConnectionManager:
    """Singleton that manages SQLAlchemy async engine pools per database alias."""

    _instance: ConnectionManager | None = None
    _engines: dict[str, AsyncEngine]
    _db_types: dict[str, str]

    def __new__(cls) -> ConnectionManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._engines = {}
            cls._instance._db_types = {}
        return cls._instance

    async def initialize(self, connections: list[DBConnection]) -> None:
        """Create engines for all saved connections on startup."""
        for conn in connections:
            try:
                await self.add_connection(conn)
            except Exception:
                logger.exception("Failed to initialize connection '%s'", conn.alias)

    async def add_connection(self, conn: DBConnection) -> None:
        """Create an async engine and register it under the given alias."""
        if conn.alias in self._engines:
            await self.remove_connection(conn.alias)

        password = decrypt_password(conn.password_encrypted)
        url = _build_url(conn, password)

        engine_kwargs: dict[str, Any] = {"echo": False}
        # SQLite (used for meta-DB) doesn't support pool_size, but target DBs always
        # use asyncpg or aioodbc which do.
        if conn.db_type in ("postgres", "mssql"):
            engine_kwargs["pool_size"] = conn.pool_size or 5
            engine_kwargs["max_overflow"] = conn.max_overflow or 3

        engine = create_async_engine(url, **engine_kwargs)
        self._engines[conn.alias] = engine
        self._db_types[conn.alias] = conn.db_type
        logger.info("Engine created for alias '%s' (%s)", conn.alias, conn.db_type)

    async def remove_connection(self, alias: str) -> None:
        """Dispose of the engine for the given alias and remove it."""
        engine = self._engines.pop(alias, None)
        self._db_types.pop(alias, None)
        if engine is not None:
            await engine.dispose()
            logger.info("Engine disposed for alias '%s'", alias)

    def get_engine(self, alias: str) -> AsyncEngine:
        """Return the engine for a given alias, or raise KeyError."""
        try:
            return self._engines[alias]
        except KeyError:
            raise KeyError(f"No engine registered for alias '{alias}'")

    def get_db_type(self, alias: str) -> str:
        """Return the database type for a given alias."""
        return self._db_types.get(alias, "unknown")

    async def test_connection(self, alias: str) -> bool:
        """Test connectivity by running SELECT 1."""
        try:
            engine = self.get_engine(alias)
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            logger.exception("Connection test failed for '%s'", alias)
            return False

    def get_status(self, alias: str) -> dict[str, Any]:
        """Return pool status info for the given alias."""
        engine = self._engines.get(alias)
        if engine is None:
            return {"alias": alias, "status": "not_registered"}

        pool = engine.pool
        return {
            "alias": alias,
            "status": "registered",
            "pool_size": getattr(pool, "size", None),
            "checked_in": getattr(pool, "checkedin", None),
            "checked_out": getattr(pool, "checkedout", None),
            "overflow": getattr(pool, "overflow", None),
        }

    def list_aliases(self) -> list[str]:
        """Return all registered aliases."""
        return list(self._engines.keys())

    async def dispose_all(self) -> None:
        """Dispose of all engines. Called on application shutdown."""
        for alias in list(self._engines.keys()):
            await self.remove_connection(alias)


# Module-level singleton
connection_manager = ConnectionManager()
