"""MCP network-map tools: offered by grant, sent out on the network routes."""
from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from app.services import mcp_tools, network_map
from tests.conftest import auth_header
from tests.test_mcp import (  # noqa: F401 - the gateway fixture is used by name
    _call,
    _create_api_key,
    _put_settings,
    _text,
    _tool_names,
    gateway,
)

READ_TOOLS = [tool.name for tool in mcp_tools.NETWORK_READ_TOOLS]
WRITE_TOOLS = [tool.name for tool in mcp_tools.NETWORK_WRITE_TOOLS]


@pytest.fixture(autouse=True)
def _no_gateway_discovery(monkeypatch):
    """Usage discovery reads APISIX upstreams; give it none instead of dialing out."""
    monkeypatch.setattr(
        network_map.apisix_client, "list_resources", AsyncMock(return_value={"items": []})
    )
    network_map.clear_gateway_cache()
    yield
    network_map.clear_gateway_cache()


async def _map(client, admin_token) -> dict[str, int]:
    """Regions A (10.1/16) and B (10.2/16); hosts a1, a2 in A and b1 in B."""
    h = auth_header(admin_token)
    ids: dict[str, int] = {}
    for name, cidr in (("A", "10.1.0.0/16"), ("B", "10.2.0.0/16")):
        resp = await client.post("/network/regions", headers=h, json={"name": name, "cidrs": [cidr]})
        assert resp.status_code == 201, resp.text
        ids[name] = resp.json()["id"]
    for name, ip in (("a1", "10.1.0.1"), ("a2", "10.1.0.2"), ("b1", "10.2.0.1")):
        resp = await client.post("/network/hosts", headers=h, json={"name": name, "ip": ip})
        assert resp.status_code == 201, resp.text
        ids[name] = resp.json()["id"]
    return ids


def _json(result: dict) -> object:
    import json

    assert result["isError"] is False, result
    return json.loads(_text(result))


async def test_network_tools_follow_the_grants_and_the_group(client, seeded_db, admin_token, gateway):
    none = await _create_api_key(seeded_db, "plain", routes=["mcp-api", "query-api"])
    reader = await _create_api_key(seeded_db, "reader", routes=["mcp-api", "network-api"])
    writer = await _create_api_key(
        seeded_db, "writer", routes=["mcp-api", "network-api", "network-write-api"]
    )
    # The write grant alone offers nothing: the tools look hosts up on network-api.
    write_only = await _create_api_key(seeded_db, "write-only", routes=["mcp-api", "network-write-api"])

    assert not any(name.startswith("network_") for name in await _tool_names(client, none))
    assert await _tool_names(client, reader) == READ_TOOLS
    assert await _tool_names(client, writer) == READ_TOOLS + WRITE_TOOLS
    assert await _tool_names(client, write_only) == []

    tools = (
        await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=writer
        )
    ).json()["result"]["tools"]
    hints = {tool["name"]: tool["annotations"]["readOnlyHint"] for tool in tools}
    assert all(hints[name] for name in READ_TOOLS)
    assert not any(hints[name] for name in WRITE_TOOLS)
    for tool in tools:
        if tool["name"] in WRITE_TOOLS and tool["name"].startswith(("network_create", "network_register", "network_record", "network_update")):
            assert "unverified" in tool["description"]

    await _put_settings(client, admin_token, network=False)
    assert await _tool_names(client, writer) == []
    assert gateway.calls == []


async def test_read_tools_go_out_on_network_api(client, seeded_db, admin_token, gateway):
    ids = await _map(client, admin_token)
    key = await _create_api_key(seeded_db, "reader", routes=["mcp-api", "network-api"])

    regions = _json(await _call(client, key, "network_list_regions", {}))
    assert [r["name"] for r in regions["regions"]] == ["A", "B"]
    found = _json(await _call(client, key, "network_find_hosts", {"query": "10.1.", "limit": 1}))
    assert [h["name"] for h in found["hosts"]] == ["a1"]
    assert found["total"] == 2 and found["truncated"] is True
    # By name, IP or id.
    for ref in ("B1", "10.2.0.1", str(ids["b1"])):
        host = _json(await _call(client, key, "network_get_host", {"host": ref}))
        assert host["id"] == ids["b1"]
    missing = await _call(client, key, "network_get_host", {"host": "nope"})
    assert missing["isError"] is True and "network_find_hosts" in _text(missing)
    overview = _json(await _call(client, key, "network_overview", {}))
    assert overview["host_count"] == 3

    get_calls = [call for call in gateway.calls if call[0] == "network-api"]
    assert get_calls == gateway.calls
    assert ("network-api", "GET", "/api/network/regions") in gateway.calls
    assert ("network-api", "GET", f"/api/network/hosts/{ids['b1']}") in gateway.calls


