"""Audit coverage for the expanded set of admin mutations (AdminAuditLog).

Complements ``test_admin_audit.py`` (gateway/API-key wiring). Covers:
  * DB connection / permission / query template mutations (routers/admin.py).
  * S3 / NAS connection mutations (routers/s3.py, routers/nas.py).
  * Alert settings / channel / resource-owner mutations (routers/alerts.py).
  * Role CRUD (routers/roles.py) and Keycloak user mutations (routers/users.py).
  * Secret masking: DB passwords, S3 keys, webhook URLs/headers, user passwords
    never appear in before/after snapshots.
  * The best-effort guarantee: a failed audit write never breaks the mutation.

Each test starts with a fresh in-memory DB (function-scoped ``engine``
fixture), so audit-row counts are asserted absolutely.
"""
from __future__ import annotations

import json
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.audit import redact_snapshot
from tests.conftest import auth_header

# ── Helpers ──────────────────────────────────────────────────────────────────

DB_PAYLOAD = {
    "alias": "auditdb",
    "db_type": "postgres",
    "host": "localhost",
    "port": 5432,
    "database": "mydb",
    "username": "pguser",
    "password": "super-secret-pw",
}

S3_PAYLOAD = {
    "alias": "audit-s3",
    "endpoint_url": "https://minio.example.com",
    "region": "us-east-1",
    "access_key_id": "AKIAIOSFODNN7EXAMPLE",
    "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "default_bucket": "my-bucket",
    "use_ssl": True,
}

NAS_PAYLOAD = {
    "alias": "audit-nas",
    "base_path": "/mnt/share1",
    "max_download_bytes": 1048576,
}

USER_1_ID = "00000000-0000-4000-a000-000000000001"
NEW_USER_ID = "00000000-0000-4000-a000-000000000002"


