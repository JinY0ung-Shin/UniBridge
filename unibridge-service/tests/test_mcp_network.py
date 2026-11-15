"""MCP network-map tools: offered by grant, sent out on the network routes."""
from __future__ import annotations

import httpx
import pytest

from app.services import mcp_tools
from tests.conftest import auth_header
from tests.helpers import HOSTS_A2_B1, REGIONS_AB, seed_network
from tests.mcp_helpers import (
    create_mcp_key,
    mcp_call,
    mcp_text,
    mcp_tool_names,
    put_mcp_settings,
)

pytestmark = pytest.mark.usefixtures("no_gateway")


READ_TOOLS = [tool.name for tool in mcp_tools.NETWORK_READ_TOOLS]
WRITE_TOOLS = [tool.name for tool in mcp_tools.NETWORK_WRITE_TOOLS]


async def _map(client, admin_token) -> dict[str, int]:
    """Regions A (10.1/16) and B (10.2/16); hosts a1, a2 in A and b1 in B."""
    return await seed_network(client, auth_header(admin_token), REGIONS_AB, HOSTS_A2_B1)


def _json(result: dict) -> object:
    import json

    assert result["isError"] is False, result
    return json.loads(mcp_text(result))


async def test_network_tools_follow_the_grants_and_the_group(client, seeded_db, admin_token, gateway):
    none = await create_mcp_key(seeded_db, "plain", routes=["mcp-api", "query-api"])
    reader = await create_mcp_key(seeded_db, "reader", routes=["mcp-api", "network-api"])
    writer = await create_mcp_key(
        seeded_db, "writer", routes=["mcp-api", "network-api", "network-write-api"]
    )
    # The write grant alone offers nothing: the tools look hosts up on network-api.
    write_only = await create_mcp_key(seeded_db, "write-only", routes=["mcp-api", "network-write-api"])

    assert not any(name.startswith("network_") for name in await mcp_tool_names(client, none))
    assert await mcp_tool_names(client, reader) == READ_TOOLS
    assert await mcp_tool_names(client, writer) == READ_TOOLS + WRITE_TOOLS
    assert await mcp_tool_names(client, write_only) == []

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

    await put_mcp_settings(client, admin_token, network=False)
    assert await mcp_tool_names(client, writer) == []
    assert gateway.calls == []


async def test_read_tools_go_out_on_network_api(client, seeded_db, admin_token, gateway):
    ids = await _map(client, admin_token)
    key = await create_mcp_key(seeded_db, "reader", routes=["mcp-api", "network-api"])

    regions = _json(await mcp_call(client, key, "network_list_regions", {}))
    assert [r["name"] for r in regions["regions"]] == ["A", "B"]
    found = _json(await mcp_call(client, key, "network_find_hosts", {"query": "10.1.", "limit": 1}))
    assert [h["name"] for h in found["hosts"]] == ["a1"]
    assert found["total"] == 2 and found["truncated"] is True
    # By name, IP or id.
    for ref in ("B1", "10.2.0.1", str(ids["b1"])):
        host = _json(await mcp_call(client, key, "network_get_host", {"host": ref}))
        assert host["id"] == ids["b1"]
    missing = await mcp_call(client, key, "network_get_host", {"host": "nope"})
    assert missing["isError"] is True and "network_find_hosts" in mcp_text(missing)
    overview = _json(await mcp_call(client, key, "network_overview", {}))
    assert overview["host_count"] == 3

    get_calls = [call for call in gateway.calls if call[0] == "network-api"]
    assert get_calls == gateway.calls
    assert ("network-api", "GET", "/api/network/regions") in gateway.calls
    assert ("network-api", "GET", f"/api/network/hosts/{ids['b1']}") in gateway.calls


async def test_check_access_explains_and_drafts_a_request(client, seeded_db, admin_token, gateway):
    await _map(client, admin_token)
    key = await create_mcp_key(seeded_db, "reader", routes=["mcp-api", "network-api"])

    same = _json(await mcp_call(client, key, "network_check_access", {"src": "a1", "dst": "a2", "port": 22}))
    assert same["status"] == "open" and same["reason"] == "same_region"
    blocked = _json(
        await mcp_call(client, key, "network_check_access", {"src": "a1", "dst": "10.2.0.1", "port": 5432})
    )
    assert blocked["status"] == "blocked"
    assert blocked["request_draft"]
    assert gateway.calls[-1] == ("network-api", "GET", "/api/network/check")


