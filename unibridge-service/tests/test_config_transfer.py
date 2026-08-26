"""Tests for admin configuration export/import (/admin/config)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import create_token, invalidate_permission_cache
from app.models import (
    AdminAuditLog,
    AlertChannel,
    AlertSettings,
    DBConnection,
    MonitoredHost,
    MonitoredService,
    NASConnection,
    Permission,
    QueryTemplate,
    ResourceOwner,
    Role,
    RolePermission,
    S3Connection,
)
from app.routers.config_transfer import MAX_IMPORT_BYTES
from app.services.connection_manager import decrypt_password, encrypt_password
from app.services.settings_manager import settings_manager
from tests.conftest import auth_header

DB_PASSWORD = "super-secret-db-password"
S3_ACCESS_KEY = "AKIAEXAMPLEACCESSKEY"
S3_SECRET_KEY = "s3-secret-access-key-value"
CHANNEL_HEADER_SECRET = "Bearer channel-header-secret"
# A webhook URL is a credential in its own right: the token lives in the query.
CHANNEL_WEBHOOK_URL = "https://hooks.example.com/mail?token=channel-webhook-token"
ROUTE_SERVICE_KEY = "upstream-service-key-value"

PLACEHOLDER = "<excluded>"

# Runtime marker for the daily GPU report — never part of the portable config.
GPU_REPORT_MARKER = datetime(2026, 8, 20, 23, 5, tzinfo=timezone.utc)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def apisix_state():
    """In-memory stand-in for the APISIX admin API (routes + upstreams)."""
    state: dict[str, dict[str, dict]] = {"routes": {}, "upstreams": {}}

    async def list_resources(resource: str) -> dict:
        items = [dict(item) for item in state[resource].values()]
        return {"items": items, "total": len(items)}

    async def get_resource(resource: str, resource_id: str) -> dict:
        if resource_id not in state[resource]:
            raise RuntimeError(f"APISIX 404 not found: {resource}/{resource_id}")
        return dict(state[resource][resource_id])

    async def put_resource(resource: str, resource_id: str, body: dict) -> dict:
        stored = {**body, "id": resource_id}
        state[resource][resource_id] = stored
        return dict(stored)

    with patch("app.services.apisix_client.list_resources", new=AsyncMock(side_effect=list_resources)), \
         patch("app.services.apisix_client.get_resource", new=AsyncMock(side_effect=get_resource)), \
         patch("app.services.apisix_client.put_resource", new=AsyncMock(side_effect=put_resource)):
        yield state


@pytest.fixture(autouse=True)
def _isolate_side_effects():
    """Keep imports away from the file_sd writer and real backend clients."""
    with patch("app.services.server_monitor.sync_targets_from_db", new=AsyncMock()), \
         patch("app.services.server_monitor.sync_service_targets_from_db", new=AsyncMock()), \
         patch("app.services.connection_manager.connection_manager.add_connection", new=AsyncMock()), \
         patch("app.services.s3_manager.s3_manager.add_connection", new=AsyncMock()), \
         patch("app.services.nas_manager.nas_manager.add_connection", new=AsyncMock()):
        yield


@pytest.fixture
def restore_settings_manager():
    """Undo mutations to the process-wide settings singleton."""
    before = settings_manager.get_all()
    yield
    for key, value in before.items():
        setattr(settings_manager, key, value)


@pytest.fixture
async def config_writer_token(seeded_db):
    """A non-admin role holding only the config transfer permissions."""
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        role = Role(name="configwriter", description="Config transfer", is_system=False)
        db.add(role)
        await db.flush()
        for perm in ("admin.config.read", "admin.config.write"):
            db.add(RolePermission(role_id=role.id, permission=perm))
        await db.commit()
    await invalidate_permission_cache()
    return create_token("testconfigwriter", "configwriter")


def factory_for(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def export_doc(**sections) -> dict:
    return {
        "unibridge_export_version": 1,
        "exported_at": "2026-08-15T00:00:00+00:00",
        "sections": sections,
        "excluded": {},
    }


async def do_import(client, token, *, dry_run: bool, sections: list[str], doc: dict):
    return await client.post(
        "/admin/config/import",
        json={"dry_run": dry_run, "sections": sections, "data": doc},
        headers=auth_header(token),
    )


def rows_for(payload: dict, section: str) -> list[dict]:
    return [row for row in payload["results"] if row["section"] == section]


async def seed_full_config(engine) -> None:
    async with factory_for(engine)() as db:
        db.add(DBConnection(
            alias="analytics", db_type="postgres", host="db.internal", port=5432,
            database="analytics", username="reader",
            password_encrypted=encrypt_password(DB_PASSWORD),
            pool_size=5, max_overflow=3, query_timeout=30,
        ))
        db.add(S3Connection(
            alias="datalake", endpoint_url="https://s3.example.com", region="ap-northeast-2",
            access_key_id_encrypted=encrypt_password(S3_ACCESS_KEY),
            secret_access_key_encrypted=encrypt_password(S3_SECRET_KEY),
            default_bucket="raw", allowed_buckets=json.dumps(["raw", "curated"]), use_ssl=True,
        ))
        db.add(NASConnection(
            alias="share", base_path="/mnt/share", read_only=True,
            max_download_bytes=1024, show_hidden=False, follow_symlinks=False,
        ))
        role = Role(name="analyst", description="Read-only analyst", is_system=False)
        db.add(role)
        await db.flush()
        db.add(RolePermission(role_id=role.id, permission="query.execute"))
        db.add(Permission(
            role="analyst", db_alias="analytics", allow_select=True,
            allowed_tables=json.dumps(["orders"]),
        ))
        db.add(QueryTemplate(
            path="sales/daily", name="Daily sales", description="", db_alias="analytics",
            sql="SELECT 1", default_limit=100, timeout=30, enabled=True,
        ))
        db.add(MonitoredHost(
            name="web1", address="10.0.0.5:9100", description="edge",
            labels=json.dumps({"env": "prod"}), disk_mountpoints="/,/data",
            gpu_address="10.0.0.5:9400", disk_warn_pct=70.0, gpu_util_target_pct=25.0,
        ))
        db.add(MonitoredService(
            name="billing", address="10.0.0.9:8080", metrics_path="/actuator/prometheus",
            scheme="http", description="billing api",
        ))
        channel = AlertChannel(
            name="mail-a", webhook_url=CHANNEL_WEBHOOK_URL,
            payload_template="{}", headers=json.dumps({"Authorization": CHANNEL_HEADER_SECRET}),
        )
        db.add(channel)
        await db.flush()
        db.add(AlertSettings(
            id=1, mail_channel_id=channel.id, admin_emails=json.dumps(["ops@example.com"]),
            server_gpu_util_target_pct=35.0,
            server_gpu_report_last_sent_at=GPU_REPORT_MARKER,
        ))
        db.add(ResourceOwner(resource_type="db", resource_id="analytics", emails=json.dumps(["owner@example.com"])))
        await db.commit()


def seed_gateway(state: dict) -> None:
    state["upstreams"]["unibridge-service"] = {
        "id": "unibridge-service", "name": "unibridge-service", "type": "roundrobin",
        "nodes": {"unibridge-service:8000": 1},
    }
    state["upstreams"]["svc-a-up"] = {
        "id": "svc-a-up", "name": "svc-a-up", "type": "roundrobin",
        "nodes": {"svc-a:8080": 1}, "create_time": 1, "update_time": 2,
    }
    state["routes"]["query-api"] = {
        "id": "query-api", "name": "query-api", "uri": "/api/query/*",
        "upstream_id": "unibridge-service",
        "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": ["k1"]}},
    }
    state["routes"]["svc-a"] = {
        "id": "svc-a", "name": "service-a", "uri": "/api/svc-a/*",
        "upstream_id": "svc-a-up", "create_time": 1, "update_time": 2,
        "plugins": {
            "key-auth": {},
            "consumer-restriction": {"whitelist": ["consumer-1"]},
            "proxy-rewrite": {
                "regex_uri": ["^/api/svc-a(.*)", "$1"],
                "headers": {"set": {"X-Service-Key": ROUTE_SERVICE_KEY}},
            },
        },
    }


# ── Export ──────────────────────────────────────────────────────────────────


class TestExport:
    async def test_export_excludes_every_secret(self, client, admin_token, seeded_db, apisix_state):
        await seed_full_config(seeded_db)
        seed_gateway(apisix_state)

        resp = await client.get("/admin/config/export", headers=auth_header(admin_token))
        assert resp.status_code == 200, resp.text
        serialized = resp.text

        for secret in (
            DB_PASSWORD, S3_ACCESS_KEY, S3_SECRET_KEY,
            CHANNEL_HEADER_SECRET, CHANNEL_WEBHOOK_URL, ROUTE_SERVICE_KEY,
        ):
            assert secret not in serialized, f"{secret!r} leaked into the export"

        # The stored ciphertext must not travel either.
        async with factory_for(seeded_db)() as db:
            conn = (await db.execute(select(DBConnection))).scalar_one()
            s3 = (await db.execute(select(S3Connection))).scalar_one()
        assert conn.password_encrypted not in serialized
        assert s3.access_key_id_encrypted not in serialized
        assert s3.secret_access_key_encrypted not in serialized

        sections = resp.json()["sections"]
        assert "password" not in sections["db_connections"][0]
        assert sections["db_connections"][0]["secrets_excluded"] is True
        assert sections["s3_connections"][0]["secrets_excluded"] is True
        assert sections["s3_connections"][0]["allowed_buckets"] == ["raw", "curated"]
        # NAS connections hold no credentials, so nothing is dropped.
        assert "secrets_excluded" not in sections["nas_connections"][0]
        assert sections["alert_channels"][0]["headers"] == {"Authorization": PLACEHOLDER}
        assert sections["alert_channels"][0]["webhook_url"] == PLACEHOLDER
        assert sections["alert_channels"][0]["secrets_excluded"] is True
        assert "channel-webhook-token" not in serialized

    async def test_export_omits_builtin_routes_and_consumer_restriction(
        self, client, admin_token, seeded_db, apisix_state
    ):
        await seed_full_config(seeded_db)
        seed_gateway(apisix_state)

        resp = await client.get("/admin/config/export", headers=auth_header(admin_token))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        sections = body["sections"]

        assert [r["id"] for r in sections["routes"]] == ["svc-a"]
        assert [u["id"] for u in sections["upstreams"]] == ["svc-a-up"]
        assert "query-api" in body["excluded"]["builtin_routes"]
        assert "unibridge-service" in body["excluded"]["builtin_upstreams"]

        route = sections["routes"][0]
        assert "consumer-restriction" not in route["plugins"]
        assert route["plugins"]["proxy-rewrite"]["headers"]["set"] == {
            "X-Service-Key": PLACEHOLDER
        }
        assert route["secrets_excluded"] is True
        # APISIX-maintained timestamps are not part of the config.
        assert "create_time" not in route and "update_time" not in route
        assert "create_time" not in sections["upstreams"][0]

    async def test_export_section_contents(self, client, admin_token, seeded_db, apisix_state):
        await seed_full_config(seeded_db)
        resp = await client.get("/admin/config/export", headers=auth_header(admin_token))
        sections = resp.json()["sections"]

        assert {"name": "analyst", "description": "Read-only analyst", "is_system": False,
                "permissions": ["query.execute"]} in sections["roles"]
        assert sections["db_permissions"][0]["allowed_tables"] == ["orders"]
        assert sections["query_templates"][0]["path"] == "sales/daily"
        assert sections["monitored_hosts"][0]["gpu_address"] == "10.0.0.5:9400"
        assert sections["monitored_hosts"][0]["gpu_util_target_pct"] == 25.0
        assert sections["monitored_hosts"][0]["labels"] == {"env": "prod"}
        assert sections["monitored_services"][0]["metrics_path"] == "/actuator/prometheus"
        # The mail channel link travels by name, not by row id.
        assert sections["alert_settings"]["mail_channel_name"] == "mail-a"
        assert sections["alert_settings"]["admin_emails"] == ["ops@example.com"]
        assert sections["alert_settings"]["server_gpu_util_target_pct"] == 35.0
        # The daily-report marker is runtime state: exporting it would let an
        # import stamp another deployment's "already sent today" onto this one.
        assert "server_gpu_report_last_sent_at" not in sections["alert_settings"]
        assert sections["alert_recipients"][0]["emails"] == ["owner@example.com"]
        assert "rate_limit_per_minute" in sections["system_settings"]
        assert list(sections) == [
            "upstreams", "routes", "db_connections", "s3_connections", "nas_connections",
            "roles", "db_permissions", "query_templates", "monitored_hosts",
            "monitored_services", "alert_settings", "alert_channels", "alert_recipients",
            "system_settings",
        ]

    async def test_export_performs_no_db_writes(self, client, admin_token, seeded_db, apisix_state):
        """Export runs under a read-only permission, so it must not write.

        A fresh deployment has no alert_settings row; reading one must not
        create it.
        """
        executed: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):
            executed.append(statement.strip().upper())

        event.listen(seeded_db.sync_engine, "before_cursor_execute", record)
        try:
            resp = await client.get("/admin/config/export", headers=auth_header(admin_token))
        finally:
            event.remove(seeded_db.sync_engine, "before_cursor_execute", record)

        assert resp.status_code == 200, resp.text
        # The listener is wired (the export does read), and none of it wrote.
        assert any(s.startswith("SELECT") for s in executed)
        writes = [s for s in executed if s.startswith(("INSERT", "UPDATE", "DELETE"))]
        assert writes == []

        async with factory_for(seeded_db)() as db:
            assert (await db.execute(select(AlertSettings))).scalars().all() == []

        # The absent singleton still exports as the model's own defaults.
        settings_section = resp.json()["sections"]["alert_settings"]
        assert settings_section["mail_channel_id"] is None
        assert settings_section["mail_channel_name"] is None
        assert settings_section["admin_emails"] == []
        assert settings_section["route_error_threshold_pct"] == 10.0
        assert settings_section["check_interval_seconds"] == 60
        assert settings_section["server_disk_crit_pct"] == 90.0

    async def test_export_requires_permission(self, client, user_token, apisix_state):
        resp = await client.get("/admin/config/export", headers=auth_header(user_token))
        assert resp.status_code == 403

    async def test_export_reports_apisix_outage(self, client, admin_token, seeded_db):
        with patch(
            "app.services.apisix_client.list_resources",
            new=AsyncMock(side_effect=RuntimeError("connection refused")),
        ):
            resp = await client.get("/admin/config/export", headers=auth_header(admin_token))
        assert resp.status_code == 502


# ── Import: request validation ──────────────────────────────────────────────


class TestImportValidation:
    async def test_rejects_unsupported_version(self, client, admin_token, apisix_state):
        doc = export_doc(roles=[])
        doc["unibridge_export_version"] = 2
        resp = await do_import(client, admin_token, dry_run=True, sections=["roles"], doc=doc)
        assert resp.status_code == 422
        assert "version" in resp.json()["detail"].lower()

    async def test_rejects_missing_sections(self, client, admin_token, apisix_state):
        resp = await do_import(
            client, admin_token, dry_run=True, sections=["roles"],
            doc={"unibridge_export_version": 1, "exported_at": "x"},
        )
        assert resp.status_code == 422
        assert "sections" in resp.json()["detail"]

    async def test_rejects_unknown_section_name(self, client, admin_token, apisix_state):
        resp = await do_import(
            client, admin_token, dry_run=True, sections=["roles", "nope"], doc=export_doc(roles=[])
        )
        assert resp.status_code == 422
        assert "nope" in resp.json()["detail"]

    async def test_rejects_oversized_body(self, client, admin_token, apisix_state):
        payload = b'{"dry_run": true, "sections": [], "data": {"pad": "' + b"x" * (10 * 1024 * 1024 + 64) + b'"}}'
        resp = await client.post(
            "/admin/config/import",
            content=payload,
            headers={**auth_header(admin_token), "content-type": "application/json"},
        )
        assert resp.status_code == 422
        assert "10MB" in resp.json()["detail"]

    async def test_rejects_oversized_chunked_body(self, client, admin_token, apisix_state):
        """Without a Content-Length the cap has to fire mid-stream."""
        chunk = b"x" * (1024 * 1024)
        sent = 0

        async def body_stream():
            nonlocal sent
            yield b'{"dry_run": true, "sections": [], "data": {"pad": "'
            for _ in range(20):
                sent += len(chunk)
                yield chunk
            yield b'"}}'

        resp = await client.post(
            "/admin/config/import",
            content=body_stream(),
            headers={**auth_header(admin_token), "content-type": "application/json"},
        )
        assert resp.status_code == 422
        assert "10MB" in resp.json()["detail"]
        # Aborted as soon as the cap tripped instead of accumulating all 20MB.
        assert sent <= MAX_IMPORT_BYTES + len(chunk)

    async def test_requires_permission(self, client, user_token, apisix_state):
        resp = await do_import(client, user_token, dry_run=True, sections=["roles"], doc=export_doc(roles=[]))
        assert resp.status_code == 403

    async def test_requested_section_absent_from_file(self, client, admin_token, apisix_state):
        resp = await do_import(
            client, admin_token, dry_run=True, sections=["roles", "routes"], doc=export_doc(roles=[])
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert rows_for(body, "routes") == [
            {"section": "routes", "name": "-", "action": "skip", "reason": "section not in file"}
        ]
        assert body["summary"]["skip"] == 1


# ── Import: dry run ─────────────────────────────────────────────────────────


class TestDryRun:
    @staticmethod
    def sample_doc() -> dict:
        return export_doc(
            roles=[{"name": "analyst", "description": "d", "is_system": False, "permissions": ["query.execute"]}],
            monitored_hosts=[{"name": "web1", "address": "10.0.0.5:9100", "enabled": True,
                              "description": "", "labels": None, "disk_mountpoints": None,
                              "gpu_address": None, "disk_warn_pct": None, "disk_crit_pct": None,
                              "cpu_warn_pct": None, "mem_warn_pct": None,
                              "gpu_util_warn_pct": None, "gpu_mem_warn_pct": None,
                              "gpu_util_target_pct": None}],
            routes=[{"id": "svc-a", "name": "service-a", "uri": "/api/svc-a/*",
                     "upstream_id": "svc-a-up", "plugins": {"key-auth": {}}}],
        )

    async def test_dry_run_plans_without_writing(self, client, admin_token, seeded_db, apisix_state):
        sections = ["roles", "monitored_hosts", "routes"]
        resp = await do_import(client, admin_token, dry_run=True, sections=sections, doc=self.sample_doc())
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["dry_run"] is True
        assert body["summary"] == {"create": 3, "update": 0, "skip": 0, "error": 0}
        assert {row["action"] for row in body["results"]} == {"create"}

        async with factory_for(seeded_db)() as db:
            assert (await db.execute(select(Role).where(Role.name == "analyst"))).scalar_one_or_none() is None
            assert (await db.execute(select(MonitoredHost))).scalars().all() == []
            assert (await db.execute(select(AdminAuditLog))).scalars().all() == []
        assert apisix_state["routes"] == {}

    async def test_dry_run_reports_validation_errors_without_writing(
        self, client, admin_token, seeded_db, apisix_state
    ):
        doc = export_doc(monitored_hosts=[{"name": "web1", "address": ""}])
        resp = await do_import(client, admin_token, dry_run=True, sections=["monitored_hosts"], doc=doc)
        assert resp.status_code == 200, resp.text
        row = rows_for(resp.json(), "monitored_hosts")[0]
        assert row["action"] == "error"
        assert "address" in row["reason"]

        async with factory_for(seeded_db)() as db:
            assert (await db.execute(select(MonitoredHost))).scalars().all() == []


# ── Import: apply ───────────────────────────────────────────────────────────


class TestApply:
    async def test_creates_then_updates(self, client, admin_token, seeded_db, apisix_state):
        doc = TestDryRun.sample_doc()
        sections = ["roles", "monitored_hosts", "routes"]

        resp = await do_import(client, admin_token, dry_run=False, sections=sections, doc=doc)
        assert resp.status_code == 200, resp.text
        assert resp.json()["summary"] == {"create": 3, "update": 0, "skip": 0, "error": 0}

        async with factory_for(seeded_db)() as db:
            role = (await db.execute(select(Role).where(Role.name == "analyst"))).scalar_one()
            grants = (await db.execute(
                select(RolePermission.permission).where(RolePermission.role_id == role.id)
            )).scalars().all()
            host = (await db.execute(select(MonitoredHost))).scalar_one()
        assert list(grants) == ["query.execute"]
        assert host.address == "10.0.0.5:9100"
        assert apisix_state["routes"]["svc-a"]["uri"] == "/api/svc-a/*"

        # Replaying the same document updates in place.
        doc["sections"]["monitored_hosts"][0]["address"] = "10.0.0.6:9100"
        resp = await do_import(client, admin_token, dry_run=False, sections=sections, doc=doc)
        assert resp.json()["summary"] == {"create": 0, "update": 3, "skip": 0, "error": 0}

        async with factory_for(seeded_db)() as db:
            hosts = (await db.execute(select(MonitoredHost))).scalars().all()
        assert [h.address for h in hosts] == ["10.0.0.6:9100"]

    async def test_duplicate_permissions_in_a_role_are_deduplicated(
        self, client, admin_token, seeded_db, apisix_state
    ):
        doc = export_doc(roles=[{
            "name": "analyst", "description": "d", "is_system": False,
            "permissions": ["query.execute", "query.execute", "dashboard.read", "query.execute"],
        }])
        resp = await do_import(client, admin_token, dry_run=False, sections=["roles"], doc=doc)
        assert resp.status_code == 200, resp.text
        row = rows_for(resp.json(), "roles")[0]
        assert (row["action"], row["reason"]) == ("create", None)

        async with factory_for(seeded_db)() as db:
            role = (await db.execute(select(Role).where(Role.name == "analyst"))).scalar_one()
            grants = (await db.execute(
                select(RolePermission.permission).where(RolePermission.role_id == role.id)
            )).scalars().all()
        assert sorted(grants) == ["dashboard.read", "query.execute"]

    async def test_applies_connections_templates_and_alerting(
        self, client, admin_token, seeded_db, apisix_state
    ):
        await seed_full_config(seeded_db)
        doc = export_doc(
            db_connections=[{"alias": "analytics", "db_type": "postgres", "host": "new-db.internal",
                             "port": 5433, "database": "analytics", "username": "reader",
                             "protocol": None, "secure": None, "pool_size": 7,
                             "max_overflow": 3, "query_timeout": 45, "secrets_excluded": True}],
            nas_connections=[{"alias": "share2", "base_path": "/mnt/share2", "read_only": True,
                              "max_download_bytes": 2048, "show_hidden": True, "follow_symlinks": False}],
            query_templates=[{"path": "sales/daily", "name": "Daily sales v2", "description": "",
                              "database": "analytics", "sql": "SELECT 2", "default_limit": 50,
                              "timeout": 20, "enabled": True}],
            alert_recipients=[{"resource_type": "s3", "resource_id": "datalake",
                               "emails": ["dl@example.com"], "alerts_enabled": False}],
        )
        sections = ["db_connections", "nas_connections", "query_templates", "alert_recipients"]
        resp = await do_import(client, admin_token, dry_run=False, sections=sections, doc=doc)
        assert resp.status_code == 200, resp.text
        assert resp.json()["summary"]["error"] == 0

        async with factory_for(seeded_db)() as db:
            conn = (await db.execute(select(DBConnection))).scalar_one()
            nas = (await db.execute(
                select(NASConnection).where(NASConnection.alias == "share2")
            )).scalar_one()
            template = (await db.execute(select(QueryTemplate))).scalar_one()
            owner = (await db.execute(
                select(ResourceOwner).where(ResourceOwner.resource_type == "s3")
            )).scalar_one()

        assert (conn.host, conn.port, conn.pool_size, conn.query_timeout) == (
            "new-db.internal", 5433, 7, 45
        )
        # The stored credential is untouched by an import.
        assert decrypt_password(conn.password_encrypted) == DB_PASSWORD
        assert (nas.base_path, nas.show_hidden) == ("/mnt/share2", True)
        assert (template.name, template.sql) == ("Daily sales v2", "SELECT 2")
        assert json.loads(owner.emails) == ["dl@example.com"]
        assert owner.alerts_enabled is False

    async def test_relinks_mail_channel_by_name(self, client, admin_token, seeded_db, apisix_state):
        await seed_full_config(seeded_db)
        doc = export_doc(
            alert_channels=[{"name": "mail-b", "webhook_url": "https://hooks.example.com/b",
                             "payload_template": "{}", "recipient_item_template": None,
                             "headers": None, "enabled": True}],
            alert_settings={"mail_channel_id": 999, "mail_channel_name": "mail-b",
                            "admin_emails": ["new@example.com"], "check_interval_seconds": 120},
        )
        resp = await do_import(
            client, admin_token, dry_run=False,
            sections=["alert_channels", "alert_settings"], doc=doc,
        )
        assert resp.status_code == 200, resp.text

        async with factory_for(seeded_db)() as db:
            channel = (await db.execute(
                select(AlertChannel).where(AlertChannel.name == "mail-b")
            )).scalar_one()
            settings_row = (await db.execute(select(AlertSettings))).scalar_one()
        assert settings_row.mail_channel_id == channel.id
        assert json.loads(settings_row.admin_emails) == ["new@example.com"]
        assert settings_row.check_interval_seconds == 120

    async def test_unresolvable_mail_channel_leaves_link_alone(
        self, client, admin_token, seeded_db, apisix_state
    ):
        await seed_full_config(seeded_db)
        async with factory_for(seeded_db)() as db:
            before = (await db.execute(select(AlertSettings))).scalar_one().mail_channel_id

        doc = export_doc(alert_settings={"mail_channel_id": 42, "mail_channel_name": "ghost",
                                         "check_interval_seconds": 90})
        resp = await do_import(client, admin_token, dry_run=False, sections=["alert_settings"], doc=doc)
        row = rows_for(resp.json(), "alert_settings")[0]
        assert row["action"] == "update"
        assert "ghost" in row["reason"]

        async with factory_for(seeded_db)() as db:
            settings_row = (await db.execute(select(AlertSettings))).scalar_one()
        assert settings_row.mail_channel_id == before
        assert settings_row.check_interval_seconds == 90

    async def test_imports_gpu_report_targets_and_keeps_the_marker(
        self, client, admin_token, seeded_db, apisix_state
    ):
        await seed_full_config(seeded_db)
        doc = export_doc(
            monitored_hosts=[{"name": "web1", "address": "10.0.0.5:9100",
                              "gpu_address": "10.0.0.5:9400", "gpu_util_target_pct": 45.0}],
            alert_settings={"server_gpu_util_target_pct": 15.0},
        )
        resp = await do_import(
            client, admin_token, dry_run=False,
            sections=["monitored_hosts", "alert_settings"], doc=doc,
        )
        assert resp.status_code == 200, resp.text

        async with factory_for(seeded_db)() as db:
            host = (await db.execute(
                select(MonitoredHost).where(MonitoredHost.name == "web1")
            )).scalar_one()
            settings_row = (await db.execute(select(AlertSettings))).scalar_one()
        assert host.gpu_util_target_pct == 45.0
        assert settings_row.server_gpu_util_target_pct == 15.0
        # Importing config must not stamp another deployment's run history here.
        assert settings_row.server_gpu_report_last_sent_at == GPU_REPORT_MARKER

    async def test_applies_system_settings(
        self, client, admin_token, seeded_db, apisix_state, restore_settings_manager
    ):
        doc = export_doc(system_settings={"rate_limit_per_minute": 42, "default_row_limit": 777,
                                          "unknown_future_key": 1})
        resp = await do_import(client, admin_token, dry_run=False, sections=["system_settings"], doc=doc)
        assert resp.status_code == 200, resp.text
        row = rows_for(resp.json(), "system_settings")[0]
        assert row["action"] == "update"
        assert "unknown_future_key" in row["reason"]
        assert settings_manager.rate_limit_per_minute == 42
        assert settings_manager.default_row_limit == 777


class TestRoundTrip:
    async def test_export_then_import_is_idempotent(
        self, client, admin_token, seeded_db, apisix_state, restore_settings_manager
    ):
        """The document a deployment produces must replay cleanly into itself.

        This is what catches a field name that the export writes and the import
        does not understand.
        """
        await seed_full_config(seeded_db)
        seed_gateway(apisix_state)

        exported = await client.get("/admin/config/export", headers=auth_header(admin_token))
        assert exported.status_code == 200, exported.text
        doc = exported.json()

        resp = await do_import(
            client, admin_token, dry_run=False, sections=list(doc["sections"]), doc=doc
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["summary"]["error"] == 0, body["results"]

        actions = {(row["section"], row["name"]): row["action"] for row in body["results"]}
        assert actions[("routes", "svc-a")] == "update"
        assert actions[("upstreams", "svc-a-up")] == "update"
        assert actions[("roles", "analyst")] == "update"
        assert actions[("roles", "admin")] == "skip"
        assert actions[("db_permissions", "analyst @ analytics")] == "update"
        assert actions[("db_connections", "analytics")] == "update"
        assert actions[("s3_connections", "datalake")] == "update"
        assert actions[("nas_connections", "share")] == "update"
        assert actions[("query_templates", "sales/daily")] == "update"
        assert actions[("monitored_hosts", "web1")] == "update"
        assert actions[("monitored_services", "billing")] == "update"
        assert actions[("alert_channels", "mail-a")] == "update"
        assert actions[("alert_settings", "global")] == "update"
        assert actions[("alert_recipients", "db/analytics")] == "update"
        assert actions[("system_settings", "global")] == "update"

        # Replaying the document changes nothing observable.
        second = await client.get("/admin/config/export", headers=auth_header(admin_token))
        assert second.json()["sections"] == doc["sections"]

    async def test_round_trip_preserves_live_secrets(
        self, client, admin_token, seeded_db, apisix_state, restore_settings_manager
    ):
        await seed_full_config(seeded_db)
        seed_gateway(apisix_state)
        doc = (await client.get("/admin/config/export", headers=auth_header(admin_token))).json()
        await do_import(client, admin_token, dry_run=False, sections=list(doc["sections"]), doc=doc)

        async with factory_for(seeded_db)() as db:
            conn = (await db.execute(select(DBConnection))).scalar_one()
            channel = (await db.execute(select(AlertChannel))).scalar_one()
        assert decrypt_password(conn.password_encrypted) == DB_PASSWORD
        assert json.loads(channel.headers) == {"Authorization": CHANNEL_HEADER_SECRET}
        assert channel.webhook_url == CHANNEL_WEBHOOK_URL

        route = apisix_state["routes"]["svc-a"]
        assert route["plugins"]["proxy-rewrite"]["headers"]["set"] == {
            "X-Service-Key": ROUTE_SERVICE_KEY
        }
        assert route["plugins"]["consumer-restriction"] == {"whitelist": ["consumer-1"]}


# ── Import: skips ───────────────────────────────────────────────────────────


class TestSkips:
    async def test_builtin_route_and_upstream_are_skipped(self, client, admin_token, apisix_state):
        doc = export_doc(
            routes=[{"id": "query-api", "name": "query-api", "uri": "/api/query/*",
                     "upstream_id": "unibridge-service"}],
            upstreams=[{"id": "unibridge-service", "name": "unibridge-service",
                        "type": "roundrobin", "nodes": {"evil:8000": 1}}],
        )
        resp = await do_import(
            client, admin_token, dry_run=False, sections=["routes", "upstreams"], doc=doc
        )
        assert resp.status_code == 200, resp.text
        reasons = {row["section"]: row["reason"] for row in resp.json()["results"]}
        assert reasons["routes"] == "builtin route (auto-provisioned)"
        assert reasons["upstreams"] == "builtin upstream (auto-provisioned)"
        assert apisix_state["routes"] == {} and apisix_state["upstreams"] == {}

    async def test_route_shadowing_a_system_namespace_is_rejected(
        self, client, admin_token, apisix_state
    ):
        # A config import writes straight to APISIX, so it must honour the same
        # system-namespace guard save_route enforces — else it is a bypass.
        doc = export_doc(
            routes=[{"id": "sneaky", "name": "sneaky", "uri": "/api/query/exec",
                     "upstream_id": "unibridge-service"}],
        )
        resp = await do_import(
            client, admin_token, dry_run=False, sections=["routes"], doc=doc
        )
        assert resp.status_code == 200, resp.text
        row = rows_for(resp.json(), "routes")[0]
        assert row["action"] == "error"
        assert "/api/query" in row["reason"]
        assert apisix_state["routes"] == {}

    async def test_route_outside_system_namespaces_still_imports(
        self, client, admin_token, apisix_state
    ):
        doc = export_doc(
            routes=[{"id": "myservice", "name": "myservice", "uri": "/api/myservice/*",
                     "upstream_id": "unibridge-service"}],
        )
        resp = await do_import(
            client, admin_token, dry_run=False, sections=["routes"], doc=doc
        )
        assert resp.status_code == 200, resp.text
        row = rows_for(resp.json(), "routes")[0]
        assert row["action"] == "create"
        assert "myservice" in apisix_state["routes"]

    async def test_system_role_is_skipped(self, client, admin_token, seeded_db, apisix_state):
        doc = export_doc(roles=[{"name": "admin", "description": "hijack", "is_system": True,
                                 "permissions": ["query.execute"]}])
        resp = await do_import(client, admin_token, dry_run=False, sections=["roles"], doc=doc)
        row = rows_for(resp.json(), "roles")[0]
        assert (row["action"], row["reason"]) == ("skip", "system role (reseeded at boot)")

        async with factory_for(seeded_db)() as db:
            role = (await db.execute(select(Role).where(Role.name == "admin"))).scalar_one()
            grants = (await db.execute(
                select(RolePermission.permission).where(RolePermission.role_id == role.id)
            )).scalars().all()
        assert len(grants) > 1  # untouched: still holds the full admin grant

    async def test_own_role_is_skipped(self, client, config_writer_token, seeded_db, apisix_state):
        doc = export_doc(roles=[{"name": "configwriter", "description": "escalate",
                                 "is_system": False, "permissions": list(["admin.roles.write"])}])
        resp = await do_import(client, config_writer_token, dry_run=False, sections=["roles"], doc=doc)
        assert resp.status_code == 200, resp.text
        row = rows_for(resp.json(), "roles")[0]
        assert (row["action"], row["reason"]) == ("skip", "cannot modify your own role")

        async with factory_for(seeded_db)() as db:
            role = (await db.execute(select(Role).where(Role.name == "configwriter"))).scalar_one()
            grants = (await db.execute(
                select(RolePermission.permission).where(RolePermission.role_id == role.id)
            )).scalars().all()
        assert sorted(grants) == ["admin.config.read", "admin.config.write"]

    async def test_db_type_change_is_rejected(self, client, admin_token, seeded_db, apisix_state):
        await seed_full_config(seeded_db)
        doc = export_doc(db_connections=[{
            "alias": "analytics", "db_type": "clickhouse", "host": "ch.internal",
            "port": 8443, "database": "analytics", "username": "reader",
            "protocol": "https", "secure": True, "secrets_excluded": True,
        }])
        resp = await do_import(client, admin_token, dry_run=False, sections=["db_connections"], doc=doc)
        row = rows_for(resp.json(), "db_connections")[0]
        assert row["action"] == "error"
        assert "db_type mismatch" in row["reason"]

        async with factory_for(seeded_db)() as db:
            conn = (await db.execute(select(DBConnection))).scalar_one()
        assert (conn.db_type, conn.host) == ("postgres", "db.internal")

    async def test_invalid_connection_fields_are_rejected(
        self, client, admin_token, seeded_db, apisix_state
    ):
        await seed_full_config(seeded_db)
        doc = export_doc(
            db_connections=[{"alias": "analytics", "db_type": "postgres", "port": 999999}],
            s3_connections=[{"alias": "datalake", "endpoint_url": "not-a-url"}],
        )
        resp = await do_import(
            client, admin_token, dry_run=False,
            sections=["db_connections", "s3_connections"], doc=doc,
        )
        assert {row["action"] for row in resp.json()["results"]} == {"error"}

        async with factory_for(seeded_db)() as db:
            conn = (await db.execute(select(DBConnection))).scalar_one()
            s3 = (await db.execute(select(S3Connection))).scalar_one()
        assert conn.port == 5432
        assert s3.endpoint_url == "https://s3.example.com"

    async def test_new_connections_needing_secrets_are_skipped(
        self, client, admin_token, seeded_db, apisix_state
    ):
        doc = export_doc(
            db_connections=[{"alias": "unknown-db", "db_type": "postgres", "host": "h",
                             "port": 5432, "database": "d", "username": "u",
                             "secrets_excluded": True}],
            s3_connections=[{"alias": "unknown-s3", "region": "us-east-1",
                             "secrets_excluded": True}],
        )
        resp = await do_import(
            client, admin_token, dry_run=False,
            sections=["db_connections", "s3_connections"], doc=doc,
        )
        assert resp.status_code == 200, resp.text
        for row in resp.json()["results"]:
            assert row["action"] == "skip"
            assert row["reason"] == "secrets required — create manually first"

        async with factory_for(seeded_db)() as db:
            assert (await db.execute(select(DBConnection))).scalars().all() == []
            assert (await db.execute(select(S3Connection))).scalars().all() == []


# ── Import: consumer-restriction and service keys ───────────────────────────


class TestRoutePlugins:
    async def test_existing_consumer_restriction_is_preserved(
        self, client, admin_token, seeded_db, apisix_state
    ):
        seed_gateway(apisix_state)
        doc = export_doc(routes=[{
            "id": "svc-a", "name": "service-a", "uri": "/api/svc-a/*", "upstream_id": "svc-a-up",
            # A restriction in the file must never win over the live one.
            "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": ["attacker"]}},
        }])
        resp = await do_import(client, admin_token, dry_run=False, sections=["routes"], doc=doc)
        assert resp.status_code == 200, resp.text
        assert rows_for(resp.json(), "routes")[0]["action"] == "update"

        stored = apisix_state["routes"]["svc-a"]
        assert stored["plugins"]["consumer-restriction"] == {"whitelist": ["consumer-1"]}

    async def test_new_route_never_gets_a_consumer_restriction(
        self, client, admin_token, seeded_db, apisix_state
    ):
        doc = export_doc(routes=[{
            "id": "svc-new", "name": "service-new", "uri": "/api/svc-new/*",
            "upstream_id": "svc-a-up",
            "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": ["attacker"]}},
        }])
        resp = await do_import(client, admin_token, dry_run=False, sections=["routes"], doc=doc)
        assert rows_for(resp.json(), "routes")[0]["action"] == "create"
        assert "consumer-restriction" not in apisix_state["routes"]["svc-new"]["plugins"]

    async def test_service_key_placeholder_resolves_to_the_live_secret(
        self, client, admin_token, seeded_db, apisix_state
    ):
        seed_gateway(apisix_state)
        doc = export_doc(routes=[{
            "id": "svc-a", "name": "service-a", "uri": "/api/svc-a/v2/*", "upstream_id": "svc-a-up",
            "secrets_excluded": True,
            "plugins": {"proxy-rewrite": {"headers": {"set": {"X-Service-Key": PLACEHOLDER}}}},
        }])
        resp = await do_import(client, admin_token, dry_run=False, sections=["routes"], doc=doc)
        assert rows_for(resp.json(), "routes")[0]["reason"] is None

        stored = apisix_state["routes"]["svc-a"]
        assert stored["plugins"]["proxy-rewrite"]["headers"]["set"] == {
            "X-Service-Key": ROUTE_SERVICE_KEY
        }
        assert stored["uri"] == "/api/svc-a/v2/*"

    async def test_unrecoverable_service_key_is_dropped_not_written(
        self, client, admin_token, seeded_db, apisix_state
    ):
        doc = export_doc(routes=[{
            "id": "svc-new", "name": "service-new", "uri": "/api/svc-new/*",
            "upstream_id": "svc-a-up", "secrets_excluded": True,
            "plugins": {"proxy-rewrite": {"regex_uri": ["^/api/svc-new(.*)", "$1"],
                                          "headers": {"set": {"X-Service-Key": PLACEHOLDER}}}},
        }])
        resp = await do_import(client, admin_token, dry_run=False, sections=["routes"], doc=doc)
        row = rows_for(resp.json(), "routes")[0]
        assert row["action"] == "create"
        assert "X-Service-Key" in row["reason"]

        plugins = apisix_state["routes"]["svc-new"]["plugins"]
        assert "headers" not in plugins["proxy-rewrite"]
        assert PLACEHOLDER not in json.dumps(apisix_state["routes"]["svc-new"])


# ── Import: alert channel webhook URLs ──────────────────────────────────────


class TestAlertChannelWebhookUrl:
    def _channel_doc(self, **overrides) -> dict:
        return export_doc(alert_channels=[{
            "name": "mail-a", "webhook_url": PLACEHOLDER, "payload_template": "{}",
            "recipient_item_template": None, "headers": {"Authorization": PLACEHOLDER},
            "enabled": True, "secrets_excluded": True, **overrides,
        }])

    async def test_placeholder_keeps_the_stored_url(
        self, client, admin_token, seeded_db, apisix_state
    ):
        await seed_full_config(seeded_db)
        doc = self._channel_doc(payload_template='{"v":2}')
        resp = await do_import(
            client, admin_token, dry_run=False, sections=["alert_channels"], doc=doc
        )
        assert resp.status_code == 200, resp.text
        row = rows_for(resp.json(), "alert_channels")[0]
        assert row["action"] == "update"
        assert row["reason"] == "webhook URL unchanged"

        async with factory_for(seeded_db)() as db:
            channel = (await db.execute(select(AlertChannel))).scalar_one()
        assert channel.webhook_url == CHANNEL_WEBHOOK_URL
        assert channel.payload_template == '{"v":2}'

    async def test_new_channel_with_placeholder_url_errors(
        self, client, admin_token, seeded_db, apisix_state
    ):
        doc = self._channel_doc(name="mail-new", headers=None)
        resp = await do_import(
            client, admin_token, dry_run=False, sections=["alert_channels"], doc=doc
        )
        assert resp.status_code == 200, resp.text
        row = rows_for(resp.json(), "alert_channels")[0]
        assert row["action"] == "error"
        assert "webhook_url not in export" in row["reason"]

        async with factory_for(seeded_db)() as db:
            channels = (await db.execute(select(AlertChannel))).scalars().all()
        assert channels == []

    async def test_dry_run_reports_the_same_error(
        self, client, admin_token, seeded_db, apisix_state
    ):
        doc = self._channel_doc(name="mail-new", headers=None)
        resp = await do_import(
            client, admin_token, dry_run=True, sections=["alert_channels"], doc=doc
        )
        row = rows_for(resp.json(), "alert_channels")[0]
        assert row["action"] == "error"
        assert "webhook_url not in export" in row["reason"]

    async def test_hand_edited_url_still_applies(
        self, client, admin_token, seeded_db, apisix_state
    ):
        """An operator who re-enters the URL in the file gets it written."""
        await seed_full_config(seeded_db)
        doc = self._channel_doc(webhook_url="https://hooks.example.com/moved")
        resp = await do_import(
            client, admin_token, dry_run=False, sections=["alert_channels"], doc=doc
        )
        assert resp.status_code == 200, resp.text
        assert rows_for(resp.json(), "alert_channels")[0]["reason"] is None

        async with factory_for(seeded_db)() as db:
            channel = (await db.execute(select(AlertChannel))).scalar_one()
        assert channel.webhook_url == "https://hooks.example.com/moved"


# ── Import: error isolation ─────────────────────────────────────────────────


class TestErrorIsolation:
    async def test_one_bad_item_does_not_stop_the_import(
        self, client, admin_token, seeded_db, apisix_state
    ):
        doc = export_doc(monitored_hosts=[
            {"name": "good1", "address": "10.0.0.1:9100"},
            {"name": "bad", "address": ""},
            {"name": "good2", "address": "10.0.0.2:9100"},
        ])
        resp = await do_import(client, admin_token, dry_run=False, sections=["monitored_hosts"], doc=doc)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["summary"] == {"create": 2, "update": 0, "skip": 0, "error": 1}

        async with factory_for(seeded_db)() as db:
            hosts = (await db.execute(select(MonitoredHost).order_by(MonitoredHost.name))).scalars().all()
        assert [h.name for h in hosts] == ["good1", "good2"]

    async def test_apisix_failure_is_isolated_per_route(
        self, client, admin_token, seeded_db, apisix_state
    ):
        async def flaky_put(resource, resource_id, body):
            if resource_id == "boom":
                raise RuntimeError("APISIX rejected the route")
            apisix_state[resource][resource_id] = {**body, "id": resource_id}
            return apisix_state[resource][resource_id]

        doc = export_doc(routes=[
            {"id": "ok-1", "name": "ok-1", "uri": "/api/ok-1/*", "upstream_id": "u"},
            {"id": "boom", "name": "boom", "uri": "/api/boom/*", "upstream_id": "u"},
        ])
        with patch("app.services.apisix_client.put_resource", new=AsyncMock(side_effect=flaky_put)):
            resp = await do_import(client, admin_token, dry_run=False, sections=["routes"], doc=doc)

        body = resp.json()
        assert body["summary"] == {"create": 1, "update": 0, "skip": 0, "error": 1}
        failed = next(row for row in body["results"] if row["name"] == "boom")
        assert "APISIX rejected the route" in failed["reason"]
        assert "ok-1" in apisix_state["routes"]

    async def test_malformed_items_are_reported_individually(
        self, client, admin_token, seeded_db, apisix_state
    ):
        doc = export_doc(roles=["not-an-object", {"description": "no name"}])
        resp = await do_import(client, admin_token, dry_run=True, sections=["roles"], doc=doc)
        body = resp.json()
        assert body["summary"]["error"] == 2
        assert {row["reason"] for row in body["results"]} == {
            "item must be an object", "missing name"
        }


# ── Audit ───────────────────────────────────────────────────────────────────


class TestAudit:
    async def test_apply_writes_per_resource_and_summary_rows(
        self, client, admin_token, seeded_db, apisix_state
    ):
        doc = TestDryRun.sample_doc()
        resp = await do_import(
            client, admin_token, dry_run=False,
            sections=["roles", "monitored_hosts", "routes"], doc=doc,
        )
        assert resp.status_code == 200, resp.text

        async with factory_for(seeded_db)() as db:
            logs = (await db.execute(select(AdminAuditLog))).scalars().all()
        by_type = {log.resource_type: log for log in logs}

        assert {"role", "monitored_host", "route", "config_import"} <= set(by_type)
        assert by_type["role"].action == "create"
        assert by_type["monitored_host"].resource_id == "web1"

        summary_row = by_type["config_import"]
        assert summary_row.action == "import"
        assert summary_row.actor == "testadmin"
        payload = json.loads(summary_row.after)
        assert payload["summary"] == {"create": 3, "update": 0, "skip": 0, "error": 0}
        assert sorted(payload["sections"]) == ["monitored_hosts", "roles", "routes"]

    async def test_dry_run_writes_no_audit_rows(self, client, admin_token, seeded_db, apisix_state):
        await do_import(
            client, admin_token, dry_run=True,
            sections=["roles", "monitored_hosts", "routes"], doc=TestDryRun.sample_doc(),
        )
        async with factory_for(seeded_db)() as db:
            logs = (await db.execute(select(AdminAuditLog))).scalars().all()
        assert logs == []

    async def test_audit_snapshots_never_carry_secrets(
        self, client, admin_token, seeded_db, apisix_state
    ):
        seed_gateway(apisix_state)
        doc = export_doc(routes=[{
            "id": "svc-a", "name": "service-a", "uri": "/api/svc-a/*", "upstream_id": "svc-a-up",
            "plugins": {"proxy-rewrite": {"headers": {"set": {"X-Service-Key": PLACEHOLDER}}}},
        }])
        await do_import(client, admin_token, dry_run=False, sections=["routes"], doc=doc)

        async with factory_for(seeded_db)() as db:
            logs = (await db.execute(select(AdminAuditLog))).scalars().all()
        serialized = json.dumps([{"before": log.before, "after": log.after} for log in logs])
        assert ROUTE_SERVICE_KEY not in serialized
