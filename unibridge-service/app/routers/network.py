"""Network map: regions, hosts, firewall openings and port usage.

People reach this through the UI with a Keycloak JWT (``network.read`` /
``network.write``); agents through APISIX with an API key holding the
``network-api`` (reads) or ``network-write-api`` (writes) route, directly or via
the MCP tools. Whatever an API key writes is recorded as ``source='agent'`` and
stays unverified until a person confirms it (``POST .../verify``, people only).
The rules themselves live in :mod:`app.services.network_map`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    ApiKeyUser,
    CurrentUser,
    get_current_user_or_apikey,
    get_role_permissions,
    require_permission,
)
from app.database import get_db
from app.db_types import utcnow
from app.models import (
    FirewallOpening,
    MonitoredHost,
    NetworkHost,
    NetworkProbe,
    PortUsage,
    Region,
)
from app.schemas import (
    AccessCheckResponse,
    FirewallOpeningCreate,
    FirewallOpeningResponse,
    FirewallOpeningUpdate,
    MonitoredImportResult,
    NetworkHostCreate,
    NetworkHostDetail,
    NetworkHostResponse,
    NetworkHostUpdate,
    NetworkOverview,
    NetworkProtocol,
    NetworkSettings,
    PortFinderResponse,
    PortUsageCreate,
    PortUsageResponse,
    PortUsageUpdate,
    ProbeBatch,
    ProbeBatchError,
    ProbeBatchResponse,
    ProbeResponse,
    ProbeTargetsResponse,
    NetworkImportRequest,
    NetworkImportResponse,
    RegionCreate,
    RegionResponse,
    RegionUpdate,
)
from app.services import network_import, network_map
from app.services.apisix_system_resources import NETWORK_API_ROUTE_ID, NETWORK_WRITE_ROUTE_ID
from app.services.audit import log_admin_action
from app.services.network_map import NetworkMapError, Snapshot

router = APIRouter(prefix="/network", tags=["Network"])


# ── Auth ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Actor:
    name: str
    is_agent: bool
    # Discovered usages name UniBridge's own config (DB aliases, upstreams, …).
    # People and master keys see them; other keys see only the kind.
    sees_config: bool = True


def _key_has_route(user: ApiKeyUser, route_id: str) -> bool:
    return "*" in user.allowed_routes or route_id in user.allowed_routes


async def _actor(
    user: CurrentUser | ApiKeyUser, db: AsyncSession, *, route_id: str, perms: tuple[str, ...]
) -> Actor:
    if isinstance(user, ApiKeyUser):
        if not _key_has_route(user, route_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Required API key route: {route_id}",
            )
        return Actor(
            name=f"apikey:{user.consumer_name}",
            is_agent=True,
            sees_config="*" in user.allowed_routes,
        )
    user_perms = await get_role_permissions(db, user.role)
    if not any(p in user_perms for p in perms):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Required permission: {' or '.join(perms)}",
        )
    return Actor(name=user.username, is_agent=False)


async def network_reader(
    user: CurrentUser | ApiKeyUser = Depends(get_current_user_or_apikey),
    db: AsyncSession = Depends(get_db),
) -> Actor:
    return await _actor(
        user, db, route_id=NETWORK_API_ROUTE_ID, perms=("network.read", "network.write")
    )


async def network_writer(
    user: CurrentUser | ApiKeyUser = Depends(get_current_user_or_apikey),
    db: AsyncSession = Depends(get_db),
) -> Actor:
    return await _actor(user, db, route_id=NETWORK_WRITE_ROUTE_ID, perms=("network.write",))


async def network_person_writer(
    user: CurrentUser = Depends(require_permission("network.write")),
) -> Actor:
    """People only: confirming agent records and platform settings."""
    return Actor(name=user.username, is_agent=False)


def _stamp(row: Any, actor: Actor, *, created: bool) -> None:
    """Record who wrote ``row``: an agent's write is unverified until a person confirms."""
    if created:
        row.created_by = actor.name
        row.source = "agent" if actor.is_agent else "manual"
    if actor.is_agent:
        row.verified_at = None
        row.verified_by = None
    else:
        row.verified_at = utcnow()
        row.verified_by = actor.name


def _shown(actor: Actor, usages: list[PortUsageResponse]) -> list[PortUsageResponse]:
    return usages if actor.sees_config else network_map.redact_discovered(usages)


def _agent_may_delete(actor: Actor, *rows: Any) -> None:
    """A person-confirmed record is deleted by a person: an agent's delete would
    skip the review every other agent write goes through."""
    if actor.is_agent and any(row.verified_at is not None for row in rows):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "A person confirmed this record (or one that goes with it), so only a "
                "person can delete it; an opening can be marked closed instead"
            ),
        )


def _raise(exc: NetworkMapError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail)


async def _snapshot(db: AsyncSession) -> Snapshot:
    return await network_map.load_snapshot(db)


async def _commit(db: AsyncSession, conflict: str) -> None:
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail=conflict)


# ── Overview, settings, access check ────────────────────────────────────────


@router.get("/overview", response_model=NetworkOverview)
async def get_overview(
    _actor: Actor = Depends(network_reader), db: AsyncSession = Depends(get_db)
) -> NetworkOverview:
    return await network_map.overview(db, await _snapshot(db))


