"""Network map: regions, hosts, firewall openings, port usage and access checks."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import create_token, invalidate_permission_cache
from app.models import ApiKeyAccess, DBConnection, MonitoredHost, Role, RolePermission
from app.services import network_map
from tests.conftest import auth_header


@pytest.fixture(autouse=True)
def _no_gateway(monkeypatch):
    """Discovery reads APISIX upstreams; tests feed them instead of dialing a gateway."""
    upstreams = AsyncMock(return_value={"items": []})
    monkeypatch.setattr(network_map.apisix_client, "list_resources", upstreams)
    network_map.clear_gateway_cache()
    yield upstreams
    network_map.clear_gateway_cache()


async def _api_key(seeded_db, name: str, routes: list[str]) -> dict[str, str]:
    session = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session() as db:
        db.add(ApiKeyAccess(consumer_name=name, allowed_routes=json.dumps(routes)))
        await db.commit()
    return {"X-Consumer-Username": name}


async def _role_token(seeded_db, perms: list[str]) -> dict[str, str]:
    session = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session() as db:
        role = Role(name="netviewer", description="", is_system=False)
        db.add(role)
        await db.flush()
        for perm in perms:
            db.add(RolePermission(role_id=role.id, permission=perm))
        await db.commit()
    await invalidate_permission_cache()
    return auth_header(create_token("viewer", "netviewer"))


async def _setup(client, h) -> dict[str, int]:
    """Two regions (with CIDRs) and three hosts: a1, a2 in A, b1 in B."""
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


# ── Regions & hosts ─────────────────────────────────────────────────────────


async def test_host_is_placed_by_region_cidr_and_verified_when_a_person_writes(client, admin_token):
    h = auth_header(admin_token)
    ids = await _setup(client, h)
    host = (await client.get(f"/network/hosts/{ids['b1']}", headers=h)).json()
    assert host["region_name"] == "B"
    assert host["source"] == "manual" and host["verified"] is True
    assert host["created_by"] == "testadmin"

    regions = (await client.get("/network/regions", headers=h)).json()
    assert {r["name"]: r["host_count"] for r in regions} == {"A": 2, "B": 1}


async def test_region_validation_and_delete_rules(client, admin_token):
    h = auth_header(admin_token)
    ids = await _setup(client, h)
    bad = await client.post("/network/regions", headers=h, json={"name": "C", "cidrs": ["10.0.0.300/8"]})
    assert bad.status_code == 422
    dup = await client.post("/network/regions", headers=h, json={"name": "A"})
    assert dup.status_code == 409
    # Normalized: host bits dropped.
    made = await client.post("/network/regions", headers=h, json={"name": "C", "cidrs": ["10.3.1.5/16"]})
    assert made.json()["cidrs"] == ["10.3.0.0/16"]
    # A region with hosts cannot go; an empty one can.
    assert (await client.delete(f"/network/regions/{ids['A']}", headers=h)).status_code == 409
    assert (await client.delete(f"/network/regions/{made.json()['id']}", headers=h)).status_code == 204


async def test_host_region_by_name_explicit_unassign_and_bad_ip(client, admin_token):
    h = auth_header(admin_token)
    ids = await _setup(client, h)
    resp = await client.post(
        "/network/hosts", headers=h, json={"name": "x1", "ip": "192.168.0.9", "region": "b"}
    )
    assert resp.status_code == 201 and resp.json()["region_id"] == ids["B"]
    resp = await client.put(f"/network/hosts/{resp.json()['id']}", headers=h, json={"region_id": None})
    assert resp.json()["region_id"] is None
    assert (await client.post(
        "/network/hosts", headers=h, json={"name": "x2", "ip": "not-an-ip"}
    )).status_code == 422
    assert (await client.post(
        "/network/hosts", headers=h, json={"name": "x3", "ip": "10.9.9.9", "region": "nope"}
    )).status_code == 422
    assert (await client.post(
        "/network/hosts", headers=h, json={"name": "a1", "ip": "10.9.9.9"}
    )).status_code == 409


# ── Openings & access checks ────────────────────────────────────────────────


async def test_same_region_is_open_without_an_opening(client, admin_token):
    h = auth_header(admin_token)
    await _setup(client, h)
    resp = await client.get("/network/check", headers=h, params={"src": "a1", "dst": "10.1.0.2", "port": 5432})
    body = resp.json()
    assert (body["status"], body["reason"]) == ("open", "same_region")
    assert body["request_draft"] is None

    rejected = await client.post(
        "/network/openings", headers=h,
        json={"src_host": "a1", "dst_host": "a2", "port_from": 5432},
    )
    assert rejected.status_code == 422
    assert "same region" in rejected.json()["detail"]


async def test_cross_region_blocked_until_an_opening_is_recorded(client, admin_token):
    h = auth_header(admin_token)
    ids = await _setup(client, h)
    params = {"src": "a1", "dst": "b1", "port": 5432}
    blocked = (await client.get("/network/check", headers=h, params=params)).json()
    assert (blocked["status"], blocked["reason"]) == ("blocked", "no_opening")
    assert "목적지: b1 (10.2.0.1) / region B" in blocked["request_draft"]
    assert "TCP 5432" in blocked["request_draft"]

    made = await client.post(
        "/network/openings", headers=h,
        json={"src_host": "a1", "dst_host_id": ids["b1"], "port_from": 5400, "port_to": 5500,
              "ticket_no": "FW-123", "purpose": "orders db"},
    )
    assert made.status_code == 201, made.text
    opening = made.json()
    assert (opening["src_region_name"], opening["dst_region_name"]) == ("A", "B")
    assert opening["verified"] is True and opening["in_use"] is False

    allowed = (await client.get("/network/check", headers=h, params=params)).json()
    assert (allowed["status"], allowed["reason"]) == ("open", "opening")
    assert [o["ticket_no"] for o in allowed["openings"]] == ["FW-123"]
    # One direction only: b1 → a1 is still blocked.
    reverse = (await client.get(
        "/network/check", headers=h, params={"src": "b1", "dst": "a1", "port": 5432}
    )).json()
    assert reverse["status"] == "blocked"
    # Other protocol / port outside the range: blocked.
    for extra in ({"protocol": "udp"}, {"port": 6000}):
        other = (await client.get("/network/check", headers=h, params={**params, **extra})).json()
        assert other["status"] == "blocked"

    closed = await client.put(f"/network/openings/{opening['id']}", headers=h, json={"status": "closed"})
    assert closed.status_code == 200
    after = (await client.get("/network/check", headers=h, params=params)).json()
    assert (after["status"], after["reason"]) == ("blocked", "opening_closed")


async def test_cidr_source_covers_unregistered_ips_and_duplicates_conflict(client, admin_token):
    h = auth_header(admin_token)
    await _setup(client, h)
    body = {"src_cidr": "10.1.5.0/24", "dst_host": "b1", "port_from": 443}
    assert (await client.post("/network/openings", headers=h, json=body)).status_code == 201
    dup = await client.post("/network/openings", headers=h, json=body)
    assert dup.status_code == 409 and isinstance(dup.json()["detail"]["id"], int)

    inside = (await client.get(
        "/network/check", headers=h, params={"src": "10.1.5.77", "dst": "b1", "port": 443}
    )).json()
    assert (inside["status"], inside["src"]["region_name"]) == ("open", "A")
    outside = (await client.get(
        "/network/check", headers=h, params={"src": "10.1.6.1", "dst": "b1", "port": 443}
    )).json()
    assert outside["status"] == "blocked"


async def test_unknown_region_and_unknown_host(client, admin_token):
    h = auth_header(admin_token)
    await _setup(client, h)
    await client.post("/network/hosts", headers=h, json={"name": "lost", "ip": "172.16.0.1"})
    unknown = (await client.get(
        "/network/check", headers=h, params={"src": "lost", "dst": "b1", "port": 22}
    )).json()
    assert (unknown["status"], unknown["reason"]) == ("unknown", "unknown_region")
    missing = await client.get(
        "/network/check", headers=h, params={"src": "nosuchhost", "dst": "b1", "port": 22}
    )
    assert missing.status_code == 404


async def test_opening_validation(client, admin_token):
    h = auth_header(admin_token)
    await _setup(client, h)
    cases = [
        {"dst_host": "b1", "port_from": 1},  # no source
        {"src_host": "a1", "src_cidr": "10.0.0.0/8", "dst_host": "b1", "port_from": 1},  # two sources
        {"src_host": "a1", "dst_host": "b1", "port_from": 10, "port_to": 5},
        {"src_host": "a1", "dst_host": "b1", "port_from": 70000},
        {"src_host": "a1", "dst_host": "nosuch", "port_from": 22},
        {"src_host": "b1", "dst_host": "b1", "port_from": 22},
    ]
    for body in cases:
        resp = await client.post("/network/openings", headers=h, json=body)
        assert resp.status_code == 422, (body, resp.text)


async def test_deleting_a_host_removes_its_openings_and_usages(client, admin_token):
    h = auth_header(admin_token)
    ids = await _setup(client, h)
    await client.post("/network/openings", headers=h, json={"src_host": "a1", "dst_host": "b1", "port_from": 22})
    await client.post("/network/port-usages", headers=h, json={"host": "b1", "port": 22, "service_name": "sshd"})
    assert (await client.delete(f"/network/hosts/{ids['b1']}", headers=h)).status_code == 204
    assert (await client.get("/network/openings", headers=h)).json() == []
    assert (await client.get("/network/port-usages", headers=h)).json() == []


# ── Port usage, discovery and the overview ──────────────────────────────────


async def test_in_use_flags_and_overview_counts(client, admin_token):
    h = auth_header(admin_token)
    ids = await _setup(client, h)
    used = (await client.post(
        "/network/openings", headers=h, json={"src_host": "a1", "dst_host": "b1", "port_from": 8080}
    )).json()
    await client.post("/network/openings", headers=h, json={"src_host": "a1", "dst_host": "b1", "port_from": 9999})
    usage = await client.post(
        "/network/port-usages", headers=h,
        json={"host": "10.2.0.1", "port": 8080, "service_name": "orders-api"},
    )
    assert usage.status_code == 201
    dup = await client.post(
        "/network/port-usages", headers=h,
        json={"host_id": ids["b1"], "port": 8080, "service_name": "orders-api"},
    )
    assert dup.status_code == 409

    openings = {o["port_from"]: o for o in (await client.get("/network/openings", headers=h)).json()}
    assert openings[8080]["in_use"] is True and openings[9999]["in_use"] is False
    assert openings[8080]["id"] == used["id"]

    overview = (await client.get("/network/overview", headers=h)).json()
    assert overview["opening_count"] == 2 and overview["unused_openings"] == 1
    [link] = overview["links"]
    assert (link["src_region_id"], link["dst_region_id"]) == (ids["A"], ids["B"])
    assert link["ports"] == ["tcp/8080", "tcp/9999"]

    detail = (await client.get(f"/network/hosts/{ids['b1']}", headers=h)).json()
    assert [o["port_from"] for o in detail["inbound"]] == [8080, 9999]
    assert [u["service_name"] for u in detail["usages"]] == ["orders-api"]


async def test_discovered_usages_and_platform_reachability(client, admin_token, seeded_db, _no_gateway):
    h = auth_header(admin_token)
    ids = await _setup(client, h)
    session = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session() as db:
        db.add(DBConnection(alias="orders", db_type="postgres", host="10.2.0.1", port=5432,
                            database="orders", username="u", password_encrypted="x"))
        db.add(MonitoredHost(name="node-b1", address="b1:39100"))
        await db.commit()
    _no_gateway.return_value = {"items": [{"id": "u1", "name": "orders-api", "nodes": {"10.2.0.1:8000": 1}}]}

    usages = (await client.get("/network/port-usages", headers=h, params={"source": "auto"})).json()
    assert {(u["port"], u["auto_kind"]) for u in usages} == {
        (5432, "db"), (39100, "node_exporter"), (8000, "gateway_upstream"),
    }
    # No platform host yet: reachability unknown.
    assert {u["reachable_from_platform"] for u in usages} == {None}

    assert (await client.put("/network/settings", headers=h, json={"platform_host_id": ids["a1"]})).status_code == 200
    await client.post("/network/openings", headers=h, json={"src_host": "a1", "dst_host": "b1", "port_from": 5432})
    usages = (await client.get("/network/port-usages", headers=h, params={"source": "auto"})).json()
    reach = {u["port"]: u["reachable_from_platform"] for u in usages}
    assert reach == {5432: True, 39100: False, 8000: False}
    overview = (await client.get("/network/overview", headers=h)).json()
    assert overview["unreachable_usages"] == 2 and overview["auto_sources_failed"] == []

    _no_gateway.side_effect = RuntimeError("apisix down")
    network_map.clear_gateway_cache()  # past the 30s cache
    overview = (await client.get("/network/overview", headers=h)).json()
    assert overview["auto_sources_failed"] == ["gateway_upstream"]


async def test_import_monitored_servers_creates_and_links(client, admin_token, seeded_db):
    h = auth_header(admin_token)
    await _setup(client, h)
    session = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session() as db:
        db.add_all([
            MonitoredHost(name="b1-node", address="10.2.0.1:39100"),  # IP of b1 → link
            MonitoredHost(name="newbox", address="10.1.9.9:39100"),  # new, region A by CIDR
            MonitoredHost(name="dnsbox", address="box.example.com:39100"),  # not an IP
        ])
        await db.commit()
    result = (await client.post("/network/hosts/import-monitored", headers=h)).json()
    assert result["created"] == ["newbox"]
    assert result["linked"] == ["b1-node → b1"]
    assert [s["name"] for s in result["skipped"]] == ["dnsbox"]
    hosts = {x["name"]: x for x in (await client.get("/network/hosts", headers=h)).json()}
    assert hosts["newbox"]["region_name"] == "A" and hosts["newbox"]["source"] == "import"
    assert hosts["b1"]["monitored_host_name"] == "b1-node"
    # Running it again changes nothing.
    again = (await client.post("/network/hosts/import-monitored", headers=h)).json()
    assert again["created"] == [] and again["linked"] == []


# ── Who may do what ─────────────────────────────────────────────────────────


async def test_agent_writes_are_unverified_until_a_person_confirms(client, admin_token, seeded_db):
    h = auth_header(admin_token)
    await _setup(client, h)
    agent = await _api_key(seeded_db, "fw-agent", ["network-api", "network-write-api"])

    made = await client.post(
        "/network/openings", headers=agent,
        json={"src_host": "a1", "dst_host": "b1", "port_from": 6379, "ticket_no": "FW-9"},
    )
    assert made.status_code == 201, made.text
    opening = made.json()
    assert (opening["source"], opening["verified"], opening["created_by"]) == (
        "agent", False, "apikey:fw-agent"
    )
    unverified = (await client.get("/network/openings", headers=agent, params={"unverified": True})).json()
    assert [o["id"] for o in unverified] == [opening["id"]]

    # Agents cannot verify (people only), a person can.
    assert (await client.post(f"/network/openings/{opening['id']}/verify", headers=agent)).status_code == 401
    verified = await client.post(f"/network/openings/{opening['id']}/verify", headers=h)
    assert verified.json()["verified"] is True and verified.json()["verified_by"] == "testadmin"

    # An agent edit puts it back to unverified.
    edited = await client.put(f"/network/openings/{opening['id']}", headers=agent, json={"purpose": "cache"})
    assert edited.json()["verified"] is False
    assert (await client.get("/network/overview", headers=h)).json()["unverified_count"] == 1


async def test_api_key_needs_the_matching_route_grant(client, admin_token, seeded_db):
    h = auth_header(admin_token)
    await _setup(client, h)
    reader = await _api_key(seeded_db, "fw-reader", ["network-api"])
    nobody = await _api_key(seeded_db, "other-key", ["query-api"])

    assert (await client.get("/network/hosts", headers=reader)).status_code == 200
    denied = await client.post("/network/hosts", headers=reader, json={"name": "z", "ip": "10.9.0.1"})
    assert denied.status_code == 403 and "network-write-api" in denied.json()["detail"]
    assert (await client.get("/network/hosts", headers=nobody)).status_code == 403


async def test_person_permissions(client, seeded_db, user_token):
    viewer = await _role_token(seeded_db, ["network.read"])
    assert (await client.get("/network/overview", headers=viewer)).status_code == 200
    assert (await client.post("/network/regions", headers=viewer, json={"name": "Z"})).status_code == 403
    assert (await client.put("/network/settings", headers=viewer, json={})).status_code == 403
    plain = auth_header(user_token)
    assert (await client.get("/network/overview", headers=plain)).status_code == 403


async def test_settings_reject_unknown_platform_host(client, admin_token):
    h = auth_header(admin_token)
    resp = await client.put("/network/settings", headers=h, json={"platform_host_id": 999})
    assert resp.status_code == 422


# ── Pure helpers ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("10.0.0.1:9100", ("10.0.0.1", 9100)),
        ("[fe80::1]:9400", ("fe80::1", 9400)),
        ("host.example.com", ("host.example.com", None)),
        ("fe80::1", ("fe80::1", None)),
        ("", None),
    ],
)
def test_split_host_port(address, expected):
    assert network_map.split_host_port(address) == expected


# ── Review follow-ups ───────────────────────────────────────────────────────


async def test_opening_from_a_cidr_outside_every_region_still_opens(client, admin_token):
    h = auth_header(admin_token)
    await _setup(client, h)
    await client.post("/network/openings", headers=h, json={"src_cidr": "10.200.0.0/16", "dst_host": "b1", "port_from": 5432})
    hit = (await client.get("/network/check", headers=h, params={"src": "10.200.1.5", "dst": "b1", "port": 5432})).json()
    assert (hit["status"], hit["reason"]) == ("open", "opening")
    # No opening and a region unknown: not provably blocked.
    miss = (await client.get("/network/check", headers=h, params={"src": "10.200.1.5", "dst": "b1", "port": 22})).json()
    assert (miss["status"], miss["reason"]) == ("unknown", "unknown_region")


async def test_opening_stays_editable_after_its_hosts_end_up_in_one_region(client, admin_token):
    h = auth_header(admin_token)
    ids = await _setup(client, h)
    opening = (await client.post("/network/openings", headers=h, json={"src_host": "a1", "dst_host": "b1", "port_from": 22})).json()
    await client.put(f"/network/hosts/{ids['b1']}", headers=h, json={"region_id": ids["A"]})
    closed = await client.put(f"/network/openings/{opening['id']}", headers=h, json={"status": "closed", "ticket_no": "T-1"})
    assert closed.status_code == 200, closed.text
    # Pointing it at a same-region pair anew is still refused.
    moved = await client.put(f"/network/openings/{opening['id']}", headers=h, json={"dst_host": "a2"})
    assert moved.status_code == 422


async def test_agents_cannot_delete_confirmed_records_or_define_regions(client, admin_token, seeded_db):
    h = auth_header(admin_token)
    ids = await _setup(client, h)
    agent = await _api_key(seeded_db, "fw-agent", ["network-api", "network-write-api"])
    confirmed = (await client.post("/network/openings", headers=h, json={"src_host": "a1", "dst_host": "b1", "port_from": 22})).json()
    assert (await client.delete(f"/network/openings/{confirmed['id']}", headers=agent)).status_code == 403
    # b1 has a confirmed opening, so the host cannot go either.
    assert (await client.delete(f"/network/hosts/{ids['b1']}", headers=agent)).status_code == 403
    # Its own unconfirmed records it may remove.
    own = (await client.post("/network/openings", headers=agent, json={"src_host": "a1", "dst_host": "b1", "port_from": 80})).json()
    assert (await client.delete(f"/network/openings/{own['id']}", headers=agent)).status_code == 204
    # Regions are defined by people.
    assert (await client.post("/network/regions", headers=agent, json={"name": "Z"})).status_code == 401
    assert (await client.put(f"/network/regions/{ids['A']}", headers=agent, json={"cidrs": ["0.0.0.0/0"]})).status_code == 401


async def test_host_names_cannot_shadow_ips_or_differ_only_in_case(client, admin_token):
    h = auth_header(admin_token)
    ids = await _setup(client, h)
    ip_name = await client.post("/network/hosts", headers=h, json={"name": "10.2.0.1", "ip": "10.1.0.9"})
    assert ip_name.status_code == 422
    assert (await client.post("/network/hosts", headers=h, json={"name": "A1", "ip": "10.1.0.9"})).status_code == 409
    renamed = await client.put(f"/network/hosts/{ids['a2']}", headers=h, json={"name": "B1"})
    assert renamed.status_code == 409
    # Duplicate IP on update is a conflict, not a server error.
    assert (await client.put(f"/network/hosts/{ids['a2']}", headers=h, json={"ip": "10.2.0.1"})).status_code == 409


async def test_non_master_keys_see_discovered_usages_without_config_names(client, admin_token, seeded_db):
    h = auth_header(admin_token)
    await _setup(client, h)
    session = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session() as db:
        db.add(DBConnection(alias="secret-orders", db_type="postgres", host="10.2.0.1", port=5432,
                            database="orders", username="u", password_encrypted="x"))
        await db.commit()
    scoped = await _api_key(seeded_db, "scoped", ["network-api"])
    master = await _api_key(seeded_db, "master", ["*"])
    [seen] = (await client.get("/network/port-usages", headers=scoped, params={"source": "auto"})).json()
    assert seen["service_name"] == "database (discovered)" and "secret" not in json.dumps(seen)
    [full] = (await client.get("/network/port-usages", headers=master, params={"source": "auto"})).json()
    assert full["service_name"] == "postgres DB 'secret-orders'"
    check = (await client.get("/network/check", headers=scoped, params={"src": "a1", "dst": "b1", "port": 5432})).json()
    assert "secret" not in json.dumps(check)
