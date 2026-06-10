"""Tests for per-user query history (/query/history) and saved queries (/query/saved)."""
from __future__ import annotations

import os
import tempfile

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AuditLog

from tests.conftest import auth_header


async def _seed_audit_logs(engine, rows: list[dict]) -> None:
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        for row in rows:
            db.add(AuditLog(**row))
        await db.commit()


def _audit_row(user: str, **overrides) -> dict:
    row = {
        "user": user,
        "database_alias": "test-db",
        "sql": "SELECT 1",
        "status": "success",
        "row_count": 1,
        "elapsed_ms": 5,
    }
    row.update(overrides)
    return row


# ── /query/history ───────────────────────────────────────────────────────────


async def test_history_requires_authentication(client):
    resp = await client.get("/query/history")
    assert resp.status_code == 401


async def test_history_rejects_apikey_consumer_header(client):
    # API-key callers (APISIX consumer header, no Bearer token) must not be
    # able to read history — the endpoint is JWT-only.
    resp = await client.get(
        "/query/history", headers={"X-Consumer-Username": "some-key"}
    )
    assert resp.status_code == 401


async def test_history_returns_only_own_rows_newest_first(client, seeded_db, admin_token):
    await _seed_audit_logs(
        seeded_db,
        [
            _audit_row("testadmin", sql="SELECT 1"),
            _audit_row("someone-else", sql="SELECT 'secret'"),
            _audit_row("testadmin", sql="SELECT 2", status="error", error_message="boom"),
        ],
    )

    resp = await client.get("/query/history", headers=auth_header(admin_token))
    assert resp.status_code == 200
    body = resp.json()

    assert body["total"] == 2
    sqls = [item["sql"] for item in body["items"]]
    assert sqls == ["SELECT 2", "SELECT 1"]  # newest first
    assert all(item["user"] == "testadmin" for item in body["items"])
    assert body["items"][0]["status"] == "error"
    assert body["items"][0]["error_message"] == "boom"