@router.get("/settings", response_model=NetworkSettings)
async def get_settings(
    _actor: Actor = Depends(network_reader), db: AsyncSession = Depends(get_db)
) -> NetworkSettings:
    return await network_map.load_settings(db)


@router.put("/settings", response_model=NetworkSettings)
async def put_settings(
    body: NetworkSettings,
    actor: Actor = Depends(network_person_writer),
    db: AsyncSession = Depends(get_db),
) -> NetworkSettings:
    if body.platform_host_id is not None and await db.get(NetworkHost, body.platform_host_id) is None:
        raise HTTPException(status_code=422, detail="platform_host_id is not a registered host")
    before = await network_map.load_settings(db)
    await network_map.save_settings(db, body)
    await log_admin_action(
        db, actor=actor.name, action="update", resource_type="network_settings",
        resource_id="network", before=before.model_dump(), after=body.model_dump(),
    )
    return body


@router.get("/check", response_model=AccessCheckResponse)
async def check_access(
    src: str = Query(..., min_length=1, max_length=100, description="Source host name or IP"),
    dst: str = Query(..., min_length=1, max_length=100, description="Destination host name or IP"),
    port: int = Query(..., ge=1, le=65535),
    protocol: NetworkProtocol = Query("tcp"),
    actor: Actor = Depends(network_reader),
    db: AsyncSession = Depends(get_db),
) -> AccessCheckResponse:
    snap = await _snapshot(db)
    try:
        return await network_map.check_access(
            db, snap, src, dst, port, protocol, redact=not actor.sees_config
        )
    except NetworkMapError as exc:
        _raise(exc)
        raise  # unreachable


@router.get("/port-finder", response_model=PortFinderResponse)
async def find_ports(
    src: list[str] = Query(
        default_factory=list,
        max_length=50,
        description="Source host names or IPs that must reach the service (repeatable)",
    ),
    src_region: list[int] = Query(
        default_factory=list, max_length=20, description="Every host of these regions is a source"
    ),
    dst: str | None = Query(
        None, max_length=100, description="Destination host; omitted = every host, best first"
    ),
    dst_region: int | None = Query(None, description="Only destination hosts in this region (ignored with dst)"),
    port: int | None = Query(None, ge=1, le=65535, description="The port you would like"),
    protocol: NetworkProtocol = Query("tcp"),
    actor: Actor = Depends(network_reader),
    db: AsyncSession = Depends(get_db),
) -> PortFinderResponse:
    """Ports every source already reaches on a destination and nothing uses yet:
    where a new service can listen without a firewall request."""
    if any(len(ref) > 100 or not ref.strip() for ref in src):
        raise HTTPException(status_code=422, detail="Each src is a host name or IP (1-100 chars)")
    snap = await _snapshot(db)
    try:
        return await network_map.find_ports(
            db,
            snap,
            src_refs=[ref.strip() for ref in src],
            src_region_ids=src_region,
            dst_ref=dst.strip() if dst and dst.strip() else None,
            dst_region_id=dst_region,
            protocol=protocol,
            port=port,
            redact=not actor.sees_config,
        )
    except NetworkMapError as exc:
        _raise(exc)
        raise  # unreachable


# ── Regions ─────────────────────────────────────────────────────────────────


def _region_snapshot(region: Region) -> dict[str, Any]:
    return {
        "name": region.name,
        "description": region.description,
        "cidrs": network_map.region_cidrs(region),
    }


@router.get("/regions", response_model=list[RegionResponse])
async def list_regions(
    _actor: Actor = Depends(network_reader), db: AsyncSession = Depends(get_db)
) -> list[RegionResponse]:
    snap = await _snapshot(db)
    return [
        network_map.region_response(snap, r)
        for r in sorted(snap.regions.values(), key=lambda r: r.name)
    ]


@router.post("/regions", response_model=RegionResponse, status_code=201)
async def create_region(
    body: RegionCreate,
    actor: Actor = Depends(network_person_writer),
    db: AsyncSession = Depends(get_db),
) -> RegionResponse:
    region = Region(name=body.name, description=body.description, cidrs=json.dumps(body.cidrs))
    db.add(region)
    await _commit(db, f"Region '{body.name}' already exists")
    await db.refresh(region)
    await log_admin_action(
        db, actor=actor.name, action="create", resource_type="network_region",
        resource_id=region.name, before=None, after=_region_snapshot(region),
    )
    return network_map.region_response(await _snapshot(db), region)


async def _get_region(db: AsyncSession, region_id: int) -> Region:
    region = await db.get(Region, region_id)
    if region is None:
        raise HTTPException(status_code=404, detail="Region not found")
    return region


@router.put("/regions/{region_id}", response_model=RegionResponse)
async def update_region(
    region_id: int,
    body: RegionUpdate,
    actor: Actor = Depends(network_person_writer),
    db: AsyncSession = Depends(get_db),
) -> RegionResponse:
    region = await _get_region(db, region_id)
    before = _region_snapshot(region)
    if body.name is not None:
        region.name = body.name
    if body.description is not None:
        region.description = body.description
    if body.cidrs is not None:
        region.cidrs = json.dumps(body.cidrs)
    await _commit(db, f"Region '{body.name}' already exists")
    await db.refresh(region)
    await log_admin_action(
        db, actor=actor.name, action="update", resource_type="network_region",
        resource_id=region.name, before=before, after=_region_snapshot(region),
    )
    return network_map.region_response(await _snapshot(db), region)