async def test_find_ports_places_a_service_where_sources_already_get_through(
    client, seeded_db, admin_token, gateway
):
    ids = await _map(client, admin_token)
    h = auth_header(admin_token)
    await client.post("/network/openings", headers=h, json={
        "src_host": "a1", "dst_host": "b1", "port_from": 8000, "port_to": 8002,
    })
    await client.post("/network/port-usages", headers=h, json={"host": "b1", "port": 8000, "service_name": "x"})
    key = await create_mcp_key(seeded_db, "reader", routes=["mcp-api", "network-api"])

    one = _json(await mcp_call(client, key, "network_find_ports", {"sources": ["a1"], "dst": "b1"}))
    assert [s["port"] for s in one["hosts"][0]["suggestions"]] == [8001, 8002]
    assert gateway.calls[-1] == ("network-api", "GET", "/api/network/port-finder")

    ranked = _json(await mcp_call(client, key, "network_find_ports", {"sources": ["a1"], "limit": 5}))
    assert [r["host"]["name"] for r in ranked["hosts"]] == ["a2", "b1"]  # same region first
    region = _json(await mcp_call(client, key, "network_find_ports", {"source_region_ids": [ids["A"]]}))
    assert region["hosts"] == []  # a2 is in A too and has no opening to b1
    both = _json(await mcp_call(client, key, "network_find_ports", {"sources": ["a1", "a2"], "port": 8001}))
    assert both["hosts"] == []  # a2 has no opening to b1

    nothing = await mcp_call(client, key, "network_find_ports", {})
    assert nothing["isError"] is True and "sources" in mcp_text(nothing)


async def test_write_tools_go_out_on_network_write_api_and_stay_unverified(
    client, seeded_db, admin_token, gateway
):
    ids = await _map(client, admin_token)
    key = await create_mcp_key(
        seeded_db, "recorder", routes=["mcp-api", "network-api", "network-write-api"]
    )

    # Regions are defined by people; there is no tool for them.
    assert "network_create_region" not in await mcp_tool_names(client, key)
    host = _json(await mcp_call(client, key, "network_register_host", {"name": "c1", "ip": "10.2.0.9"}))
    assert host["region_name"] == "B"
    assert host["source"] == "agent" and host["verified"] is False
    updated = _json(await mcp_call(client, key, "network_update_host", {"host": "c1", "owner": "infra"}))
    assert updated["owner"] == "infra"

    opening = _json(
        await mcp_call(
            client,
            key,
            "network_record_opening",
            {"src_host": "a1", "dst_host": "b1", "port_from": 5432, "ticket_no": "FW-1"},
        )
    )
    assert opening["verified"] is False and opening["port_to"] == 5432
    closed = _json(
        await mcp_call(client, key, "network_update_opening", {"opening_id": opening["id"], "status": "closed"})
    )
    assert closed["status"] == "closed"
    listed = _json(await mcp_call(client, key, "network_list_openings", {"host": "b1"}))
    assert [o["id"] for o in listed["openings"]] == [opening["id"]]
    deleted = _json(await mcp_call(client, key, "network_delete_opening", {"opening_id": opening["id"]}))
    assert deleted == {"deleted": True, "opening_id": opening["id"]}

    usage = _json(
        await mcp_call(
            client, key, "network_record_port_usage", {"host": "b1", "port": 5432, "service_name": "pg"}
        )
    )
    assert usage["source"] == "agent" and usage["verified"] is False
    usages = _json(await mcp_call(client, key, "network_list_port_usages", {"host": str(ids["b1"])}))
    assert [u["service_name"] for u in usages["port_usages"]] == ["pg"]
    _json(await mcp_call(client, key, "network_delete_port_usage", {"usage_id": usage["id"]}))

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
    key = await create_mcp_key(
        seeded_db, "recorder", routes=["mcp-api", "network-api", "network-write-api"]
    )

    same_region = await mcp_call(
        client, key, "network_record_opening", {"src_host": "a1", "dst_host": "a2", "port_from": 80}
    )
    assert same_region["isError"] is True
    assert mcp_text(same_region).startswith("422: ") and "same region" in mcp_text(same_region)

    await mcp_call(client, key, "network_record_opening", {"src_host": "a1", "dst_host": "b1", "port_from": 80})
    duplicate = await mcp_call(
        client, key, "network_record_opening", {"src_host": "a1", "dst_host": "b1", "port_from": 80}
    )
    assert mcp_text(duplicate).startswith("409: ") and "already recorded" in mcp_text(duplicate)

    unknown = await mcp_call(client, key, "network_check_access", {"src": "ghost", "dst": "b1", "port": 80})
    assert mcp_text(unknown).startswith("404: ")

    nothing = await mcp_call(client, key, "network_update_host", {"host": "a1"})
    assert mcp_text(nothing) == "Nothing to change: pass at least one field"

    gateway.reply = httpx.Response(403, json={"message": "The consumer_name is forbidden."})
    refused = await mcp_call(client, key, "network_overview", {})
    assert mcp_text(refused) == "403: The consumer_name is forbidden."


async def test_admin_preview_lists_the_network_group(client, admin_token, seeded_db):
    await create_mcp_key(seeded_db, routes=["network-api"])
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