async def _audit_logs(client, token, **params):
    resp = await client.get(
        "/admin/audit-logs", params=params, headers=auth_header(token)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _cm_patch(db_type: str = "postgres"):
    """Patch the admin router's connection_manager singleton (same shape as
    the mock used in test_admin.py)."""
    mock_cm = MagicMock()
    mock_cm.add_connection = AsyncMock()
    mock_cm.remove_connection = AsyncMock()
    mock_cm.get_status = MagicMock(return_value={"status": "registered"})
    mock_cm.get_db_type = MagicMock(return_value=db_type)
    mock_cm.get_engine = MagicMock(return_value=MagicMock())
    mock_cm.get_clickhouse_lock = MagicMock(return_value=threading.Lock())
    mock_cm.has_connection = MagicMock(return_value=True)
    mock_cm.test_connection = AsyncMock(return_value=(True, "Connection successful"))
    return patch("app.routers.admin.connection_manager", mock_cm)


def _s3_patch():
    mgr = MagicMock()
    mgr.add_connection = AsyncMock()
    mgr.remove_connection = AsyncMock()
    mgr.has_connection.return_value = True
    return patch("app.routers.s3.s3_manager", mgr)


def _nas_patch():
    mgr = MagicMock()
    mgr.add_connection = AsyncMock()
    mgr.remove_connection = AsyncMock()
    mgr.has_connection.return_value = True
    return patch("app.routers.nas.nas_manager", mgr)


def _make_kc_mock() -> AsyncMock:
    kc = AsyncMock()
    kc.get_user.return_value = {
        "id": USER_1_ID,
        "username": "alice",
        "email": "alice@example.com",
        "enabled": True,
        "createdTimestamp": 1700000000000,
    }
    kc.get_user_realm_roles.return_value = [{"id": "role-id-user", "name": "user"}]
    kc.create_user.return_value = NEW_USER_ID
    return kc


@pytest.fixture
def kc_mock():
    import app.routers.users as users_mod
    users_mod._kc_admin = None
    mock = _make_kc_mock()
    with patch("app.routers.users._get_kc_admin", return_value=mock):
        yield mock
    users_mod._kc_admin = None


# ── redact_snapshot: headers.add gap ─────────────────────────────────────────


class TestRedactHeadersAdd:
    def test_masks_proxy_rewrite_header_add_values(self):
        snap = {
            "plugins": {"proxy-rewrite": {"headers": {"add": {"X-Custom": "abcdef123456"}}}}
        }
        out = redact_snapshot(snap)
        assert out["plugins"]["proxy-rewrite"]["headers"]["add"]["X-Custom"] == "***3456"

    def test_masks_s3_credential_keys(self):
        out = redact_snapshot(
            {"secret_access_key": "wJalrXUtnFEMI1234", "session_token": "tok-abcd9876"}
        )
        assert out["secret_access_key"] == "***1234"
        assert out["session_token"] == "***9876"


# ── DB connections ───────────────────────────────────────────────────────────


class TestDbConnectionAuditing:
    async def test_create_writes_masked_audit(self, client, admin_token):
        with _cm_patch():
            resp = await client.post(
                "/admin/query/databases", json=DB_PAYLOAD, headers=auth_header(admin_token)
            )
            assert resp.status_code == 201, resp.text

        logs = await _audit_logs(client, admin_token, resource_type="db_connection")
        assert len(logs) == 1
        entry = logs[0]
        assert entry["actor"] == "testadmin"
        assert entry["action"] == "create"
        assert entry["resource_id"] == "auditdb"
        assert entry["before"] is None
        after = json.loads(entry["after"])
        assert after["alias"] == "auditdb"
        assert after["password"] == "***"
        assert "super-secret-pw" not in entry["after"]

    async def test_update_records_before_and_masks_password(self, client, admin_token):
        with _cm_patch():
            await client.post(
                "/admin/query/databases", json=DB_PAYLOAD, headers=auth_header(admin_token)
            )
            resp = await client.put(
                "/admin/query/databases/auditdb",
                json={"host": "newhost", "password": "rotated-secret-pw"},
                headers=auth_header(admin_token),
            )
            assert resp.status_code == 200, resp.text

        logs = await _audit_logs(
            client, admin_token, resource_type="db_connection", action="update"
        )
        assert len(logs) == 1
        before = json.loads(logs[0]["before"])
        after = json.loads(logs[0]["after"])
        assert before["host"] == "localhost"
        assert after["host"] == "newhost"
        assert before["password"] == "***" and after["password"] == "***"
        assert "rotated-secret-pw" not in (logs[0]["before"] + logs[0]["after"])

    async def test_delete_writes_before_snapshot(self, client, admin_token):
        with _cm_patch():
            await client.post(
                "/admin/query/databases", json=DB_PAYLOAD, headers=auth_header(admin_token)
            )
            resp = await client.delete(
                "/admin/query/databases/auditdb", headers=auth_header(admin_token)
            )
            assert resp.status_code == 204

        logs = await _audit_logs(
            client, admin_token, resource_type="db_connection", action="delete"
        )
        assert len(logs) == 1
        assert logs[0]["resource_id"] == "auditdb"
        assert logs[0]["after"] is None
        assert json.loads(logs[0]["before"])["alias"] == "auditdb"
        assert "super-secret-pw" not in logs[0]["before"]


# ── Permissions ──────────────────────────────────────────────────────────────


class TestPermissionAuditing:
    async def test_upsert_create_then_update_then_delete(self, client, admin_token):
        body = {"role": "user", "db_alias": "auditdb", "allow_select": True}
        with _cm_patch():
            resp = await client.put(
                "/admin/query/permissions", json=body, headers=auth_header(admin_token)
            )
            assert resp.status_code == 200, resp.text
            perm_id = resp.json()["id"]

            resp = await client.put(
                "/admin/query/permissions",
                json={**body, "allow_insert": True},
                headers=auth_header(admin_token),
            )
            assert resp.status_code == 200

            resp = await client.delete(
                f"/admin/query/permissions/{perm_id}", headers=auth_header(admin_token)
            )
            assert resp.status_code == 204

        logs = await _audit_logs(client, admin_token, resource_type="permission")
        assert [e["action"] for e in logs] == ["delete", "update", "create"]
        created = logs[2]
        assert created["resource_id"] == str(perm_id)
        assert created["summary"] == "user @ auditdb"
        assert created["before"] is None
        assert json.loads(created["after"])["allow_select"] is True
        updated = logs[1]
        assert json.loads(updated["before"])["allow_insert"] is False
        assert json.loads(updated["after"])["allow_insert"] is True
        deleted = logs[0]
        assert deleted["after"] is None
        assert json.loads(deleted["before"])["role"] == "user"


# ── Query templates ──────────────────────────────────────────────────────────


class TestQueryTemplateAuditing:
    async def _create_db(self, client, admin_token):
        resp = await client.post(
            "/admin/query/databases", json=DB_PAYLOAD, headers=auth_header(admin_token)
        )
        assert resp.status_code == 201, resp.text

    async def test_create_update_delete_write_audit(self, client, admin_token):
        with _cm_patch():
            await self._create_db(client, admin_token)
            resp = await client.post(
                "/admin/query/templates",
                json={
                    "path": "reports/users",
                    "name": "Users report",
                    "database": "auditdb",
                    "sql": "SELECT id FROM users",
                },
                headers=auth_header(admin_token),
            )
            assert resp.status_code == 201, resp.text

            resp = await client.put(
                "/admin/query/templates/reports/users",
                json={"name": "Users report v2"},
                headers=auth_header(admin_token),
            )
            assert resp.status_code == 200, resp.text

            resp = await client.delete(
                "/admin/query/templates/reports/users", headers=auth_header(admin_token)
            )
            assert resp.status_code == 204

        logs = await _audit_logs(client, admin_token, resource_type="query_template")
        assert [e["action"] for e in logs] == ["delete", "update", "create"]
        assert all(e["resource_id"] == "reports/users" for e in logs)
        created = logs[2]
        assert created["before"] is None
        assert json.loads(created["after"])["sql"] == "SELECT id FROM users"
        updated = logs[1]
        assert json.loads(updated["before"])["name"] == "Users report"
        assert json.loads(updated["after"])["name"] == "Users report v2"
        deleted = logs[0]
        assert deleted["after"] is None
        assert deleted["summary"] == "Users report v2"


# ── S3 connections ───────────────────────────────────────────────────────────


class TestS3ConnectionAuditing:
    async def test_create_masks_credentials(self, client, admin_token):
        with _s3_patch():
            resp = await client.post(
                "/admin/s3/connections", json=S3_PAYLOAD, headers=auth_header(admin_token)
            )
            assert resp.status_code == 201, resp.text

        logs = await _audit_logs(client, admin_token, resource_type="s3_connection")
        assert len(logs) == 1
        entry = logs[0]
        assert entry["action"] == "create"
        assert entry["resource_id"] == "audit-s3"
        assert entry["before"] is None
        after = json.loads(entry["after"])
        assert after["secret_access_key"] == "***"
        assert after["access_key_id"] == "***MPLE"
        assert S3_PAYLOAD["secret_access_key"] not in entry["after"]
        assert S3_PAYLOAD["access_key_id"] not in entry["after"]

    async def test_update_and_delete_write_audit(self, client, admin_token):
        with _s3_patch():
            await client.post(
                "/admin/s3/connections", json=S3_PAYLOAD, headers=auth_header(admin_token)
            )
            resp = await client.put(
                "/admin/s3/connections/audit-s3",
                json={"region": "ap-northeast-2", "secret_access_key": "rotated/secretKEY99"},
                headers=auth_header(admin_token),
            )
            assert resp.status_code == 200, resp.text
            resp = await client.delete(
                "/admin/s3/connections/audit-s3", headers=auth_header(admin_token)
            )
            assert resp.status_code == 204

        logs = await _audit_logs(client, admin_token, resource_type="s3_connection")
        assert [e["action"] for e in logs] == ["delete", "update", "create"]
        updated = logs[1]
        assert json.loads(updated["before"])["region"] == "us-east-1"
        assert json.loads(updated["after"])["region"] == "ap-northeast-2"
        assert "rotated/secretKEY99" not in (updated["before"] + updated["after"])
        deleted = logs[0]
        assert deleted["after"] is None
        assert json.loads(deleted["before"])["secret_access_key"] == "***"


# ── NAS connections ──────────────────────────────────────────────────────────


class TestNasConnectionAuditing:
    async def test_create_update_delete_write_audit(self, client, admin_token):
        with _nas_patch():
            resp = await client.post(
                "/admin/nas/connections", json=NAS_PAYLOAD, headers=auth_header(admin_token)
            )
            assert resp.status_code == 201, resp.text
            resp = await client.put(
                "/admin/nas/connections/audit-nas",
                json={"base_path": "/mnt/share2"},
                headers=auth_header(admin_token),
            )
            assert resp.status_code == 200, resp.text
            resp = await client.delete(
                "/admin/nas/connections/audit-nas", headers=auth_header(admin_token)
            )
            assert resp.status_code == 204

        logs = await _audit_logs(client, admin_token, resource_type="nas_connection")
        assert [e["action"] for e in logs] == ["delete", "update", "create"]
        assert all(e["resource_id"] == "audit-nas" for e in logs)
        assert json.loads(logs[2]["after"])["base_path"] == "/mnt/share1"
        assert json.loads(logs[1]["before"])["base_path"] == "/mnt/share1"
        assert json.loads(logs[1]["after"])["base_path"] == "/mnt/share2"
        assert logs[0]["after"] is None


# ── Alerts: settings / channels / resource owners ────────────────────────────


class TestAlertSettingsAuditing:
    async def test_update_settings_writes_audit(self, client, admin_token):
        resp = await client.put(
            "/admin/alerts/settings",
            json={"admin_emails": ["ops@example.com"], "check_interval_seconds": 120},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200, resp.text

        logs = await _audit_logs(client, admin_token, resource_type="alert_settings")
        assert len(logs) == 1
        entry = logs[0]
        assert entry["action"] == "update"
        assert entry["resource_id"] == "global"
        assert json.loads(entry["before"])["admin_emails"] == []
        after = json.loads(entry["after"])
        assert after["admin_emails"] == ["ops@example.com"]
        assert after["check_interval_seconds"] == 120


class TestAlertChannelAuditing:
    CREATE_BODY = {
        "name": "secret-ch",
        "webhook_url": "https://hooks.example.com/services/T123/B456/SECRETTOKEN",
        "payload_template": "{}",
        "headers": {"Authorization": "Bearer super-secret-token"},
    }

    async def test_create_masks_webhook_url_and_headers(self, client, admin_token):
        resp = await client.post(
            "/admin/alerts/channels", json=self.CREATE_BODY, headers=auth_header(admin_token)
        )
        assert resp.status_code == 201, resp.text
        channel_id = resp.json()["id"]

        logs = await _audit_logs(client, admin_token, resource_type="alert_channel")
        assert len(logs) == 1
        entry = logs[0]
        assert entry["action"] == "create"
        assert entry["resource_id"] == str(channel_id)
        assert entry["summary"] == "secret-ch"
        assert entry["before"] is None
        after = json.loads(entry["after"])
        assert after["webhook_url"] == "https://hooks.example.com/***"
        assert after["headers"] == {"Authorization": "***"}
        assert "SECRETTOKEN" not in entry["after"]
        assert "super-secret-token" not in entry["after"]

    async def test_update_and_delete_write_audit(self, client, admin_token):
        resp = await client.post(
            "/admin/alerts/channels", json=self.CREATE_BODY, headers=auth_header(admin_token)
        )
        channel_id = resp.json()["id"]
        resp = await client.put(
            f"/admin/alerts/channels/{channel_id}",
            json={"name": "renamed-ch", "headers": {"X-Api-Token": "another-secret"}},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200, resp.text
        resp = await client.delete(
            f"/admin/alerts/channels/{channel_id}", headers=auth_header(admin_token)
        )
        assert resp.status_code == 204

        logs = await _audit_logs(client, admin_token, resource_type="alert_channel")
        assert [e["action"] for e in logs] == ["delete", "update", "create"]
        updated = logs[1]
        assert json.loads(updated["before"])["name"] == "secret-ch"
        after = json.loads(updated["after"])
        assert after["name"] == "renamed-ch"
        assert after["headers"] == {"X-Api-Token": "***"}
        assert "another-secret" not in updated["after"]
        deleted = logs[0]
        assert deleted["after"] is None
        assert json.loads(deleted["before"])["name"] == "renamed-ch"


class TestResourceOwnerAuditing:
    async def test_upsert_and_delete_write_audit(self, client, admin_token):
        with _cm_patch():
            await client.post(
                "/admin/query/databases", json=DB_PAYLOAD, headers=auth_header(admin_token)
            )

        resp = await client.put(
            "/admin/alerts/resource-owners/db/auditdb",
            json={"emails": ["owner@example.com"]},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200, resp.text
        resp = await client.delete(
            "/admin/alerts/resource-owners/db/auditdb", headers=auth_header(admin_token)
        )
        assert resp.status_code == 204

        logs = await _audit_logs(client, admin_token, resource_type="resource_owner")
        assert [e["action"] for e in logs] == ["delete", "create"]
        assert all(e["resource_id"] == "db/auditdb" for e in logs)
        assert json.loads(logs[1]["after"])["emails"] == ["owner@example.com"]
        assert json.loads(logs[0]["before"])["emails"] == ["owner@example.com"]


# ── Roles ────────────────────────────────────────────────────────────────────


class TestRoleAuditing:
    async def test_create_update_delete_write_audit(self, client, admin_token):
        resp = await client.post(
            "/admin/roles",
            json={"name": "auditor", "description": "audit role", "permissions": ["alerts.read"]},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 201, resp.text
        role_id = resp.json()["id"]

        resp = await client.put(
            f"/admin/roles/{role_id}",
            json={"permissions": ["alerts.read", "alerts.write"]},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200, resp.text

        resp = await client.delete(f"/admin/roles/{role_id}", headers=auth_header(admin_token))
        assert resp.status_code == 204

        logs = await _audit_logs(client, admin_token, resource_type="role")
        assert [e["action"] for e in logs] == ["delete", "update", "create"]
        assert all(e["resource_id"] == str(role_id) for e in logs)
        assert all(e["summary"] == "auditor" for e in logs)
        created = logs[2]
        assert created["before"] is None
        assert json.loads(created["after"])["permissions"] == ["alerts.read"]
        updated = logs[1]
        assert json.loads(updated["before"])["permissions"] == ["alerts.read"]
        assert json.loads(updated["after"])["permissions"] == ["alerts.read", "alerts.write"]
        deleted = logs[0]
        assert deleted["after"] is None
        assert json.loads(deleted["before"])["name"] == "auditor"


# ── Users (Keycloak proxy) ───────────────────────────────────────────────────


class TestUserAuditing:
    async def test_create_user_writes_audit_without_password(self, client, admin_token, kc_mock):
        resp = await client.post(
            "/admin/users",
            json={
                "username": "bob",
                "email": "bob@example.com",
                "password": "initial-pw-secret",
                "role": "user",
            },
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 201, resp.text

        logs = await _audit_logs(client, admin_token, resource_type="user", action="create")
        assert len(logs) == 1
        entry = logs[0]
        assert entry["resource_id"] == NEW_USER_ID
        assert entry["summary"] == "bob"
        after = json.loads(entry["after"])
        assert after["username"] == "bob"
        assert "password" not in after
        assert "initial-pw-secret" not in entry["after"]

    async def test_change_role_writes_user_role_audit(self, client, admin_token, kc_mock):
        kc_mock.get_user_realm_roles.return_value = [
            {"id": "role-id-user", "name": "user"},
            {"id": "role-id-admin", "name": "admin"},
        ]
        resp = await client.put(
            f"/admin/users/{USER_1_ID}/role",
            json={"role": "admin"},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200, resp.text

        logs = await _audit_logs(client, admin_token, resource_type="user_role")
        assert len(logs) == 1
        entry = logs[0]
        assert entry["action"] == "update"
        assert entry["resource_id"] == USER_1_ID
        assert json.loads(entry["before"]) == {"role": "user"}
        assert json.loads(entry["after"]) == {"role": "admin"}

    async def test_reset_password_writes_audit_without_password(
        self, client, admin_token, kc_mock
    ):
        resp = await client.put(
            f"/admin/users/{USER_1_ID}/reset-password",
            json={"password": "new-pw-secret-123", "temporary": True},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 204, resp.text

        logs = await _audit_logs(client, admin_token, resource_type="user", action="update")
        assert len(logs) == 1
        entry = logs[0]
        assert entry["summary"] == "password reset"
        after = json.loads(entry["after"])
        assert after["password"] == "***"
        assert "new-pw-secret-123" not in entry["after"]

    async def test_toggle_enabled_records_before_after(self, client, admin_token, kc_mock):
        resp = await client.put(
            f"/admin/users/{USER_1_ID}/enabled",
            json={"enabled": False},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200, resp.text

        logs = await _audit_logs(client, admin_token, resource_type="user", action="update")
        assert len(logs) == 1
        assert json.loads(logs[0]["before"]) == {"enabled": True}
        assert json.loads(logs[0]["after"]) == {"enabled": False}

    async def test_delete_user_writes_audit(self, client, admin_token, kc_mock):
        resp = await client.delete(
            f"/admin/users/{USER_1_ID}", headers=auth_header(admin_token)
        )
        assert resp.status_code == 204, resp.text

        logs = await _audit_logs(client, admin_token, resource_type="user", action="delete")
        assert len(logs) == 1
        entry = logs[0]
        assert entry["resource_id"] == USER_1_ID
        assert entry["after"] is None
        assert json.loads(entry["before"])["username"] == "alice"


# ── Best-effort guarantee ────────────────────────────────────────────────────


class TestAuditFailureDoesNotBreakMutation:
    async def test_db_connection_create_survives_audit_failure(self, client, admin_token):
        with (
            _cm_patch(),
            patch(
                "app.services.audit.async_sessionmaker",
                side_effect=RuntimeError("audit db down"),
            ),
        ):
            resp = await client.post(
                "/admin/query/databases", json=DB_PAYLOAD, headers=auth_header(admin_token)
            )
            assert resp.status_code == 201, resp.text

        # The audit write failed and was swallowed, so no row was persisted.
        assert await _audit_logs(client, admin_token, resource_type="db_connection") == []

    async def test_alert_channel_create_survives_audit_failure(self, client, admin_token):
        with patch(
            "app.services.audit.async_sessionmaker",
            side_effect=RuntimeError("audit db down"),
        ):
            resp = await client.post(
                "/admin/alerts/channels",
                json={
                    "name": "best-effort-ch",
                    "webhook_url": "https://hooks.example.com/hook",
                    "payload_template": "{}",
                },
                headers=auth_header(admin_token),
            )
            assert resp.status_code == 201, resp.text

        assert await _audit_logs(client, admin_token, resource_type="alert_channel") == []