@router.delete("/regions/{region_id}", status_code=204, response_model=None)
async def delete_region(
    region_id: int,
    actor: Actor = Depends(network_person_writer),
    db: AsyncSession = Depends(get_db),
) -> None:
    region = await _get_region(db, region_id)
    in_use = (
        await db.execute(select(NetworkHost.id).where(NetworkHost.region_id == region_id).limit(1))
    ).first()
    if in_use is not None:
        raise HTTPException(
            status_code=409, detail="Region still has hosts: move or delete them first"
        )
    before = _region_snapshot(region)
    await db.delete(region)
    await db.commit()
    await log_admin_action(
        db, actor=actor.name, action="delete", resource_type="network_region",
        resource_id=before["name"], before=before, after=None,
    )


# ── Hosts ───────────────────────────────────────────────────────────────────


def _host_snapshot(host: NetworkHost) -> dict[str, Any]:
    return {
        "name": host.name,
        "ip": host.ip,
        "region_id": host.region_id,
        "description": host.description,
        "owner": host.owner,
        "monitored_host_id": host.monitored_host_id,
        "verified": host.verified_at is not None,
    }


async def _region_id_for(
    db: AsyncSession, snap: Snapshot, *, region_id: int | None, region: str | None
) -> int | None:
    if region_id is not None:
        if region_id not in snap.regions:
            raise HTTPException(status_code=422, detail=f"Region id {region_id} does not exist")
        return region_id
    if region is not None:
        wanted = region.strip().lower()
        for candidate in snap.regions.values():
            if candidate.name.lower() == wanted:
                return candidate.id
        raise HTTPException(status_code=422, detail=f"Region {region!r} does not exist")
    return None


def _check_name_free(snap: Snapshot, name: str, *, host_id: int | None = None) -> None:
    """Names are looked up case-insensitively, so they must be unique that way."""
    lowered = name.lower()
    if any(h.name.lower() == lowered and h.id != host_id for h in snap.hosts.values()):
        raise HTTPException(status_code=409, detail=f"A host named {name!r} already exists")


async def _check_monitored(db: AsyncSession, monitored_host_id: int | None) -> None:
    if monitored_host_id is not None and await db.get(MonitoredHost, monitored_host_id) is None:
        raise HTTPException(status_code=422, detail="monitored_host_id is not a monitored server")


@router.get("/hosts", response_model=list[NetworkHostResponse])
async def list_hosts(
    region_id: int | None = Query(None),
    q: str | None = Query(None, max_length=100, description="Name, IP or description contains"),
    unverified: bool = Query(False),
    _actor: Actor = Depends(network_reader),
    db: AsyncSession = Depends(get_db),
) -> list[NetworkHostResponse]:
    snap = await _snapshot(db)
    needle = (q or "").strip().lower()
    out = []
    for host in sorted(snap.hosts.values(), key=lambda h: h.name):
        if region_id is not None and host.region_id != region_id:
            continue
        if unverified and host.verified_at is not None:
            continue
        if needle and not any(
            needle in (value or "").lower() for value in (host.name, host.ip, host.description)
        ):
            continue
        out.append(network_map.host_response(snap, host))
    return out


@router.get("/hosts/{host_id}", response_model=NetworkHostDetail)
async def get_host(
    host_id: int,
    actor: Actor = Depends(network_reader),
    db: AsyncSession = Depends(get_db),
) -> NetworkHostDetail:
    snap = await _snapshot(db)
    host = snap.hosts.get(host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="Host not found")
    usages, _failed = await network_map.all_usages(db, snap)
    base = network_map.host_response(snap, host)
    return NetworkHostDetail(
        **base.model_dump(),
        inbound=[
            network_map.opening_response(snap, o, usages)
            for o in snap.openings
            if o.dst_host_id == host_id
        ],
        outbound=[
            network_map.opening_response(snap, o, usages)
            for o in snap.openings
            if o.src_host_id == host_id
        ],
        outbound_via_cidr=[
            network_map.opening_response(snap, o, usages)
            for o in snap.openings
            if o.src_host_id is None
            and o.dst_host_id != host_id
            and network_map.source_covers_host(o, host)
        ],
        usages=sorted(
            _shown(actor, [u for u in usages if u.host_id == host_id]),
            key=lambda u: (u.port, u.service_name),
        ),
        same_region_hosts=[
            network_map.host_response(snap, h)
            for h in sorted(snap.hosts.values(), key=lambda h: h.name)
            if host.region_id is not None and h.region_id == host.region_id and h.id != host_id
        ],
    )


@router.post("/hosts", response_model=NetworkHostResponse, status_code=201)
async def create_host(
    body: NetworkHostCreate,
    actor: Actor = Depends(network_writer),
    db: AsyncSession = Depends(get_db),
) -> NetworkHostResponse:
    snap = await _snapshot(db)
    _check_name_free(snap, body.name)
    region_id = await _region_id_for(db, snap, region_id=body.region_id, region=body.region)
    if region_id is None:
        region_id = snap.region_for_ip(body.ip)
    await _check_monitored(db, body.monitored_host_id)
    host = NetworkHost(
        name=body.name,
        ip=body.ip,
        region_id=region_id,
        description=body.description,
        owner=body.owner,
        monitored_host_id=body.monitored_host_id,
    )
    _stamp(host, actor, created=True)
    db.add(host)
    await _commit(db, "A host with this name, IP or monitored server already exists")
    await db.refresh(host)
    await log_admin_action(
        db, actor=actor.name, action="create", resource_type="network_host",
        resource_id=host.name, summary=host.ip, before=None, after=_host_snapshot(host),
    )
    return network_map.host_response(await _snapshot(db), host)


