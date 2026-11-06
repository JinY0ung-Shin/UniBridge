"""Network map phase 2: probes, CSV import and one record per opening."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import ApiKeyAccess
from app.services import network_import, network_map
from tests.conftest import auth_header


@pytest.fixture(autouse=True)
def _no_gateway(monkeypatch):
    monkeypatch.setattr(
        network_map.apisix_client, "list_resources", AsyncMock(return_value={"items": []})
    )
    network_map.clear_gateway_cache()
    yield
    network_map.clear_gateway_cache()


async def _agent(seeded_db, name="prober") -> dict[str, str]:
    session = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session() as db:
        db.add(ApiKeyAccess(consumer_name=name, allowed_routes=json.dumps(["network-api", "network-write-api"])))
        await db.commit()
    return {"X-Consumer-Username": name}


async def _setup(client, h) -> dict[str, int]:
    ids: dict[str, int] = {}
    for name, cidr in (("A", "10.1.0.0/16"), ("B", "10.2.0.0/16")):
        resp = await client.post("/network/regions", headers=h, json={"name": name, "cidrs": [cidr]})
        ids[name] = resp.json()["id"]
    for name, ip in (("a1", "10.1.0.1"), ("a2", "10.1.0.2"), ("b1", "10.2.0.1"), ("b2", "10.2.0.2")):
        resp = await client.post("/network/hosts", headers=h, json={"name": name, "ip": ip})
        ids[name] = resp.json()["id"]
    return ids


def _probe(src, dst, port, result, **extra):
    return {"src_host": src, "dst_host": dst, "port": port, "result": result, **extra}


# ── Probes ──────────────────────────────────────────────────────────────────


async def test_probe_mismatches_against_the_records(client, admin_token, seeded_db):
    h = auth_header(admin_token)
    await _setup(client, h)
    agent = await _agent(seeded_db)
    await client.post("/network/openings", headers=h, json={"src_host": "a1", "dst_host": "b1", "port_from": 5432})

    resp = await client.post("/network/probes", headers=agent, json={"results": [
        _probe("a1", "b1", 5432, "timeout", error="timed out"),  # recorded open, blocked
        _probe("a1", "b2", 22, "open", latency_ms=3.5),           # no opening, passes
        _probe("a1", "a2", 80, "refused"),                         # same region: fine
        _probe("a1", "b1", 443, "timeout"),                        # blocked as recorded
        _probe("ghost", "b1", 22, "open"),                         # unknown host
    ]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [e["index"] for e in body["errors"]] == [4]
    by_port = {p["port"]: p for p in body["stored"]}
    assert by_port[5432]["mismatch"] == "recorded_open_but_blocked"
    assert by_port[22]["mismatch"] == "passes_without_opening"
    assert by_port[80]["mismatch"] is None and by_port[80]["expected_reason"] == "same_region"
    assert by_port[443]["mismatch"] is None and by_port[443]["expected"] == "blocked"
    assert by_port[22]["reported_by"] == "apikey:prober"

    mismatches = (await client.get("/network/probes", headers=h, params={"mismatch": True})).json()
    assert sorted(p["port"] for p in mismatches) == [22, 5432]
    assert (await client.get("/network/overview", headers=h)).json()["probe_mismatches"] == 2

    opening = (await client.get("/network/openings", headers=h)).json()[0]
    assert opening["last_probe"]["result"] == "timeout"
    assert opening["last_probe"]["mismatch"] == "recorded_open_but_blocked"

    check = (await client.get("/network/check", headers=h, params={"src": "a1", "dst": "b1", "port": 5432})).json()
    assert check["probe"]["result"] == "timeout"


async def test_newer_probe_replaces_older_and_older_never_overwrites(client, admin_token, seeded_db):
    h = auth_header(admin_token)
    await _setup(client, h)
    agent = await _agent(seeded_db)
    now = datetime.now(timezone.utc)
    await client.post("/network/probes", headers=agent, json={"results": [
        _probe("a1", "b1", 22, "timeout", observed_at=(now - timedelta(minutes=5)).isoformat()),
    ]})
    await client.post("/network/probes", headers=agent, json={"results": [
        _probe("a1", "b1", 22, "open", observed_at=now.isoformat()),
    ]})
    stale = await client.post("/network/probes", headers=agent, json={"results": [
        _probe("a1", "b1", 22, "error", observed_at=(now - timedelta(hours=1)).isoformat()),
    ]})
    assert [p["result"] for p in stale.json()["stored"]] == ["open"]
    probes = (await client.get("/network/probes", headers=h)).json()
    assert [(p["port"], p["result"]) for p in probes] == [(22, "open")]

    future = await client.post("/network/probes", headers=agent, json={"results": [
        _probe("a1", "b1", 23, "open", observed_at=(now + timedelta(hours=1)).isoformat()),
    ]})
    assert future.json()["errors"][0]["message"] == "observed_at is in the future"


async def test_probe_reports_need_the_write_grant(client, admin_token, seeded_db):
    h = auth_header(admin_token)
    await _setup(client, h)
    session = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session() as db:
        db.add(ApiKeyAccess(consumer_name="reader", allowed_routes=json.dumps(["network-api"])))
        await db.commit()
    resp = await client.post("/network/probes", headers={"X-Consumer-Username": "reader"},
                             json={"results": [_probe("a1", "b1", 22, "open")]})
    assert resp.status_code == 403
    bad = await client.post("/network/probes", headers=h, json={"results": [_probe("a1", "b1", 22, "maybe")]})
    assert bad.status_code == 422


async def test_probe_targets_cover_host_and_cidr_openings(client, admin_token):
    h = auth_header(admin_token)
    await _setup(client, h)
    await client.post("/network/openings", headers=h, json={"src_host": "a1", "dst_host": "b1", "port_from": 8000, "port_to": 8002})
    await client.post("/network/openings", headers=h, json={"src_cidr": "10.1.0.0/24", "dst_host": "b2", "port_from": 22, "status": "closed"})
    await client.post("/network/openings", headers=h, json={"src_host": "a1", "dst_host": "b2", "port_from": 1000, "port_to": 2000})
    await client.post("/network/port-usages", headers=h, json={"host": "b2", "port": 1500, "service_name": "svc"})
    await client.post("/network/openings", headers=h, json={"src_host": "a2", "dst_host": "b1", "port_from": 9000})

    body = (await client.get("/network/probe-targets", headers=h, params={"src": "10.1.0.1"})).json()
    assert body["src_host_name"] == "a1"
    got = {(t["dst_host_name"], t["port"]): t["expected"] for t in body["targets"]}
    assert got == {
        ("b1", 8000): "open", ("b1", 8001): "open", ("b1", 8002): "open",  # small range: every port
        ("b2", 22): "blocked",                                              # closed, via the CIDR
        ("b2", 1000): "open", ("b2", 1500): "open",                         # wide range: first + used
    }
    assert (await client.get("/network/probe-targets", headers=h, params={"src": "nobody"})).status_code == 404


async def test_deleting_a_host_drops_its_probes(client, admin_token, seeded_db):
    h = auth_header(admin_token)
    ids = await _setup(client, h)
    agent = await _agent(seeded_db)
    await client.post("/network/probes", headers=agent, json={"results": [_probe("a1", "b1", 22, "open")]})
    assert (await client.delete(f"/network/hosts/{ids['b1']}", headers=h)).status_code == 204
    assert (await client.get("/network/probes", headers=h)).json() == []


# ── One record per opening ──────────────────────────────────────────────────


async def test_same_opening_cannot_be_recorded_twice_even_past_the_app_check(client, admin_token, monkeypatch):
    h = auth_header(admin_token)
    await _setup(client, h)
    body = {"src_cidr": "10.9.0.0/16", "dst_host": "b1", "port_from": 22}
    first = (await client.post("/network/openings", headers=h, json=body)).json()
    # Simulate the race: the app-level check sees nothing, the database still refuses.
    from app.routers import network as network_router
    monkeypatch.setattr(network_router, "_validate_opening", lambda *a, **k: None)
    dup = await client.post("/network/openings", headers=h, json=body)
    assert dup.status_code == 409
    assert dup.json()["detail"]["id"] == first["id"]


# ── CSV import ──────────────────────────────────────────────────────────────


async def _import(client, h, kind, text, dry_run=True):
    resp = await client.post("/network/import", headers=h, json={"kind": kind, "csv": text, "dry_run": dry_run})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_import_hosts_with_korean_headers_dry_run_then_apply(client, admin_token):
    h = auth_header(admin_token)
    await _setup(client, h)
    text = "﻿서버명,IP 주소,리전,담당자\nc1,10.2.5.5,,인프라팀\na1,10.1.9.9,,\nbad name,10.2.5.6,,\nc2,10.9.9.9,Nowhere,\nc3,10.1.7.7,a,\n"
    preview = await _import(client, h, "hosts", text)
    assert [(r["line"], r["action"]) for r in preview["rows"]] == [
        (2, "create"), (3, "skip"), (4, "error"), (5, "error"), (6, "create"),
    ]
    assert (preview["created"], preview["skipped"], preview["errors"]) == (2, 1, 2)
    assert "B" in preview["rows"][0]["message"]  # placed by CIDR
    assert (await client.get("/network/hosts", headers=h)).json().__len__() == 4  # dry run wrote nothing

    applied = await _import(client, h, "hosts", text, dry_run=False)
    assert applied["created"] == 2
    hosts = {x["name"]: x for x in (await client.get("/network/hosts", headers=h)).json()}
    assert hosts["c1"]["source"] == "import" and hosts["c1"]["verified"] is True
    assert hosts["c1"]["owner"] == "인프라팀" and hosts["c3"]["region_name"] == "A"


async def test_import_openings_from_a_company_style_sheet(client, admin_token):
    h = auth_header(admin_token)
    await _setup(client, h)
    text = (
        "출발지,목적지,포트,프로토콜,상태,신청번호,용도\n"
        "a1,b1,\"5432, 8000~8002\",TCP,열림,FW-1,db\n"   # two openings
        "10.1.3.3,10.2.0.2,22,,해제,FW-2,ssh\n"           # unregistered IP -> /32, closed
        "a1,a2,80,,,FW-3,\n"                              # same region -> skip
        "a1,ghost,80,,,FW-4,\n"                           # unknown destination -> error
        "a1,b1,99999,,,FW-5,\n"                           # bad port -> error
        "a1,b1,5432,,,FW-6,\n"                            # duplicate of line 2 -> skip
    )
    applied = await _import(client, h, "openings", text, dry_run=False)
    actions = [(r["line"], r["action"]) for r in applied["rows"]]
    assert actions == [(2, "create"), (2, "create"), (3, "create"), (4, "skip"), (5, "error"), (6, "error"), (7, "skip")]
    openings = (await client.get("/network/openings", headers=h)).json()
    assert {(o["src_host_name"] or o["src_cidr"], o["port_from"], o["port_to"], o["status"]) for o in openings} == {
        ("a1", 5432, 5432, "open"), ("a1", 8000, 8002, "open"), ("10.1.3.3/32", 22, 22, "closed"),
    }
    assert all(o["source"] == "import" and o["verified"] for o in openings)


async def test_import_port_usages_and_format_errors(client, admin_token, seeded_db):
    h = auth_header(admin_token)
    await _setup(client, h)
    result = await _import(client, h, "port_usages", "host\tport\tservice\nb1\t5432\tpostgres\nb1\t5432\tpostgres\nzz\t1\tx\n", dry_run=False)
    assert [r["action"] for r in result["rows"]] == ["create", "skip", "error"]
    missing = await client.post("/network/import", headers=h, json={"kind": "openings", "csv": "foo,bar\n1,2\n", "dry_run": True})
    assert missing.status_code == 422 and "src" in missing.json()["detail"]
    agent = await _agent(seeded_db)
    assert (await client.post("/network/import", headers=agent, json={"kind": "hosts", "csv": "name,ip\n"})).status_code == 401


@pytest.mark.parametrize(
    ("value", "expected"),
    [("22", [(22, 22)]), ("8000-8010", [(8000, 8010)]), ("80, 443 8080~8081", [(80, 80), (443, 443), (8080, 8081)])],
)
def test_parse_ports(value, expected):
    assert network_import.parse_ports(value) == expected


@pytest.mark.parametrize("value", ["", "0", "70000", "10-5", "http"])
def test_parse_ports_rejects(value):
    with pytest.raises(ValueError):
        network_import.parse_ports(value)


# ── Review follow-ups ───────────────────────────────────────────────────────


async def test_editing_an_opening_into_a_duplicate_is_409_even_past_the_app_check(client, admin_token, monkeypatch):
    h = auth_header(admin_token)
    await _setup(client, h)
    first = (await client.post("/network/openings", headers=h, json={"src_host": "a1", "dst_host": "b1", "port_from": 22})).json()
    second = (await client.post("/network/openings", headers=h, json={"src_host": "a2", "dst_host": "b1", "port_from": 22})).json()
    from app.routers import network as network_router
    monkeypatch.setattr(network_router, "_validate_opening", lambda *a, **k: None)
    resp = await client.put(f"/network/openings/{second['id']}", headers=h, json={"src_host": "a1"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["id"] == first["id"]


async def test_probe_can_be_deleted_and_a_batch_reports_a_path_once(client, admin_token, seeded_db):
    h = auth_header(admin_token)
    await _setup(client, h)
    agent = await _agent(seeded_db)
    body = (await client.post("/network/probes", headers=agent, json={"results": [
        _probe("a1", "b1", 22, "timeout"), _probe("a1", "b1", 22, "open"),
    ]})).json()
    assert len(body["stored"]) == 1 and body["stored"][0]["result"] == "open"
    probe_id = body["stored"][0]["id"]
    assert (await client.delete(f"/network/probes/{probe_id}", headers=agent)).status_code == 204
    assert (await client.get("/network/probes", headers=h)).json() == []
    assert (await client.delete(f"/network/probes/{probe_id}", headers=h)).status_code == 404


def test_csv_quote_character_is_never_guessed():
    text = "name,ip,owner,description\nh1,10.1.0.1,'ops',x\nh2,10.1.0.2,ops,'s3 bucket owner\nh3,10.1.0.3,ops,y\nh4,10.1.0.4,ops,z\n"
    rows = network_import.parse_rows("hosts", text)
    assert [r["name"] for _, r in rows] == ["h1", "h2", "h3", "h4"]
    assert rows[1][1]["description"] == "'s3 bucket owner"


def test_one_ports_cell_cannot_explode_into_thousands_of_openings():
    with pytest.raises(ValueError, match="at most"):
        network_import.parse_ports(",".join(str(p) for p in range(1, 200)))
