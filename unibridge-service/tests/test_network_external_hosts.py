"""Network map: external hosts (someone else's servers) and openings to unregistered IPs (#99)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AdminAuditLog, ApiKeyAccess, MonitoredHost, NetworkHost
from app.services import network_map
from tests.conftest import auth_header


@pytest.fixture(autouse=True)
def _no_gateway(monkeypatch):
    monkeypatch.setattr(
        network_map.apisix_client, "list_resources", AsyncMock(return_value={"items": []})
    )
    network_map.clear_gateway_cache()
    yield
    network_map.clear_gateway_cache()


def _session(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _setup(client, h) -> dict[str, int]:
    """Regions A (10.1/16) and B (10.2/16); a1, a2 in A, b1 in B."""
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


async def _external(client, h, name="knox", ip="10.2.9.9") -> dict:
    resp = await client.post(
        "/network/hosts", headers=h, json={"name": name, "ip": ip, "kind": "external"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Openings to an IP no host has ───────────────────────────────────────────


async def test_unknown_destination_ip_is_still_422_by_default(client, admin_token):
    h = auth_header(admin_token)
    await _setup(client, h)
    resp = await client.post(
        "/network/openings", headers=h,
        json={"src_host": "a1", "dst_host": "172.20.0.5", "port_from": 443},
    )
    assert resp.status_code == 422
    assert "create_missing_destination" in resp.json()["detail"]
    hosts = (await client.get("/network/hosts", headers=h)).json()
    assert len(hosts) == 3


async def test_opening_to_an_unknown_ip_creates_an_external_host(client, admin_token, seeded_db):
    h = auth_header(admin_token)
    ids = await _setup(client, h)
    resp = await client.post(
        "/network/openings", headers=h,
        json={
            "src_host": "a1", "dst_host": "172.20.0.5", "port_from": 443,
            "create_missing_destination": True, "ticket_no": "F1",
        },
    )
    assert resp.status_code == 201, resp.text
    opening = resp.json()
    assert opening["dst_host_name"] == "dst-172.20.0.5"
    assert opening["dst_host_kind"] == "external" and opening["dst_region_id"] is None

    host = (await client.get(f"/network/hosts/{opening['dst_host_id']}", headers=h)).json()
    assert host["kind"] == "external" and host["region_id"] is None
    assert host["source"] == "manual" and host["verified"] is True
    assert [o["id"] for o in host["inbound"]] == [opening["id"]]

    # The opening matches in the access check; a port it does not cover is unknown, not blocked.
    ok = (await client.get(
        "/network/check", headers=h, params={"src": "a1", "dst": "172.20.0.5", "port": 443}
    )).json()
    assert (ok["status"], ok["reason"]) == ("open", "opening")
    other = (await client.get(
        "/network/check", headers=h, params={"src": "a2", "dst": "dst-172.20.0.5", "port": 443}
    )).json()
    assert (other["status"], other["reason"]) == ("unknown", "external_host")
    assert other["request_draft"] is None

    # A second opening to the same IP reuses the host.
    again = await client.post(
        "/network/openings", headers=h,
        json={"src_host_id": ids["b1"], "dst_host": "172.20.0.5", "port_from": 443,
              "create_missing_destination": True},
    )
    assert again.status_code == 201, again.text
    assert again.json()["dst_host_id"] == opening["dst_host_id"]

    async with _session(seeded_db)() as db:
        logs = (await db.execute(select(AdminAuditLog))).scalars().all()
    assert any(
        log.resource_type == "network_host" and log.resource_id == "dst-172.20.0.5" for log in logs
    )


async def test_missing_destination_needs_an_ip_and_a_free_name(client, admin_token):
    h = auth_header(admin_token)
    await _setup(client, h)
    by_name = await client.post(
        "/network/openings", headers=h,
        json={"src_host": "a1", "dst_host": "mail-server", "port_from": 25,
              "create_missing_destination": True},
    )
    assert by_name.status_code == 422
    assert "only an IP address" in by_name.json()["detail"]

    # dst-<ip> taken by a host with another IP: no silent second host.
    taken = await client.post(
        "/network/hosts", headers=h, json={"name": "dst-172.20.0.6", "ip": "172.20.0.99"}
    )
    assert taken.status_code == 201
    clash = await client.post(
        "/network/openings", headers=h,
        json={"src_host": "a1", "dst_host": "172.20.0.6", "port_from": 25,
              "create_missing_destination": True},
    )
    assert clash.status_code == 409
    assert len((await client.get("/network/hosts", headers=h)).json()) == 4


async def test_failed_opening_leaves_no_external_host_behind(client, admin_token):
    h = auth_header(admin_token)
    await _setup(client, h)
    first = await client.post(
        "/network/openings", headers=h,
        json={"src_cidr": "10.1.0.0/16", "dst_host": "172.20.0.7", "port_from": 22,
              "create_missing_destination": True},
    )
    assert first.status_code == 201
    dup = await client.post(
        "/network/openings", headers=h,
        json={"src_cidr": "10.1.0.0/16", "dst_host": "172.20.0.7", "port_from": 22,
              "create_missing_destination": True},
    )
    assert dup.status_code == 409  # the host exists now, the opening is the duplicate
    bad = await client.post(
        "/network/openings", headers=h,
        json={"src_host": "a1", "dst_host": "172.20.0.8", "port_from": 22,
              "port_to": 21, "create_missing_destination": True},
    )
    assert bad.status_code == 422
    names = {x["name"] for x in (await client.get("/network/hosts", headers=h)).json()}
    assert "dst-172.20.0.8" not in names


async def test_ipv6_destination_name_has_no_colons(client, admin_token):
    h = auth_header(admin_token)
    await _setup(client, h)
    resp = await client.post(
        "/network/openings", headers=h,
        json={"src_host": "a1", "dst_host": "2001:db8::5", "port_from": 443,
              "create_missing_destination": True},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["dst_host_name"] == "dst-2001-db8--5"
    assert resp.json()["dst_host_ip"] == "2001:db8::5"


async def test_agent_created_destination_is_unverified(client, admin_token, seeded_db):
    h = auth_header(admin_token)
    await _setup(client, h)
    async with _session(seeded_db)() as db:
        db.add(ApiKeyAccess(consumer_name="bot", allowed_routes=json.dumps(["network-write-api"])))
        await db.commit()
    resp = await client.post(
        "/network/openings", headers={"X-Consumer-Username": "bot"},
        json={"src_host": "a1", "dst_host": "172.20.1.1", "port_from": 80,
              "create_missing_destination": True},
    )
    assert resp.status_code == 201, resp.text
    host = (await client.get(f"/network/hosts/{resp.json()['dst_host_id']}", headers=h)).json()
    assert host["source"] == "agent" and host["verified"] is False


# ── What an external host is left out of ────────────────────────────────────


async def test_external_host_is_never_placed_in_a_region(client, admin_token):
    h = auth_header(admin_token)
    await _setup(client, h)
    # Its IP is inside B's CIDR, but it is not ours: no CIDR placement.
    ext = await _external(client, h)
    assert ext["kind"] == "external" and ext["region_id"] is None

    with_region = await client.post(
        "/network/hosts", headers=h,
        json={"name": "x", "ip": "10.2.9.8", "kind": "external", "region": "B"},
    )
    assert with_region.status_code == 422
    move = await client.put(f"/network/hosts/{ext['id']}", headers=h, json={"region": "B"})
    assert move.status_code == 422
    # Making it managed lets it be placed.
    managed = await client.put(
        f"/network/hosts/{ext['id']}", headers=h, json={"kind": "managed", "region": "B"}
    )
    assert managed.status_code == 200, managed.text
    assert managed.json()["region_name"] == "B"
    # And switching back takes it out of the region.
    back = await client.put(f"/network/hosts/{ext['id']}", headers=h, json={"kind": "external"})
    assert back.status_code == 200 and back.json()["region_id"] is None


async def test_external_host_is_not_same_region_and_not_counted_as_ours(client, admin_token):
    h = auth_header(admin_token)
    ids = await _setup(client, h)
    ext = await _external(client, h)
    # Even same-IP-range hosts do not reach it by region.
    check = (await client.get(
        "/network/check", headers=h, params={"src": "b1", "dst": "knox", "port": 25}
    )).json()
    assert (check["status"], check["reason"]) == ("unknown", "external_host")

    b1 = (await client.get(f"/network/hosts/{ids['b1']}", headers=h)).json()
    assert ext["id"] not in {x["id"] for x in b1["same_region_hosts"]}

    overview = (await client.get("/network/overview", headers=h)).json()
    assert overview["host_count"] == 3
    assert overview["external_host_count"] == 1
    assert overview["unassigned_hosts"] == 0

    only_ext = (await client.get("/network/hosts", headers=h, params={"kind": "external"})).json()
    assert [x["name"] for x in only_ext] == ["knox"]
    regions = {r["name"]: r["host_count"] for r in (await client.get("/network/regions", headers=h)).json()}
    assert regions == {"A": 2, "B": 1}


async def test_port_finder_skips_external_destinations(client, admin_token):
    h = auth_header(admin_token)
    await _setup(client, h)
    ext = await _external(client, h, ip="172.30.0.1")
    resp = await client.post(
        "/network/openings", headers=h,
        json={"src_host": "a1", "dst_host_id": ext["id"], "port_from": 9000, "port_to": 9100},
    )
    assert resp.status_code == 201
    found = (await client.get("/network/port-finder", headers=h, params={"src": "a1"})).json()
    names = {x["host"]["name"] for x in found["hosts"]}
    assert "knox" not in names and "knox" not in found["unplaced_destinations"]

    direct = await client.get(
        "/network/port-finder", headers=h, params={"src": "a1", "dst": "knox"}
    )
    assert direct.status_code == 422
    assert "external host" in direct.json()["detail"]

    # As a source it is fine, and not reported as waiting to be placed.
    as_source = (await client.get(
        "/network/port-finder", headers=h, params={"src": "knox", "dst": "b1"}
    )).json()
    assert as_source["unplaced_sources"] == []


async def test_external_host_cannot_be_the_platform_or_monitored(client, admin_token, seeded_db):
    h = auth_header(admin_token)
    ids = await _setup(client, h)
    ext = await _external(client, h)
    put = await client.put("/network/settings", headers=h, json={"platform_host_id": ext["id"]})
    assert put.status_code == 422

    assert (await client.put(
        "/network/settings", headers=h, json={"platform_host_id": ids["a1"]}
    )).status_code == 200
    to_ext = await client.put(f"/network/hosts/{ids['a1']}", headers=h, json={"kind": "external"})
    assert to_ext.status_code == 422

    async with _session(seeded_db)() as db:
        mon = MonitoredHost(name="web1", address="10.2.9.9:9100")
        db.add(mon)
        await db.commit()
        mon_id = mon.id
    link = await client.put(
        f"/network/hosts/{ext['id']}", headers=h, json={"monitored_host_id": mon_id}
    )
    assert link.status_code == 422
    created = await client.post(
        "/network/hosts", headers=h,
        json={"name": "y", "ip": "10.9.9.9", "kind": "external", "monitored_host_id": mon_id},
    )
    assert created.status_code == 422

    # The monitored-server import does not link a server to an external host by IP.
    result = (await client.post("/network/hosts/import-monitored", headers=h)).json()
    assert result["linked"] == [] and result["created"] == []
    assert "external host knox" in result["skipped"][0]["reason"]
    async with _session(seeded_db)() as db:
        row = await db.get(NetworkHost, ext["id"])
        assert row.monitored_host_id is None


async def test_linked_host_must_be_unlinked_before_it_turns_external(client, admin_token, seeded_db):
    h = auth_header(admin_token)
    ids = await _setup(client, h)
    async with _session(seeded_db)() as db:
        mon = MonitoredHost(name="web1", address="10.2.0.1:9100")
        db.add(mon)
        await db.commit()
        mon_id = mon.id
    await client.put(f"/network/hosts/{ids['b1']}", headers=h, json={"monitored_host_id": mon_id})
    refused = await client.put(f"/network/hosts/{ids['b1']}", headers=h, json={"kind": "external"})
    assert refused.status_code == 422
    both = await client.put(
        f"/network/hosts/{ids['b1']}", headers=h,
        json={"kind": "external", "monitored_host_id": None},
    )
    assert both.status_code == 200, both.text
    assert both.json()["monitored_host_id"] is None and both.json()["region_id"] is None


# ── CSV import ──────────────────────────────────────────────────────────────


async def _import(client, h, kind, text, dry_run=True, **extra):
    resp = await client.post(
        "/network/import", headers=h,
        json={"kind": kind, "csv": text, "dry_run": dry_run, **extra},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_import_openings_adds_missing_destinations_only_when_asked(client, admin_token):
    h = auth_header(admin_token)
    await _setup(client, h)
    text = (
        "출발지,목적지,포트,신청번호\n"
        "a1,172.20.0.5,443,F1\n"
        "b1,172.20.0.5,443 8443,F2\n"
        "a1,mail-server,25,F3\n"
    )
    default = await _import(client, h, "openings", text)
    assert [r["action"] for r in default["rows"]] == ["error", "error", "error"]
    assert "add missing destinations" in default["rows"][0]["message"]

    preview = await _import(client, h, "openings", text, create_missing_destinations=True)
    assert [r["action"] for r in preview["rows"]] == ["create", "create", "create", "error"]
    assert "adds external host dst-172.20.0.5" in preview["rows"][0]["message"]
    assert "adds external host" not in preview["rows"][1]["message"]  # added once
    assert len((await client.get("/network/hosts", headers=h)).json()) == 3  # dry run

    applied = await _import(
        client, h, "openings", text, dry_run=False, create_missing_destinations=True
    )
    assert applied["created"] == 3
    hosts = {x["name"]: x for x in (await client.get("/network/hosts", headers=h)).json()}
    ext = hosts["dst-172.20.0.5"]
    assert ext["kind"] == "external" and ext["source"] == "import" and ext["verified"] is True
    detail = (await client.get(f"/network/hosts/{ext['id']}", headers=h)).json()
    assert sorted((o["src_host_name"], o["port_from"]) for o in detail["inbound"]) == [
        ("a1", 443), ("b1", 443), ("b1", 8443),
    ]
    # Run again: everything is already recorded, no second host.
    again = await _import(
        client, h, "openings", text, dry_run=False, create_missing_destinations=True
    )
    assert [r["action"] for r in again["rows"]] == ["skip", "skip", "skip", "error"]
    assert len((await client.get("/network/hosts", headers=h)).json()) == 4


async def test_import_hosts_reads_a_kind_column(client, admin_token):
    h = auth_header(admin_token)
    await _setup(client, h)
    text = (
        "서버명,IP,리전,호스트구분,구분\n"
        "nas1,10.2.8.8,,외부,NAS\n"
        "c1,10.2.8.9,,,WEB\n"
        "bad,10.2.8.10,A,external,DB\n"
        "odd,10.2.8.11,,partner,DB\n"
    )
    result = await _import(client, h, "hosts", text, dry_run=False)
    assert [r["action"] for r in result["rows"]] == ["create", "create", "error", "error"]
    hosts = {x["name"]: x for x in (await client.get("/network/hosts", headers=h)).json()}
    assert hosts["nas1"]["kind"] == "external" and hosts["nas1"]["region_id"] is None
    assert hosts["c1"]["kind"] == "managed" and hosts["c1"]["region_name"] == "B"