async def _get_host(db: AsyncSession, host_id: int) -> NetworkHost:
    host = await db.get(NetworkHost, host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="Host not found")
    return host


@router.put("/hosts/{host_id}", response_model=NetworkHostResponse)
async def update_host(
    host_id: int,
    body: NetworkHostUpdate,
    actor: Actor = Depends(network_writer),
    db: AsyncSession = Depends(get_db),
) -> NetworkHostResponse:
    host = await _get_host(db, host_id)
    before = _host_snapshot(host)
    snap = await _snapshot(db)
    fields = body.model_fields_set
    # Validate everything before touching the row: a lookup after a change would
    # autoflush it and turn a duplicate name/IP into a 500 instead of a 409.
    if body.name is not None:
        _check_name_free(snap, body.name, host_id=host_id)
    if "monitored_host_id" in fields:
        await _check_monitored(db, body.monitored_host_id)
    if body.region is not None or body.region_id is not None:
        new_region = await _region_id_for(db, snap, region_id=body.region_id, region=body.region)
    elif "region_id" in fields:
        new_region = None  # explicit null: unassign
    else:
        new_region = host.region_id
    host.region_id = new_region
    for name in ("name", "ip", "description", "owner"):
        value = getattr(body, name)
        if value is not None:
            setattr(host, name, value)
    if "monitored_host_id" in fields:
        host.monitored_host_id = body.monitored_host_id
    _stamp(host, actor, created=False)
    await _commit(db, "A host with this name, IP or monitored server already exists")
    await db.refresh(host)
    await log_admin_action(
        db, actor=actor.name, action="update", resource_type="network_host",
        resource_id=host.name, summary=host.ip, before=before, after=_host_snapshot(host),
    )
    return network_map.host_response(await _snapshot(db), host)


@router.delete("/hosts/{host_id}", status_code=204, response_model=None)
async def delete_host(
    host_id: int,
    actor: Actor = Depends(network_writer),
    db: AsyncSession = Depends(get_db),
) -> None:
    host = await _get_host(db, host_id)
    if actor.is_agent:
        snap = await _snapshot(db)
        _agent_may_delete(
            actor,
            host,
            *(o for o in snap.openings if host_id in (o.src_host_id, o.dst_host_id)),
            *(u for u in snap.usages if u.host_id == host_id),
        )
    before = _host_snapshot(host)
    # Openings, usages and probes go with it. ON DELETE CASCADE covers this too, but
    # only where the database enforces foreign keys, so delete them explicitly.
    await db.execute(
        sa_delete(FirewallOpening).where(
            (FirewallOpening.src_host_id == host_id) | (FirewallOpening.dst_host_id == host_id)
        )
    )
    await db.execute(sa_delete(PortUsage).where(PortUsage.host_id == host_id))
    await db.execute(
        sa_delete(NetworkProbe).where(
            (NetworkProbe.src_host_id == host_id) | (NetworkProbe.dst_host_id == host_id)
        )
    )
    await db.delete(host)
    await db.commit()
    await log_admin_action(
        db, actor=actor.name, action="delete", resource_type="network_host",
        resource_id=before["name"], summary=before["ip"], before=before, after=None,
    )


@router.post("/hosts/{host_id}/verify", response_model=NetworkHostResponse)
async def verify_host(
    host_id: int,
    actor: Actor = Depends(network_person_writer),
    db: AsyncSession = Depends(get_db),
) -> NetworkHostResponse:
    host = await _get_host(db, host_id)
    _stamp(host, actor, created=False)
    await db.commit()
    await db.refresh(host)
    await log_admin_action(
        db, actor=actor.name, action="verify", resource_type="network_host",
        resource_id=host.name, summary=host.ip,
    )
    return network_map.host_response(await _snapshot(db), host)


@router.post("/hosts/import-monitored", response_model=MonitoredImportResult)
async def import_monitored_hosts(
    actor: Actor = Depends(network_person_writer),
    db: AsyncSession = Depends(get_db),
) -> MonitoredImportResult:
    """Register (or link by IP) every monitored server not yet on the map."""
    snap = await _snapshot(db)
    linked_ids = {h.monitored_host_id for h in snap.hosts.values() if h.monitored_host_id}
    taken_names = {h.name.lower() for h in snap.hosts.values()}
    result = MonitoredImportResult()
    monitored = (await db.execute(select(MonitoredHost).order_by(MonitoredHost.name))).scalars()
    for mon in monitored.all():
        if mon.id in linked_ids:
            continue
        parts = network_map.split_host_port(mon.address)
        ip = network_map.parse_ip(parts[0]) if parts else None
        if ip is None:
            result.skipped.append({"name": mon.name, "reason": "address is not an IP address"})
            continue
        existing = snap.find_host(str(ip))
        if existing is not None:
            if existing.monitored_host_id is None:
                existing.monitored_host_id = mon.id
                result.linked.append(f"{mon.name} → {existing.name}")
            else:
                result.skipped.append(
                    {"name": mon.name, "reason": f"IP already used by host {existing.name}"}
                )
            continue
        if mon.name.lower() in taken_names:
            result.skipped.append({"name": mon.name, "reason": "a host with this name exists"})
            continue
        host = NetworkHost(
            name=mon.name,
            ip=str(ip),
            region_id=snap.region_for_ip(str(ip)),
            description=mon.description or "",
            monitored_host_id=mon.id,
        )
        _stamp(host, actor, created=True)
        host.source = "import"
        db.add(host)
        taken_names.add(mon.name.lower())
        result.created.append(mon.name)
    await _commit(db, "Import conflicted with a concurrent change; try again")
    if result.created or result.linked:
        await log_admin_action(
            db, actor=actor.name, action="import", resource_type="network_host",
            resource_id="monitored_servers",
            summary=f"created {len(result.created)}, linked {len(result.linked)}",
            after=result.model_dump(),
        )
    return result