async def test_history_pagination_and_total(client, seeded_db, admin_token):
    await _seed_audit_logs(
        seeded_db, [_audit_row("testadmin", sql=f"SELECT {i}") for i in range(5)]
    )

    resp = await client.get(
        "/query/history",
        params={"limit": 2, "offset": 2},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert [item["sql"] for item in body["items"]] == ["SELECT 2", "SELECT 1"]


async def test_history_database_alias_filter(client, seeded_db, admin_token):
    await _seed_audit_logs(
        seeded_db,
        [
            _audit_row("testadmin", database_alias="db-a", sql="SELECT a"),
            _audit_row("testadmin", database_alias="db-b", sql="SELECT b"),
        ],
    )

    resp = await client.get(
        "/query/history",
        params={"database_alias": "db-b"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["sql"] == "SELECT b"
    assert body["items"][0]["database_alias"] == "db-b"


async def test_history_limit_is_capped_at_200(client, admin_token):
    resp = await client.get(
        "/query/history", params={"limit": 500}, headers=auth_header(admin_token)
    )
    assert resp.status_code == 422


# ── /query/saved CRUD ────────────────────────────────────────────────────────


async def test_saved_query_crud_roundtrip(client, user_token):
    headers = auth_header(user_token)

    # Create
    resp = await client.post(
        "/query/saved",
        json={
            "name": "My users",
            "database_alias": "test-db",
            "sql_text": "SELECT * FROM users",
            "description": "All users",
        },
        headers=headers,
    )
    assert resp.status_code == 201
    created = resp.json()
    assert created["name"] == "My users"
    assert created["database_alias"] == "test-db"
    assert created["sql_text"] == "SELECT * FROM users"
    assert created["description"] == "All users"
    saved_id = created["id"]

    # List
    resp = await client.get("/query/saved", headers=headers)
    assert resp.status_code == 200
    assert [item["id"] for item in resp.json()] == [saved_id]

    # Update (including clearing database_alias with explicit null)
    resp = await client.put(
        f"/query/saved/{saved_id}",
        json={"name": "Renamed", "database_alias": None, "sql_text": "SELECT 42"},
        headers=headers,
    )
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["name"] == "Renamed"
    assert updated["database_alias"] is None
    assert updated["sql_text"] == "SELECT 42"
    assert updated["description"] == "All users"  # untouched

    # Delete
    resp = await client.delete(f"/query/saved/{saved_id}", headers=headers)
    assert resp.status_code == 204

    resp = await client.get("/query/saved", headers=headers)
    assert resp.json() == []


async def test_saved_query_requires_authentication(client):
    assert (await client.get("/query/saved")).status_code == 401
    assert (
        await client.post("/query/saved", json={"name": "x", "sql_text": "SELECT 1"})
    ).status_code == 401


async def test_saved_query_rejects_apikey_consumer_header(client):
    resp = await client.get(
        "/query/saved", headers={"X-Consumer-Username": "some-key"}
    )
    assert resp.status_code == 401


async def test_saved_query_is_isolated_per_user(client, admin_token, user_token):
    resp = await client.post(
        "/query/saved",
        json={"name": "admin only", "sql_text": "SELECT 1"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 201
    saved_id = resp.json()["id"]

    other = auth_header(user_token)
    # Not visible in another user's list
    resp = await client.get("/query/saved", headers=other)
    assert resp.json() == []
    # Update / delete of someone else's row → 404 (no existence leak)
    resp = await client.put(
        f"/query/saved/{saved_id}", json={"name": "hijack"}, headers=other
    )
    assert resp.status_code == 404
    resp = await client.delete(f"/query/saved/{saved_id}", headers=other)
    assert resp.status_code == 404

    # Owner still has it
    resp = await client.get("/query/saved", headers=auth_header(admin_token))
    assert [item["name"] for item in resp.json()] == ["admin only"]


async def test_saved_query_validates_name_and_sql(client, user_token):
    headers = auth_header(user_token)

    resp = await client.post(
        "/query/saved", json={"name": "   ", "sql_text": "SELECT 1"}, headers=headers
    )
    assert resp.status_code == 422

    resp = await client.post(
        "/query/saved", json={"name": "ok", "sql_text": "   "}, headers=headers
    )
    assert resp.status_code == 422

    resp = await client.post(
        "/query/saved",
        json={"name": "too long", "sql_text": "x" * 100_001},
        headers=headers,
    )
    assert resp.status_code == 422


async def test_saved_query_missing_id_returns_404(client, user_token):
    headers = auth_header(user_token)
    resp = await client.put("/query/saved/9999", json={"name": "x"}, headers=headers)
    assert resp.status_code == 404
    resp = await client.delete("/query/saved/9999", headers=headers)
    assert resp.status_code == 404


# ── Migration 0012 round-trip ────────────────────────────────────────────────

ALEMBIC_INI = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "alembic.ini")
)


@pytest.fixture
def alembic_sqlite_url():
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    try:
        yield path
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _alembic_cfg(db_path: str) -> Config:
    cfg = Config(ALEMBIC_INI)
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


def test_migration_0012_upgrade_downgrade_roundtrip(alembic_sqlite_url):
    db_path = alembic_sqlite_url
    cfg = _alembic_cfg(db_path)
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    command.upgrade(cfg, "0012_saved_queries_history_idx")

    inspector = inspect(engine)
    assert "saved_queries" in inspector.get_table_names()
    saved_cols = {col["name"] for col in inspector.get_columns("saved_queries")}
    assert {
        "id", "owner", "name", "database_alias", "sql_text",
        "description", "created_at", "updated_at",
    } <= saved_cols
    assert {idx["name"] for idx in inspector.get_indexes("saved_queries")} >= {
        "ix_saved_queries_owner"
    }
    assert {idx["name"] for idx in inspector.get_indexes("audit_logs")} >= {
        "ix_audit_logs_user",
        "ix_audit_logs_timestamp",
        "ix_audit_logs_database_alias",
    }

    # Existing audit rows survive the index creation.
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO audit_logs (user, database_alias, sql, status) "
            "VALUES ('u', 'db', 'SELECT 1', 'success')"
        ))

    command.downgrade(cfg, "0011_apikey_expiry_write_perms")

    inspector = inspect(engine)
    assert "saved_queries" not in inspector.get_table_names()
    audit_index_names = {idx["name"] for idx in inspector.get_indexes("audit_logs")}
    assert not audit_index_names & {
        "ix_audit_logs_user",
        "ix_audit_logs_timestamp",
        "ix_audit_logs_database_alias",
    }
    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM audit_logs")).scalar_one()
    assert count == 1

    engine.dispose()