async def test_probe_tools_measure_from_the_source_and_report_mismatches(
    client, seeded_db, admin_token, gateway
):
    ids = await _map(client, admin_token)
    h = auth_header(admin_token)
    # a1 → b1:5432 is recorded open; nothing is recorded for a1 → b1:22.
    resp = await client.post(
        "/network/openings", headers=h,
        json={"src_host": "a1", "dst_host": "b1", "port_from": 5432},
    )
    assert resp.status_code == 201, resp.text
    key = await create_mcp_key(
        seeded_db, "prober", routes=["mcp-api", "network-api", "network-write-api"]
    )

    targets = _json(await mcp_call(client, key, "network_probe_targets", {"src": "10.1.0.1"}))
    assert targets["src_host_name"] == "a1"
    assert [(t["dst_host_name"], t["port"], t["expected"]) for t in targets["targets"]] == [
        ("b1", 5432, "open")
    ]
    assert targets["truncated"] is False
    unknown = await mcp_call(client, key, "network_probe_targets", {"src": "ghost"})
    assert unknown["isError"] is True and mcp_text(unknown).startswith("404: ")

    report = _json(
        await mcp_call(
            client,
            key,
            "network_report_probes",
            {
                "src_host": "a1",
                "results": [
                    # The recorded opening times out: it may have expired.
                    {"dst_host": "b1", "port": 5432, "result": "timeout"},
                    # Refused = the firewall let it through, though nothing is recorded.
                    {"dst_host": "10.2.0.1", "port": 22, "result": "refused", "latency_ms": 1.5},
                    {"dst_host": "ghost", "port": 80, "result": "open"},
                ],
            },
        )
    )
    assert report["stored"] == 2
    assert [e["index"] for e in report["errors"]] == [2]
    assert {(m["port"], m["mismatch"]) for m in report["mismatches"]} == {
        (5432, "recorded_open_but_blocked"),
        (22, "passes_without_opening"),
    }

    probes = _json(await mcp_call(client, key, "network_list_probes", {"host": "b1"}))
    assert probes["total"] == 2
    assert {p["reported_by"] for p in probes["probes"]} == {"apikey:prober"}
    assert {p["src_host_id"] for p in probes["probes"]} == {ids["a1"]}
    only_timeouts = _json(await mcp_call(client, key, "network_list_probes", {"result": "timeout"}))
    assert [p["port"] for p in only_timeouts["probes"]] == [5432]
    mismatched = _json(await mcp_call(client, key, "network_list_probes", {"mismatch": True}))
    assert mismatched["total"] == 2

    assert ("network-write-api", "POST", "/api/network/probes") in gateway.calls
    assert ("network-api", "GET", "/api/network/probe-targets") in gateway.calls
    assert ("network-api", "GET", "/api/network/probes") in gateway.calls


async def test_report_probes_takes_the_source_from_the_call(client, seeded_db, admin_token, gateway):
    await _map(client, admin_token)
    key = await create_mcp_key(
        seeded_db, "prober", routes=["mcp-api", "network-api", "network-write-api"]
    )
    # A per-item source is not taken: every attempt is reported as from src_host.
    report = _json(
        await mcp_call(
            client,
            key,
            "network_report_probes",
            {
                "src_host": "b1",
                "results": [{"dst_host": "a1", "src_host": "a2", "port": 80, "result": "open"}],
            },
        )
    )
    assert report["stored"] == 1 and report["errors"] == []
    probes = _json(await mcp_call(client, key, "network_list_probes", {}))
    assert [p["src_host_name"] for p in probes["probes"]] == ["b1"]

    empty = await mcp_call(client, key, "network_report_probes", {"src_host": "b1", "results": []})
    assert empty["isError"] is True and "non-empty" in mcp_text(empty)
    too_many = await mcp_call(
        client,
        key,
        "network_report_probes",
        {"src_host": "b1", "results": [{"dst_host": "a1", "port": 80, "result": "open"}] * 501},
    )
    assert too_many["isError"] is True and "At most 500" in mcp_text(too_many)


async def test_probe_tools_follow_the_grants(client, seeded_db, gateway):
    reader = await create_mcp_key(seeded_db, "reader", routes=["mcp-api", "network-api"])
    names = await mcp_tool_names(client, reader)
    assert {"network_list_probes", "network_probe_targets"} <= set(names)
    assert "network_report_probes" not in names
    tools = (
        await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=reader
        )
    ).json()["result"]["tools"]
    targets = next(tool for tool in tools if tool["name"] == "network_probe_targets")
    assert "network_report_probes" in targets["description"]
    report = next(tool for tool in mcp_tools.NETWORK_WRITE_TOOLS if tool.name == "network_report_probes")
    for word in ("open", "refused", "timeout", "error", "FROM src_host"):
        assert word in report.description