# ── Firewall openings ───────────────────────────────────────────────────────


def _opening_snapshot(opening: FirewallOpening) -> dict[str, Any]:
    return {
        "src_host_id": opening.src_host_id,
        "src_cidr": opening.src_cidr,
        "dst_host_id": opening.dst_host_id,
        "ports": network_map.port_label(opening.protocol, opening.port_from, opening.port_to),
        "status": opening.status,
        "ticket_no": opening.ticket_no,
        "requester": opening.requester,
        "purpose": opening.purpose,
        "verified": opening.verified_at is not None,
    }


def _resolve_host(snap: Snapshot, *, host_id: int | None, ref: str | None, what: str) -> NetworkHost:
    host = snap.hosts.get(host_id) if host_id is not None else snap.find_host(ref or "")
    if host is None:
        shown = host_id if host_id is not None else ref
        raise HTTPException(status_code=422, detail=f"The {what} host {shown!r} is not registered")
    return host


def set_src_key(opening: FirewallOpening) -> None:
    """The one non-null source value the opening's unique constraint keys on."""
    opening.src_key = (
        f"host:{opening.src_host_id}" if opening.src_host_id is not None else f"cidr:{opening.src_cidr}"
    )


async def _commit_opening(db: AsyncSession, opening: FirewallOpening) -> None:
    """Commit; a concurrent identical record (the unique constraint) answers 409."""
    # Read the identity first: a rollback expires the row, and reading an
    # expired attribute would lazy-load, which async sessions cannot.
    identity = (
        opening.src_key, opening.dst_host_id, opening.protocol, opening.port_from, opening.port_to
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        src_key, dst_host_id, protocol, port_from, port_to = identity
        existing = (
            await db.execute(
                select(FirewallOpening.id).where(
                    FirewallOpening.src_key == src_key,
                    FirewallOpening.dst_host_id == dst_host_id,
                    FirewallOpening.protocol == protocol,
                    FirewallOpening.port_from == port_from,
                    FirewallOpening.port_to == port_to,
                )
            )
        ).scalar_one_or_none()
        raise HTTPException(
            status_code=409,
            detail={"message": "This opening is already recorded", "id": existing},
        )


def _validate_opening(
    snap: Snapshot, opening: FirewallOpening, *, endpoints_changed: bool = True
) -> None:
    """``endpoints_changed=False`` (an edit that keeps src/dst) skips the region
    rule: a host or region moved since may have made the pair one region, and
    the record must still be editable (e.g. to mark it closed)."""
    dst = snap.hosts[opening.dst_host_id]
    if opening.src_host_id is not None:
        if opening.src_host_id == opening.dst_host_id:
            raise HTTPException(status_code=422, detail="Source and destination are the same host")
        src_region = snap.hosts[opening.src_host_id].region_id
    else:
        src_region = snap.region_for_cidr(opening.src_cidr)
    if endpoints_changed and src_region is not None and src_region == dst.region_id:
        raise HTTPException(
            status_code=422,
            detail=(
                "Source and destination are in the same region, where every port is "
                "already open: no firewall opening is needed"
            ),
        )
    if opening.port_from > opening.port_to:
        raise HTTPException(status_code=422, detail="port_to must be >= port_from")
    for other in snap.openings:
        if other.id == opening.id:
            continue
        if (
            other.src_host_id == opening.src_host_id
            and other.src_cidr == opening.src_cidr
            and other.dst_host_id == opening.dst_host_id
            and other.protocol == opening.protocol
            and other.port_from == opening.port_from
            and other.port_to == opening.port_to
        ):
            raise HTTPException(
                status_code=409,
                detail={"message": "This opening is already recorded", "id": other.id},
            )


async def _opening_response(db: AsyncSession, opening: FirewallOpening) -> FirewallOpeningResponse:
    snap = await _snapshot(db)
    # A write's answer does not wait on the gateway; in_use then counts only
    # recorded and locally discovered usages.
    usages, _failed = await network_map.all_usages(db, snap, include_gateway=False)
    return network_map.opening_response(snap, opening, usages)


@router.get("/openings", response_model=list[FirewallOpeningResponse])
async def list_openings(
    host_id: int | None = Query(None, description="Source or destination host"),
    region_id: int | None = Query(None, description="Source or destination region"),
    status_filter: str | None = Query(None, alias="status", pattern="^(open|closed)$"),
    port: int | None = Query(None, ge=1, le=65535),
    unverified: bool = Query(False),
    _actor: Actor = Depends(network_reader),
    db: AsyncSession = Depends(get_db),
) -> list[FirewallOpeningResponse]:
    snap = await _snapshot(db)
    usages, _failed = await network_map.all_usages(db, snap)
    out = []
    for opening in snap.openings:
        resp = network_map.opening_response(snap, opening, usages)
        if host_id is not None and host_id not in (resp.src_host_id, resp.dst_host_id):
            continue
        if region_id is not None and region_id not in (resp.src_region_id, resp.dst_region_id):
            continue
        if status_filter is not None and resp.status != status_filter:
            continue
        if port is not None and not (resp.port_from <= port <= resp.port_to):
            continue
        if unverified and resp.verified:
            continue
        out.append(resp)
    return out


@router.post("/openings", response_model=FirewallOpeningResponse, status_code=201)
async def create_opening(
    body: FirewallOpeningCreate,
    actor: Actor = Depends(network_writer),
    db: AsyncSession = Depends(get_db),
) -> FirewallOpeningResponse:
    snap = await _snapshot(db)
    src = (
        None
        if body.src_cidr is not None
        else _resolve_host(snap, host_id=body.src_host_id, ref=body.src_host, what="source")
    )
    dst = _resolve_host(snap, host_id=body.dst_host_id, ref=body.dst_host, what="destination")
    opening = FirewallOpening(
        src_host_id=src.id if src is not None else None,
        src_cidr=body.src_cidr,
        dst_host_id=dst.id,
        port_from=body.port_from,
        port_to=body.port_to if body.port_to is not None else body.port_from,
        protocol=body.protocol,
        status=body.status,
        ticket_no=body.ticket_no,
        requester=body.requester,
        purpose=body.purpose,
    )
    _validate_opening(snap, opening)
    _stamp(opening, actor, created=True)
    set_src_key(opening)
    db.add(opening)
    await _commit_opening(db, opening)
    await db.refresh(opening)
    await log_admin_action(
        db, actor=actor.name, action="create", resource_type="firewall_opening",
        resource_id=str(opening.id), summary=f"{dst.name} {_opening_snapshot(opening)['ports']}",
        before=None, after=_opening_snapshot(opening),
    )
    return await _opening_response(db, opening)


async def _get_opening(db: AsyncSession, opening_id: int) -> FirewallOpening:
    opening = await db.get(FirewallOpening, opening_id)
    if opening is None:
        raise HTTPException(status_code=404, detail="Firewall opening not found")
    return opening


@router.put("/openings/{opening_id}", response_model=FirewallOpeningResponse)
async def update_opening(
    opening_id: int,
    body: FirewallOpeningUpdate,
    actor: Actor = Depends(network_writer),
    db: AsyncSession = Depends(get_db),
) -> FirewallOpeningResponse:
    opening = await _get_opening(db, opening_id)
    before = _opening_snapshot(opening)
    snap = await _snapshot(db)
    current = next(o for o in snap.openings if o.id == opening_id)
    if body.src_cidr is not None:
        current.src_host_id, current.src_cidr = None, body.src_cidr
    elif body.src_host_id is not None or body.src_host is not None:
        src = _resolve_host(snap, host_id=body.src_host_id, ref=body.src_host, what="source")
        current.src_host_id, current.src_cidr = src.id, None
    if body.dst_host_id is not None or body.dst_host is not None:
        current.dst_host_id = _resolve_host(
            snap, host_id=body.dst_host_id, ref=body.dst_host, what="destination"
        ).id
    for name in ("port_from", "port_to", "protocol", "status", "ticket_no", "requester", "purpose"):
        value = getattr(body, name)
        if value is not None:
            setattr(current, name, value)
    if body.port_from is not None and body.port_to is None and current.port_to < body.port_from:
        current.port_to = body.port_from
    endpoints_changed = any(
        getattr(body, f) is not None
        for f in ("src_host_id", "src_host", "src_cidr", "dst_host_id", "dst_host")
    )
    _validate_opening(snap, current, endpoints_changed=endpoints_changed)
    _stamp(current, actor, created=False)
    set_src_key(current)
    await _commit_opening(db, current)
    await db.refresh(current)
    await log_admin_action(
        db, actor=actor.name, action="update", resource_type="firewall_opening",
        resource_id=str(current.id), before=before, after=_opening_snapshot(current),
    )
    return await _opening_response(db, current)


@router.delete("/openings/{opening_id}", status_code=204, response_model=None)
async def delete_opening(
    opening_id: int,
    actor: Actor = Depends(network_writer),
    db: AsyncSession = Depends(get_db),
) -> None:
    opening = await _get_opening(db, opening_id)
    _agent_may_delete(actor, opening)
    before = _opening_snapshot(opening)
    await db.delete(opening)
    await db.commit()
    await log_admin_action(
        db, actor=actor.name, action="delete", resource_type="firewall_opening",
        resource_id=str(opening_id), before=before, after=None,
    )


@router.post("/openings/{opening_id}/verify", response_model=FirewallOpeningResponse)
async def verify_opening(
    opening_id: int,
    actor: Actor = Depends(network_person_writer),
    db: AsyncSession = Depends(get_db),
) -> FirewallOpeningResponse:
    opening = await _get_opening(db, opening_id)
    _stamp(opening, actor, created=False)
    await db.commit()
    await db.refresh(opening)
    await log_admin_action(
        db, actor=actor.name, action="verify", resource_type="firewall_opening",
        resource_id=str(opening_id),
    )
    return await _opening_response(db, opening)


# ── Port usage ──────────────────────────────────────────────────────────────


def _usage_snapshot(usage: PortUsage) -> dict[str, Any]:
    return {
        "host_id": usage.host_id,
        "port": usage.port,
        "protocol": usage.protocol,
        "service_name": usage.service_name,
        "description": usage.description,
        "owner": usage.owner,
        "verified": usage.verified_at is not None,
    }


_USAGE_CONFLICT = "This service is already recorded on that host port"


@router.get("/port-usages", response_model=list[PortUsageResponse])
async def list_port_usages(
    host_id: int | None = Query(None),
    port: int | None = Query(None, ge=1, le=65535),
    source: str | None = Query(None, pattern="^(manual|agent|auto)$"),
    unverified: bool = Query(False),
    actor: Actor = Depends(network_reader),
    db: AsyncSession = Depends(get_db),
) -> list[PortUsageResponse]:
    snap = await _snapshot(db)
    usages, _failed = await network_map.all_usages(db, snap)
    usages = _shown(actor, usages)
    return [
        u
        for u in sorted(usages, key=lambda u: (u.host_name, u.port, u.service_name))
        if (host_id is None or u.host_id == host_id)
        and (port is None or u.port == port)
        and (source is None or u.source == source)
        and (not unverified or not u.verified)
    ]


@router.post("/port-usages", response_model=PortUsageResponse, status_code=201)
async def create_port_usage(
    body: PortUsageCreate,
    actor: Actor = Depends(network_writer),
    db: AsyncSession = Depends(get_db),
) -> PortUsageResponse:
    snap = await _snapshot(db)
    host = _resolve_host(snap, host_id=body.host_id, ref=body.host, what="port usage's")
    usage = PortUsage(
        host_id=host.id,
        port=body.port,
        protocol=body.protocol,
        service_name=body.service_name,
        description=body.description,
        owner=body.owner,
    )
    _stamp(usage, actor, created=True)
    db.add(usage)
    await _commit(db, _USAGE_CONFLICT)
    await db.refresh(usage)
    await log_admin_action(
        db, actor=actor.name, action="create", resource_type="port_usage",
        resource_id=str(usage.id), summary=f"{host.name}:{usage.port} {usage.service_name}",
        before=None, after=_usage_snapshot(usage),
    )
    return network_map.stored_usage_response(await _snapshot(db), usage)


async def _get_usage(db: AsyncSession, usage_id: int) -> PortUsage:
    usage = await db.get(PortUsage, usage_id)
    if usage is None:
        raise HTTPException(status_code=404, detail="Port usage not found")
    return usage


@router.put("/port-usages/{usage_id}", response_model=PortUsageResponse)
async def update_port_usage(
    usage_id: int,
    body: PortUsageUpdate,
    actor: Actor = Depends(network_writer),
    db: AsyncSession = Depends(get_db),
) -> PortUsageResponse:
    usage = await _get_usage(db, usage_id)
    before = _usage_snapshot(usage)
    for name in ("port", "protocol", "service_name", "description", "owner"):
        value = getattr(body, name)
        if value is not None:
            setattr(usage, name, value)
    _stamp(usage, actor, created=False)
    await _commit(db, _USAGE_CONFLICT)
    await db.refresh(usage)
    await log_admin_action(
        db, actor=actor.name, action="update", resource_type="port_usage",
        resource_id=str(usage.id), before=before, after=_usage_snapshot(usage),
    )
    return network_map.stored_usage_response(await _snapshot(db), usage)


@router.delete("/port-usages/{usage_id}", status_code=204, response_model=None)
async def delete_port_usage(
    usage_id: int,
    actor: Actor = Depends(network_writer),
    db: AsyncSession = Depends(get_db),
) -> None:
    usage = await _get_usage(db, usage_id)
    _agent_may_delete(actor, usage)
    before = _usage_snapshot(usage)
    await db.delete(usage)
    await db.commit()
    await log_admin_action(
        db, actor=actor.name, action="delete", resource_type="port_usage",
        resource_id=str(usage_id), before=before, after=None,
    )


@router.post("/port-usages/{usage_id}/verify", response_model=PortUsageResponse)
async def verify_port_usage(
    usage_id: int,
    actor: Actor = Depends(network_person_writer),
    db: AsyncSession = Depends(get_db),
) -> PortUsageResponse:
    usage = await _get_usage(db, usage_id)
    _stamp(usage, actor, created=False)
    await db.commit()
    await db.refresh(usage)
    await log_admin_action(
        db, actor=actor.name, action="verify", resource_type="port_usage",
        resource_id=str(usage_id),
    )
    return network_map.stored_usage_response(await _snapshot(db), usage)


# ── Probes ──────────────────────────────────────────────────────────────────

# A report may not claim to be from the future (clock skew allowance).
_PROBE_FUTURE_SLACK_SECONDS = 300


@router.post("/probes", response_model=ProbeBatchResponse)
async def report_probes(
    body: ProbeBatch,
    actor: Actor = Depends(network_writer),
    db: AsyncSession = Depends(get_db),
) -> ProbeBatchResponse:
    """Store measured connection attempts; each replaces the previous one on its path.

    Items fail on their own (unknown host, a future timestamp) and come back
    under ``errors`` by index; the rest are stored.
    """
    snap = await _snapshot(db)
    now = utcnow()
    existing = {(p.src_host_id, p.dst_host_id, p.port, p.protocol): p for p in snap.probes}
    errors: list[ProbeBatchError] = []
    touched: list[NetworkProbe] = []
    for index, item in enumerate(body.results):
        try:
            src = _resolve_host(snap, host_id=item.src_host_id, ref=item.src_host, what="source")
            dst = _resolve_host(snap, host_id=item.dst_host_id, ref=item.dst_host, what="destination")
        except HTTPException as exc:
            errors.append(ProbeBatchError(index=index, message=str(exc.detail)))
            continue
        if src.id == dst.id:
            errors.append(ProbeBatchError(index=index, message="Source and destination are the same host"))
            continue
        observed = item.observed_at or now
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        if observed > now + timedelta(seconds=_PROBE_FUTURE_SLACK_SECONDS):
            errors.append(ProbeBatchError(index=index, message="observed_at is in the future"))
            continue
        key = (src.id, dst.id, item.port, item.protocol)
        probe = existing.get(key)
        if probe is not None and probe.observed_at > observed:
            touched.append(probe)  # an older measurement never replaces a newer one
            continue
        if probe is None:
            probe = NetworkProbe(
                src_host_id=src.id, dst_host_id=dst.id, port=item.port, protocol=item.protocol
            )
            db.add(probe)
            existing[key] = probe
        probe.result = item.result
        probe.latency_ms = item.latency_ms
        probe.error = item.error
        probe.reported_by = actor.name
        probe.observed_at = observed
        touched.append(probe)
    await _commit(db, "A concurrent report stored the same path; send it again")
    snap = await _snapshot(db)
    by_id = {p.id: p for p in snap.probes}
    stored = []
    for probe_id in dict.fromkeys(p.id for p in touched):  # a path once, in order
        current = by_id.get(probe_id)
        if current is not None:
            stored.append(network_map.probe_response(snap, current))
    return ProbeBatchResponse(stored=stored, errors=errors)


@router.delete("/probes/{probe_id}", status_code=204, response_model=None)
async def delete_probe(
    probe_id: int,
    actor: Actor = Depends(network_writer),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Drop a measurement (e.g. one taken from the wrong host or with a wrong clock)."""
    probe = await db.get(NetworkProbe, probe_id)
    if probe is None:
        raise HTTPException(status_code=404, detail="Probe not found")
    before = {
        "src_host_id": probe.src_host_id, "dst_host_id": probe.dst_host_id,
        "port": probe.port, "protocol": probe.protocol, "result": probe.result,
    }
    await db.delete(probe)
    await db.commit()
    await log_admin_action(
        db, actor=actor.name, action="delete", resource_type="network_probe",
        resource_id=str(probe_id), before=before, after=None,
    )


@router.get("/probes", response_model=list[ProbeResponse])
async def list_probes(
    host_id: int | None = Query(None, description="Source or destination host"),
    mismatch: bool = Query(False, description="Only probes that contradict the records"),
    result: str | None = Query(None, pattern="^(open|refused|timeout|error)$"),
    _actor: Actor = Depends(network_reader),
    db: AsyncSession = Depends(get_db),
) -> list[ProbeResponse]:
    snap = await _snapshot(db)
    out = []
    for probe in sorted(snap.probes, key=lambda p: p.observed_at, reverse=True):
        if host_id is not None and host_id not in (probe.src_host_id, probe.dst_host_id):
            continue
        if result is not None and probe.result != result:
            continue
        resp = network_map.probe_response(snap, probe)
        if mismatch and resp.mismatch is None:
            continue
        out.append(resp)
    return out


@router.get("/probe-targets", response_model=ProbeTargetsResponse)
async def get_probe_targets(
    src: str = Query(..., min_length=1, max_length=100, description="This host's name or IP"),
    _actor: Actor = Depends(network_reader),
    db: AsyncSession = Depends(get_db),
) -> ProbeTargetsResponse:
    """What a host should test: every recorded opening whose source covers it."""
    snap = await _snapshot(db)
    host = snap.find_host(src)
    if host is None:
        raise HTTPException(status_code=404, detail=f"No registered host {src!r}")
    usages, _failed = await network_map.all_usages(db, snap, include_gateway=False)
    return ProbeTargetsResponse(
        src_host_id=host.id,
        src_host_name=host.name,
        src_ip=host.ip,
        targets=network_map.probe_targets(snap, host, usages),
    )


# ── CSV import ──────────────────────────────────────────────────────────────


@router.post("/import", response_model=NetworkImportResponse)
async def import_csv(
    body: NetworkImportRequest,
    actor: Actor = Depends(network_person_writer),
    db: AsyncSession = Depends(get_db),
) -> NetworkImportResponse:
    """Bulk-add hosts, openings or port usage from CSV (people only).

    ``dry_run`` (the default) answers what each line would do without writing.
    Lines already recorded are skipped, invalid ones reported; the valid rest
    are added in one transaction, recorded as source ``import`` and verified.
    """
    result = await network_import.run_import(db, body, actor_name=actor.name)
    if not body.dry_run and result.created:
        await log_admin_action(
            db, actor=actor.name, action="import", resource_type=f"network_{body.kind}",
            resource_id="csv", summary=f"created {result.created}, skipped {result.skipped}, "
            f"errors {result.errors}",
        )
    return result