async def test_check_access_explains_and_drafts_a_request(client, seeded_db, admin_token, gateway):
    await _map(client, admin_token)
    key = await _create_api_key(seeded_db, "reader", routes=["mcp-api", "network-api"])

    same = _json(await _call(client, key, "network_check_access", {"src": "a1", "dst": "a2", "port": 22}))
    assert same["status"] == "open" and same["reason"] == "same_region"
    blocked = _json(
        await _call(client, key, "network_check_access", {"src": "a1", "dst": "10.2.0.1", "port": 5432})
    )
    assert blocked["status"] == "blocked"
    assert blocked["request_draft"]
    assert gateway.calls[-1] == ("network-api", "GET", "/api/network/check")


async def test_write_tools_go_out_on_network_write_api_and_stay_unverified(
    client, seeded_db, admin_token, gateway
):
    ids = await _map(client, admin_token)
    key = await _create_api_key(
        seeded_db, "recorder", routes=["mcp-api", "network-api", "network-write-api"]
    )

    # Regions are defined by people; there is no tool for them.
    assert "network_create_region" not in await _tool_names(client, key)
    host = _json(await _call(client, key, "network_register_host", {"name": "c1", "ip": "10.2.0.9"}))
    assert host["region_name"] == "B"
    assert host["source"] == "agent" and host["verified"] is False
    updated = _json(await _call(client, key, "network_update_host", {"host": "c1", "owner": "infra"}))
    assert updated["owner"] == "infra"

    opening = _json(
        await _call(
            client,
            key,
            "network_record_opening",
            {"src_host": "a1", "dst_host": "b1", "port_from": 5432, "ticket_no": "FW-1"},
        )
    )
    assert opening["verified"] is False and opening["port_to"] == 5432
    closed = _json(
        await _call(client, key, "network_update_opening", {"opening_id": opening["id"], "status": "closed"})
    )
    assert closed["status"] == "closed"
    listed = _json(await _call(client, key, "network_list_openings", {"host": "b1"}))
    assert [o["id"] for o in listed["openings"]] == [opening["id"]]
    deleted = _json(await _call(client, key, "network_delete_opening", {"opening_id": opening["id"]}))
    assert deleted == {"deleted": True, "opening_id": opening["id"]}

    usage = _json(
        await _call(
            client, key, "network_record_port_usage", {"host": "b1", "port": 5432, "service_name": "pg"}
        )
    )
    assert usage["source"] == "agent" and usage["verified"] is False
    usages = _json(await _call(client, key, "network_list_port_usages", {"host": str(ids["b1"])}))
    assert [u["service_name"] for u in usages["port_usages"]] == ["pg"]
    _json(await _call(client, key, "network_delete_port_usage", {"usage_id": usage["id"]}))

    writes = [(method, path) for route, method, path in gateway.calls if route == "network-write-api"]
    assert writes == [
        ("POST", "/api/network/hosts"),
        ("PUT", f"/api/network/hosts/{host['id']}"),
        ("POST", "/api/network/openings"),
        ("PUT", f"/api/network/openings/{opening['id']}"),
        ("DELETE", f"/api/network/openings/{opening['id']}"),
        ("POST", "/api/network/port-usages"),
        ("DELETE", f"/api/network/port-usages/{usage['id']}"),
    ]
    assert all(
        method == "GET" for route, method, _ in gateway.calls if route == "network-api"
    )


async def test_network_errors_reach_the_model(client, seeded_db, admin_token, gateway):
    await _map(client, admin_token)
    key = await _create_api_key(
        seeded_db, "recorder", routes=["mcp-api", "network-api", "network-write-api"]
    )

    same_region = await _call(
        client, key, "network_record_opening", {"src_host": "a1", "dst_host": "a2", "port_from": 80}
    )
    assert same_region["isError"] is True
    assert _text(same_region).startswith("422: ") and "same region" in _text(same_region)

    await _call(client, key, "network_record_opening", {"src_host": "a1", "dst_host": "b1", "port_from": 80})
    duplicate = await _call(
        client, key, "network_record_opening", {"src_host": "a1", "dst_host": "b1", "port_from": 80}
    )
    assert _text(duplicate).startswith("409: ") and "already recorded" in _text(duplicate)

    unknown = await _call(client, key, "network_check_access", {"src": "ghost", "dst": "b1", "port": 80})
    assert _text(unknown).startswith("404: ")

    nothing = await _call(client, key, "network_update_host", {"host": "a1"})
    assert _text(nothing) == "Nothing to change: pass at least one field"

    gateway.reply = httpx.Response(403, json={"message": "The consumer_name is forbidden."})
    refused = await _call(client, key, "network_overview", {})
    assert _text(refused) == "403: The consumer_name is forbidden."


async def test_admin_preview_lists_the_network_group(client, admin_token, seeded_db):
    await _create_api_key(seeded_db, routes=["network-api"])
    resp = await client.get(
        "/admin/mcp/preview", params={"consumer": "agent"}, headers=auth_header(admin_token)
    )
    assert resp.status_code == 200
    groups = {group["group"]: group for group in resp.json()["groups"]}
    assert groups["network"] == {
        "group": "network",
        "enabled": True,
        "granted": True,
        "required_route": "network-api",
        "server": None,
    }
    tools = [tool["name"] for tool in resp.json()["tools"] if tool["group"] == "network"]
    assert tools == READ_TOOLS
