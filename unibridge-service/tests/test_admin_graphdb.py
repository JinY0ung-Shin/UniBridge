"""Admin router graphdb branches.

Mirrors the existing Neo4j admin test patterns: uses client/admin_token fixtures
and patches connection_manager to avoid touching the network.
"""
from __future__ import annotations

import pytest

from tests.conftest import auth_header
from tests.test_admin import _cm_patch


def _make_graphdb_payload(**overrides):
    payload = {
        "alias": "kg-test",
        "db_type": "graphdb",
        "host": "graphdb.local",
        "port": 7200,
        "database": "my-repo",
        "username": "admin",
        "password": "pw",
        "protocol": "http",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_create_graphdb_connection_with_http(client, admin_token):
    with _cm_patch("graphdb"):
        resp = await client.post(
            "/admin/query/databases",
            json=_make_graphdb_payload(),
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["db_type"] == "graphdb"
    assert data["protocol"] == "http"


@pytest.mark.asyncio
async def test_create_graphdb_connection_with_https(client, admin_token):
    with _cm_patch("graphdb"):
        resp = await client.post(
            "/admin/query/databases",
            json=_make_graphdb_payload(alias="kg-https", protocol="https"),
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["protocol"] == "https"


@pytest.mark.asyncio
async def test_create_graphdb_missing_protocol_returns_400(client, admin_token):
    payload = _make_graphdb_payload(alias="kg-noproto")
    del payload["protocol"]
    with _cm_patch("graphdb"):
        resp = await client.post(
            "/admin/query/databases",
            json=payload,
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 400
    assert "protocol" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_create_graphdb_invalid_protocol_returns_400(client, admin_token):
    with _cm_patch("graphdb"):
        resp = await client.post(
            "/admin/query/databases",
            json=_make_graphdb_payload(alias="kg-bolt", protocol="bolt"),
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_graphdb_with_secure_returns_400(client, admin_token):
    with _cm_patch("graphdb"):
        resp = await client.post(
            "/admin/query/databases",
            json=_make_graphdb_payload(alias="kg-secure", secure=True),
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 400
    assert "secure" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_list_tables_returns_empty_for_graphdb(client, admin_token):
    with _cm_patch("graphdb"):
        # Create
        create = await client.post(
            "/admin/query/databases",
            json=_make_graphdb_payload(alias="kg-tables"),
            headers=auth_header(admin_token),
        )
        assert create.status_code == 201, create.text
        # List tables
        resp = await client.get(
            "/admin/query/databases/kg-tables/tables",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_upsert_permission_rejects_allowed_tables_for_graphdb(client, admin_token):
    with _cm_patch("graphdb"):
        await client.post(
            "/admin/query/databases",
            json=_make_graphdb_payload(alias="kg-perm-reject"),
            headers=auth_header(admin_token),
        )
        resp = await client.put(
            "/admin/query/permissions",
            json={
                "role": "developer",
                "db_alias": "kg-perm-reject",
                "allow_select": True,
                "allowed_tables": ["foo"],
            },
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 400
    assert "allowed_tables" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_upsert_permission_accepts_no_allowed_tables_for_graphdb(client, admin_token):
    with _cm_patch("graphdb"):
        await client.post(
            "/admin/query/databases",
            json=_make_graphdb_payload(alias="kg-perm-accept"),
            headers=auth_header(admin_token),
        )
        resp = await client.put(
            "/admin/query/permissions",
            json={
                "role": "developer",
                "db_alias": "kg-perm-accept",
                "allow_select": True,
            },
            headers=auth_header(admin_token),
        )
    # Existing successful upsert returns 200 or 201 depending on update vs create — accept either.
    assert resp.status_code in (200, 201), resp.text
    data = resp.json()
    assert data["role"] == "developer"
    assert data["db_alias"] == "kg-perm-accept"


@pytest.mark.asyncio
async def test_upsert_permission_accepts_empty_list_for_graphdb(client, admin_token):
    """allowed_tables=[] (empty) is falsy and must be accepted for graphdb."""
    with _cm_patch("graphdb"):
        await client.post(
            "/admin/query/databases",
            json=_make_graphdb_payload(alias="kg-perm-empty"),
            headers=auth_header(admin_token),
        )
        resp = await client.put(
            "/admin/query/permissions",
            json={
                "role": "developer",
                "db_alias": "kg-perm-empty",
                "allow_select": True,
                "allowed_tables": [],
            },
            headers=auth_header(admin_token),
        )
    assert resp.status_code in (200, 201), resp.text
