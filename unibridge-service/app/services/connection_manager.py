from __future__ import annotations

import asyncio
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


def validate_encryption_key() -> None:
    """Validate the encryption key at startup. Raises RuntimeError if invalid."""
    try:
        _get_fernet()
    except (ValueError, Exception) as exc:
        raise RuntimeError(
            "Invalid ENCRYPTION_KEY configuration. Cannot initialize encryption. "
            "Please check your ENCRYPTION_KEY environment variable."
        ) from exc


def _build_url(conn: DBConnection, password: str) -> str:
    """Build an async SQLAlchemy connection URL."""
    user = quote_plus(conn.username)
    pwd = quote_plus(password)

    if conn.db_type == "postgres":
        return f"postgresql+asyncpg://{user}:{pwd}@{conn.host}:{conn.port}/{conn.database}"
    elif conn.db_type == "mssql":
        trust_cert = settings.MSSQL_TRUST_SERVER_CERT
        return (
            f"mssql+aioodbc://{user}:{pwd}@{conn.host}:{conn.port}/{conn.database}"
            f"?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate={trust_cert}"
        )
    else:
        raise ValueError(f"Unsupported db_type: {conn.db_type}")


class ConnectionManager:
    """Singleton that manages database connection pools per alias.

    SQLAlchemy async engines are used for postgres/mssql.
    clickhouse-connect clients are used for ClickHouse.
    """

    _instance: ConnectionManager | None = None
    _engines: dict[str, AsyncEngine]
    _ch_clients: dict[str, Any]
    _db_types: dict[str, str]

    def __new__(cls) -> ConnectionManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._engines = {}
            cls._instance._ch_clients = {}
            cls._instance._db_types = {}
        return cls._instance

    async def initialize(self, connections: list[DBConnection]) -> None:
        """Create engines/clients for all saved connections on startup."""
        validate_encryption_key()
        for conn in connections:
            try:
                await self.add_connection(conn)
            except Exception:
                logger.exception("Failed to initialize connection '%s'", conn.alias)

    async def add_connection(self, conn: DBConnection) -> None:
        """Create an engine/client and register it under the given alias."""
        if conn.alias in self._engines or conn.alias in self._ch_clients:
            await self.remove_connection(conn.alias)

        password = decrypt_password(conn.password_encrypted)

        if conn.db_type == "clickhouse":
            import clickhouse_connect

            query_timeout = conn.query_timeout if conn.query_timeout is not None else 30
            client = await asyncio.to_thread(
                clickhouse_connect.get_client,
                host=conn.host,
                port=conn.port,
                username=conn.username,
                password=password,
                database=conn.database,
                interface=conn.protocol or "http",
                secure=conn.secure if conn.secure is not None else False,
                send_receive_timeout=query_timeout,
            )
            self._ch_clients[conn.alias] = client
        else:
            url = _build_url(conn, password)
            engine_kwargs: dict[str, Any] = {"echo": False}
            if conn.db_type in ("postgres", "mssql"):
                engine_kwargs["pool_size"] = conn.pool_size if conn.pool_size is not None else 5
                engine_kwargs["max_overflow"] = conn.max_overflow if conn.max_overflow is not None else 3
            engine = create_async_engine(url, **engine_kwargs)
            self._engines[conn.alias] = engine

        self._db_types[conn.alias] = conn.db_type
        logger.info("Connection created for alias '%s' (%s)", conn.alias, conn.db_type)

    async def remove_connection(self, alias: str) -> None:
        """Dispose of the connection for the given alias and remove it."""
        engine = self._engines.pop(alias, None)
        client = self._ch_clients.pop(alias, None)
        self._db_types.pop(alias, None)
        if engine is not None:
            await engine.dispose()
            logger.info("Engine disposed for alias '%s'", alias)
        if client is not None:
            await asyncio.to_thread(client.close)
            logger.info("ClickHouse client closed for alias '%s'", alias)

    def get_engine(self, alias: str) -> AsyncEngine:
        """Return the SQLAlchemy engine for a given alias, or raise KeyError."""
        try:
            return self._engines[alias]
        except KeyError:
            raise KeyError(f"No engine registered for alias '{alias}'")

    def get_clickhouse_client(self, alias: str) -> Any:
        """Return the ClickHouse client for a given alias, or raise KeyError."""
        try:
            return self._ch_clients[alias]
        except KeyError:
            raise KeyError(f"No ClickHouse client registered for alias '{alias}'")

    def get_db_type(self, alias: str) -> str:
        """Return the database type for a given alias."""
        return self._db_types.get(alias, "unknown")

    def has_connection(self, alias: str) -> bool:
        """Return True if the alias has a registered connection."""
        return alias in self._engines or alias in self._ch_clients

    async def test_connection(self, alias: str) -> tuple[bool, str]:
        """Test connectivity. Returns (ok, message)."""
        db_type = self._db_types.get(alias)
        try:
            if db_type == "clickhouse":
                client = self.get_clickhouse_client(alias)
                ok = await asyncio.to_thread(client.ping)
                if ok:
                    return True, "Connection successful"
                return False, "Ping failed"
            else:
                engine = self.get_engine(alias)
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                return True, "Connection successful"
        except Exception as exc:
            logger.exception("Connection test failed for '%s'", alias)
            return False, str(exc)

    def get_status(self, alias: str) -> dict[str, Any]:
        """Return status info for the given alias."""
        if alias in self._ch_clients:
            return {
                "alias": alias,
                "status": "registered",
                "driver": "clickhouse-connect",
            }

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
        return list(self._engines.keys()) + list(self._ch_clients.keys())

    async def dispose_all(self) -> None:
        """Dispose of all connections. Called on application shutdown."""
        for alias in list(self._engines.keys()) + list(self._ch_clients.keys()):
            await self.remove_connection(alias)


# Module-level singleton
connection_manager = ConnectionManager()
