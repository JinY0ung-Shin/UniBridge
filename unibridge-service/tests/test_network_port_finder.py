"""Network map: the port finder (where a new service can listen without a
firewall request) and the host neighbourhood the connection map draws."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import ApiKeyAccess
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


async def _agent(seeded_db, name="finder", routes=("network-api", "network-write-api")) -> dict[str, str]:
    session = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session() as db:
        db.add(ApiKeyAccess(consumer_name=name, allowed_routes=json.dumps(list(routes))))
        await db.commit()
    return {"X-Consumer-Username": name}


async def _setup(client, h) -> dict[str, int]:
    """Regions A (a1, a2), B (b1, b2), C (c1); b1 is opened to a1 and a2 on
    8000-8010 and to a1 alone on 9000; 10.1.0.0/16 reaches c1 on 7000-7001."""
    ids: dict[str, int] = {}
    for name, cidr in (("A", "10.1.0.0/16"), ("B", "10.2.0.0/16"), ("C", "10.3.0.0/16")):
        resp = await client.post("/network/regions", headers=h, json={"name": name, "cidrs": [cidr]})
        ids[name] = resp.json()["id"]
    for name, ip in (
        ("a1", "10.1.0.1"), ("a2", "10.1.0.2"), ("b1", "10.2.0.1"), ("b2", "10.2.0.2"), ("c1", "10.3.0.1"),
    ):
        resp = await client.post("/network/hosts", headers=h, json={"name": name, "ip": ip})
        ids[name] = resp.json()["id"]
    for body in (
        {"src_host": "a1", "dst_host": "b1", "port_from": 8000, "port_to": 8010},
        {"src_host": "a2", "dst_host": "b1", "port_from": 8005, "port_to": 8020},
        {"src_host": "a1", "dst_host": "b1", "port_from": 9000},
        {"src_host": "a1", "dst_host": "b1", "port_from": 9100, "status": "closed"},
        {"src_cidr": "10.1.0.0/16", "dst_host": "c1", "port_from": 7000, "port_to": 7001},
    ):
        resp = await client.post("/network/openings", headers=h, json=body)
        assert resp.status_code == 201, resp.text
    return ids


def _spans(result):
    return [(s["port_from"], s["port_to"]) for s in result]


# ── Span arithmetic ─────────────────────────────────────────────────────────


def test_span_helpers():
    assert network_map.merge_spans([(5, 9), (1, 3), (4, 4), (20, 30), (25, 26)]) == [(1, 9), (20, 30)]
    assert network_map.intersect_spans([(1, 10), (20, 30)], [(5, 25)]) == [(5, 10), (20, 25)]
    assert network_map.intersect_spans([(1, 10)], []) == []
    assert network_map.subtract_ports([(1, 10)], [1, 5, 10, 99]) == [(2, 4), (6, 9)]
    assert network_map.subtract_ports([(1, 1)], [1]) == []
    assert network_map.span_count([(1, 10), (20, 20)]) == 11


# ── One destination ─────────────────────────────────────────────────────────


async def test_ports_every_source_reaches_minus_what_is_used(client, admin_token):
    h = auth_header(admin_token)
    await _setup(client, h)
    await client.post("/network/port-usages", headers=h, json={"host": "b1", "port": 8006, "service_name": "api"})

    resp = await client.get("/network/port-finder", headers=h, params={"src": ["a1", "a2"], "dst": "b1"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    [b1] = body["hosts"]
    assert b1["any_port"] is False
    # a1 has 8000-8010 and 9000, a2 has 8005-8020: both reach 8005-8010 only.
    assert _spans(b1["reachable"]) == [(8005, 8010)]
    assert _spans(b1["free"]) == [(8005, 8005), (8007, 8010)]
    assert b1["free_count"] == 5
    assert [(u["port"], u["kind"], u["service_name"]) for u in b1["used"]] == [(8006, "recorded", "api")]
    assert [s["port"] for s in b1["suggestions"]] == [8005, 8007, 8008, 8009, 8010]
    assert {s["evidence"] for s in b1["suggestions"]} == {"recorded"}
    assert [s["host_name"] for s in body["sources"]] == ["a1", "a2"]

    # One source alone also gets 9000; the closed 9100 never counts.
    [only_a1] = (await client.get("/network/port-finder", headers=h, params={"src": "a1", "dst": "b1"})).json()["hosts"]
    assert _spans(only_a1["reachable"]) == [(8000, 8010), (9000, 9000)]


async def test_same_region_sources_reach_any_port(client, admin_token):
    h = auth_header(admin_token)
    await _setup(client, h)
    await client.post("/network/port-usages", headers=h, json={"host": "b2", "port": 443, "service_name": "web"})
    [b2] = (await client.get("/network/port-finder", headers=h, params={"src": "b1", "dst": "b2"})).json()["hosts"]
    assert b2["any_port"] is True
    assert b2["reachable_count"] == 65535
    assert b2["free_count"] == 65534
    assert [u["port"] for u in b2["used"]] == [443]
    assert b2["suggestions"] == []  # any free port works: nothing to single out

    # A cross-region source added: only the openings remain.
    [mixed] = (await client.get(
        "/network/port-finder", headers=h, params={"src": ["b2", "a1"], "dst": "b1"},
    )).json()["hosts"]
    assert mixed["any_port"] is False
    assert _spans(mixed["reachable"]) == [(8000, 8010), (9000, 9000)]


async def test_probes_rank_measured_free_ports_and_drop_blocked_or_listening(client, admin_token, seeded_db):
    h = auth_header(admin_token)
    await _setup(client, h)
    agent = await _agent(seeded_db)
    resp = await client.post("/network/probes", headers=agent, json={"results": [
        {"src_host": "a1", "dst_host": "b1", "port": 8009, "result": "refused"},  # passes, nothing listens
        {"src_host": "a1", "dst_host": "b1", "port": 8001, "result": "open"},     # an unrecorded listener
        {"src_host": "a1", "dst_host": "b1", "port": 8002, "result": "timeout"},  # recorded open, blocked
    ]})
    assert resp.status_code == 200, resp.text
    [b1] = (await client.get("/network/port-finder", headers=h, params={"src": "a1", "dst": "b1"})).json()["hosts"]
    assert b1["suggestions"][0] == {"port": 8009, "evidence": "probed_free", "probed_from": ["a1"]}
    suggested = [s["port"] for s in b1["suggestions"]]
    assert 8001 not in suggested and 8002 not in suggested
    assert [(u["port"], u["kind"]) for u in b1["used"]] == [(8001, "listening")]
    assert [(b["port"], b["src_host_name"]) for b in b1["blocked"]] == [(8002, "a1")]
    assert b1["free_count"] == 12 - 2  # 8000-8010 + 9000, minus 8001 and 8002


async def test_a_timeout_never_hides_a_listener_and_predates_a_newer_opening(client, admin_token, seeded_db):
    """A timeout from an unrelated host says nothing about the service; one from
    before the opening was recorded says nothing about the firewall."""
    from datetime import datetime, timedelta, timezone

    h = auth_header(admin_token)
    await _setup(client, h)
    agent = await _agent(seeded_db)
    now = datetime.now(timezone.utc)
    hour_ago = (now - timedelta(hours=1)).isoformat()
    resp = await client.post("/network/probes", headers=agent, json={"results": [
        {"src_host": "b2", "dst_host": "b1", "port": 8001, "result": "open", "observed_at": hour_ago},
        {"src_host": "c1", "dst_host": "b1", "port": 8001, "result": "timeout"},
        # Before 9000 was (re)recorded: stale.
        {"src_host": "a1", "dst_host": "b1", "port": 9000, "result": "timeout",
         "observed_at": (now - timedelta(days=2)).isoformat()},
    ]})
    assert resp.status_code == 200, resp.text
    [b1] = (await client.get("/network/port-finder", headers=h, params={"src": "a1", "dst": "b1"})).json()["hosts"]
    assert [(u["port"], u["kind"]) for u in b1["used"]] == [(8001, "listening")]
    assert 8001 not in [s["port"] for s in b1["suggestions"]]
    assert b1["blocked"] == []
    assert b1["free_count"] == 12 - 1  # 9000 stays free; only the listener 8001 is taken

    # A later refused (service stopped) frees it again.
    await client.post("/network/probes", headers=agent, json={"results": [
        {"src_host": "a1", "dst_host": "b1", "port": 8001, "result": "refused"},
    ]})
    [b1] = (await client.get("/network/port-finder", headers=h, params={"src": "a1", "dst": "b1"})).json()["hosts"]
    assert b1["used"] == []
    assert b1["suggestions"][0] == {"port": 8001, "evidence": "probed_free", "probed_from": ["a1"]}


async def test_requested_port_status_and_nearest_suggestions(client, admin_token):
    h = auth_header(admin_token)
    await _setup(client, h)
    await client.post("/network/openings", headers=h, json={
        "src_host": "a2", "dst_host": "b2", "port_from": 30000, "port_to": 32767,
    })
    await client.post("/network/port-usages", headers=h, json={"host": "b2", "port": 31000, "service_name": "x"})

    async def find(port, dst="b2"):
        [r] = (await client.get(
            "/network/port-finder", headers=h, params={"src": "a2", "dst": dst, "port": port},
        )).json()["hosts"]
        return r

    used = await find(31000)
    assert used["requested_port"] == "used"
    # Suggestions sit next to the wanted port, not at the start of the range.
    assert all(abs(s["port"] - 31000) <= 32 for s in used["suggestions"])
    assert (await find(31001))["requested_port"] == "free"
    assert (await find(31001))["suggestions"][0]["port"] == 31001
    assert (await find(80))["requested_port"] == "unreachable"


async def test_without_destination_ranks_every_host(client, admin_token):
    h = auth_header(admin_token)
    ids = await _setup(client, h)
    body = (await client.get("/network/port-finder", headers=h, params={"src": "a1"})).json()
    names = [r["host"]["name"] for r in body["hosts"]]
    # a2 shares a1's region (any port) and comes first; b2 has nothing open: gone.
    assert names[0] == "a2" and set(names) == {"a2", "b1", "c1"}
    assert "a1" not in names  # the source itself is never a destination

    for_port = (await client.get("/network/port-finder", headers=h, params={"src": "a1", "port": 7001})).json()
    assert [r["host"]["name"] for r in for_port["hosts"]] == ["a2", "c1"]

    in_region = (await client.get(
        "/network/port-finder", headers=h, params={"src": "a1", "dst_region": ids["C"]},
    )).json()
    assert [r["host"]["name"] for r in in_region["hosts"]] == ["c1"]
    assert (await client.get(
        "/network/port-finder", headers=h, params={"src": "a1", "dst_region": 9999},
    )).status_code == 404

    # A host in no region is reported, not silently dropped.
    await client.post("/network/hosts", headers=h, json={"name": "loose", "ip": "192.168.9.9"})
    body = (await client.get("/network/port-finder", headers=h, params={"src": "a1"})).json()
    assert body["unplaced_destinations"] == ["loose"]


async def test_region_as_source_and_unplaced_sources(client, admin_token):
    h = auth_header(admin_token)
    ids = await _setup(client, h)
    body = (await client.get(
        "/network/port-finder", headers=h, params={"src_region": ids["A"], "dst": "c1"},
    )).json()
    assert [s["host_name"] for s in body["sources"]] == ["a1", "a2"]
    assert _spans(body["hosts"][0]["reachable"]) == [(7000, 7001)]  # the CIDR covers both

    # An unregistered IP in no region: only openings naming its range count.
    unplaced = (await client.get(
        "/network/port-finder", headers=h, params={"src": "192.168.0.9", "dst": "b1"},
    )).json()
    assert unplaced["unplaced_sources"] == ["192.168.0.9"]
    assert unplaced["hosts"][0]["reachable"] == []


async def test_finder_errors(client, admin_token):
    h = auth_header(admin_token)
    ids = await _setup(client, h)
    empty = (await client.post("/network/regions", headers=h, json={"name": "E", "cidrs": ["10.9.0.0/16"]})).json()
    assert (await client.get("/network/port-finder", headers=h)).status_code == 422
    assert (await client.get("/network/port-finder", headers=h, params={"src": "ghost"})).status_code == 404
    assert (await client.get("/network/port-finder", headers=h, params={"src_region": 9999})).status_code == 404
    assert (await client.get("/network/port-finder", headers=h, params={"src_region": empty["id"]})).status_code == 422
    assert (await client.get(
        "/network/port-finder", headers=h, params={"src": "a1", "dst": "10.2.0.99"},
    )).status_code == 404
    assert (await client.get(
        "/network/port-finder", headers=h, params={"src": "a1", "port": 0},
    )).status_code == 422
    assert ids  # setup ran


async def test_finder_for_agents_redacts_discovered_names(client, admin_token, seeded_db):
    h = auth_header(admin_token)
    await _setup(client, h)
    session = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    from app.models import DBConnection

    async with session() as db:
        db.add(DBConnection(
            alias="secret-orders", db_type="postgres", host="10.2.0.1", port=8003, database="o",
            username="u", password_encrypted="x", pool_size=1, max_overflow=0, query_timeout=30,
        ))
        await db.commit()
    reader = await _agent(seeded_db, "reader", routes=("network-api",))
    resp = await client.get("/network/port-finder", headers=reader, params={"src": "a1", "dst": "b1"})
    assert resp.status_code == 200, resp.text
    [used] = resp.json()["hosts"][0]["used"]
    assert used["port"] == 8003 and used["kind"] == "auto"
    assert "secret-orders" not in used["service_name"]
    no_grant = await _agent(seeded_db, "other", routes=("query-api",))
    assert (await client.get("/network/port-finder", headers=no_grant, params={"src": "a1"})).status_code == 403


# ── Host neighbourhood ──────────────────────────────────────────────────────


async def test_host_detail_lists_cidr_outbound_and_same_region_hosts(client, admin_token):
    h = auth_header(admin_token)
    ids = await _setup(client, h)
    a2 = (await client.get(f"/network/hosts/{ids['a2']}", headers=h)).json()
    assert [o["dst_host_name"] for o in a2["outbound"]] == ["b1"]
    assert [o["dst_host_name"] for o in a2["outbound_via_cidr"]] == ["c1"]
    assert [x["name"] for x in a2["same_region_hosts"]] == ["a1"]

    c1 = (await client.get(f"/network/hosts/{ids['c1']}", headers=h)).json()
    assert c1["outbound_via_cidr"] == []  # its own inbound CIDR opening is not outbound
    assert [o["src_cidr"] for o in c1["inbound"]] == ["10.1.0.0/16"]
