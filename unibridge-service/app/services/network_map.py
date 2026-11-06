"""Network map: regions, hosts, firewall openings and what listens on each port.

Hosts in one region reach each other on every port. A connection across
regions needs a firewall opening, requested through the company system by
whoever needs it, so nobody has the whole picture. This module answers
"can A reach B:port?" from the recorded openings (:func:`check_access`), lists
what listens on a host's ports — recorded by people or agents, plus what
UniBridge itself knows connects there (:func:`discover_usages`) — and builds the
region-to-region overview.

Everything works on a :class:`Snapshot` of the (small) tables loaded at once,
so one request answers from one consistent view without per-row queries.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DBConnection,
    FirewallOpening,
    MonitoredHost,
    MonitoredService,
    NetworkHost,
    NetworkProbe,
    PortUsage,
    Region,
    S3Connection,
    SystemConfig,
)
from app.schemas import (
    AccessCheckResponse,
    AccessEndpoint,
    FinderBlockedPort,
    FinderUsedPort,
    FirewallOpeningResponse,
    NetworkHostResponse,
    NetworkOverview,
    NetworkSettings,
    PortFinderHost,
    PortFinderResponse,
    PortSpan,
    PortSuggestion,
    PortUsageResponse,
    ProbeResponse,
    ProbeSummary,
    ProbeTarget,
    RegionLink,
    RegionResponse,
)
from app.services import apisix_client

logger = logging.getLogger(__name__)

SETTINGS_KEY = "network_settings"
EXTERNAL = "external"
# Discovery reads APISIX's upstream list; a slow gateway must not stall the page.
GATEWAY_DISCOVERY_TIMEOUT_SECONDS = 5.0

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


class NetworkMapError(Exception):
    """A request the map cannot answer: unknown reference etc. (→ HTTP 4xx)."""

    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


# ── Parsing helpers ─────────────────────────────────────────────────────────


def region_cidrs(region: Region) -> list[str]:
    try:
        value = json.loads(region.cidrs) if region.cidrs else []
    except (TypeError, ValueError):
        return []
    return [str(v) for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _network(value: str) -> IPNetwork | None:
    try:
        return ipaddress.ip_network(value, strict=False)
    except ValueError:
        return None


def parse_ip(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not value:
        return None
    try:
        return ipaddress.ip_address(value.strip().strip("[]"))
    except ValueError:
        return None


def split_host_port(address: str | None) -> tuple[str, int | None] | None:
    """``host:port`` / ``[v6]:port`` / ``host`` → (host, port or None)."""
    if not address:
        return None
    address = address.strip()
    if address.startswith("["):
        host, _, rest = address[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    elif address.count(":") == 1:
        host, _, port = address.partition(":")
    else:
        host, port = address, ""
    if not host:
        return None
    return host, int(port) if port.isdigit() else None


def port_label(protocol: str, port_from: int, port_to: int) -> str:
    return f"{protocol}/{port_from}" if port_from == port_to else f"{protocol}/{port_from}-{port_to}"


def is_external(host: NetworkHost | None) -> bool:
    return host is not None and host.kind == EXTERNAL


def external_host_name(ip: str) -> str:
    """``dst-<ip>``, the name people already gave such placeholders (an IPv6
    address with its colons as dashes, which a host name cannot hold)."""
    return f"dst-{ip.replace(':', '-')}"


def new_external_host(snap: "Snapshot", ref: str) -> NetworkHost:
    """An unsaved external host for a destination IP no host has (``dst-<ip>``).

    Raises :class:`NetworkMapError` when ``ref`` is not an IP address or the
    name is taken by a host with another IP.
    """
    addr = parse_ip(ref)
    if addr is None:
        raise NetworkMapError(
            422,
            f"The destination host {ref!r} is not registered; only an IP address can be "
            "added as an external host",
        )
    ip = str(addr)
    name = external_host_name(ip)
    if any(h.name.lower() == name.lower() for h in snap.hosts.values()):
        raise NetworkMapError(
            409,
            f"A host named {name!r} already exists with another IP: register the "
            "destination yourself",
        )
    return NetworkHost(
        name=name, ip=ip, kind=EXTERNAL, region_id=None,
        description="Added with a firewall opening", owner="",
    )


# ── Snapshot ────────────────────────────────────────────────────────────────


@dataclass
class Snapshot:
    regions: dict[int, Region]
    hosts: dict[int, NetworkHost]
    openings: list[FirewallOpening]
    usages: list[PortUsage]
    monitored_names: dict[int, str]
    settings: NetworkSettings
    probes: list[NetworkProbe] = field(default_factory=list)
    _region_nets: list[tuple[IPNetwork, int]] = field(default_factory=list)
    # Lookups the per-request loops use, so they do not scan every row per row.
    openings_by_dst: dict[int, list[FirewallOpening]] = field(default_factory=dict)
    probes_by_dst: dict[tuple[int, str], list[NetworkProbe]] = field(default_factory=dict)
    _mismatch: dict[int, str | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for opening in self.openings:
            self.openings_by_dst.setdefault(opening.dst_host_id, []).append(opening)
        for probe in self.probes:
            self.probes_by_dst.setdefault((probe.dst_host_id, probe.protocol), []).append(probe)
        nets: list[tuple[IPNetwork, int]] = []
        for region in self.regions.values():
            for cidr in region_cidrs(region):
                net = _network(cidr)
                if net is not None:
                    nets.append((net, region.id))
        # Most specific first, so a /24 inside a /16 of another region wins.
        nets.sort(key=lambda item: item[0].prefixlen, reverse=True)
        self._region_nets = nets

    def region_for_ip(self, ip: str | None) -> int | None:
        addr = parse_ip(ip)
        if addr is None:
            return None
        for net, region_id in self._region_nets:
            if addr.version == net.version and addr in net:
                return region_id
        return None

    def region_for_cidr(self, cidr: str | None) -> int | None:
        target = _network(cidr) if cidr else None
        if target is None:
            return None
        for net, region_id in self._region_nets:
            if target.version == net.version and target.subnet_of(net):  # type: ignore[arg-type]
                return region_id
        return None

    def find_host(self, ref: str) -> NetworkHost | None:
        """A registered host by IP (when ``ref`` is one) or name (case-insensitive)."""
        ref = ref.strip()
        addr = parse_ip(ref)
        if addr is not None:
            for host in self.hosts.values():
                if parse_ip(host.ip) == addr:
                    return host
            return None
        lowered = ref.lower()
        for host in self.hosts.values():
            if host.name.lower() == lowered:
                return host
        return None

    def region_name(self, region_id: int | None) -> str | None:
        region = self.regions.get(region_id) if region_id is not None else None
        return region.name if region is not None else None

    def opening_src_region(self, opening: FirewallOpening) -> int | None:
        if opening.src_host_id is not None:
            host = self.hosts.get(opening.src_host_id)
            return host.region_id if host is not None else None
        return self.region_for_cidr(opening.src_cidr)

    @property
    def platform_host(self) -> NetworkHost | None:
        host_id = self.settings.platform_host_id
        return self.hosts.get(host_id) if host_id is not None else None


async def load_settings(db: AsyncSession) -> NetworkSettings:
    row = await db.get(SystemConfig, SETTINGS_KEY)
    if row is None:
        return NetworkSettings()
    try:
        return NetworkSettings.model_validate_json(row.value)
    except ValueError:
        logger.warning("Ignoring unreadable %s in system_config", SETTINGS_KEY)
        return NetworkSettings()


async def save_settings(db: AsyncSession, value: NetworkSettings) -> None:
    row = await db.get(SystemConfig, SETTINGS_KEY)
    payload = value.model_dump_json()
    if row is None:
        db.add(SystemConfig(key=SETTINGS_KEY, value=payload))
    else:
        row.value = payload
    await db.commit()


async def load_snapshot(db: AsyncSession) -> Snapshot:
    regions = {r.id: r for r in (await db.execute(select(Region))).scalars().all()}
    hosts = {h.id: h for h in (await db.execute(select(NetworkHost))).scalars().all()}
    openings = list(
        (await db.execute(select(FirewallOpening).order_by(FirewallOpening.id))).scalars().all()
    )
    usages = list((await db.execute(select(PortUsage).order_by(PortUsage.id))).scalars().all())
    probes = list(
        (await db.execute(select(NetworkProbe).order_by(NetworkProbe.id))).scalars().all()
    )
    monitored = (await db.execute(select(MonitoredHost.id, MonitoredHost.name))).all()
    settings = await load_settings(db)
    # A deleted host drops out of the settings (each field on its own).
    if settings.platform_host_id is not None and settings.platform_host_id not in hosts:
        settings = settings.model_copy(update={"platform_host_id": None})
    if settings.main_host_id is not None and settings.main_host_id not in hosts:
        settings = settings.model_copy(update={"main_host_id": None})
    return Snapshot(
        regions=regions,
        hosts=hosts,
        openings=openings,
        usages=usages,
        monitored_names={row[0]: row[1] for row in monitored},
        settings=settings,
        probes=probes,
    )


# ── Response builders ───────────────────────────────────────────────────────


def region_response(snap: Snapshot, region: Region) -> RegionResponse:
    return RegionResponse(
        id=region.id,
        name=region.name,
        description=region.description or "",
        cidrs=region_cidrs(region),
        host_count=sum(1 for h in snap.hosts.values() if h.region_id == region.id),
        created_at=region.created_at,
        updated_at=region.updated_at,
    )


def host_response(snap: Snapshot, host: NetworkHost) -> NetworkHostResponse:
    return NetworkHostResponse(
        id=host.id,
        name=host.name,
        ip=host.ip,
        region_id=host.region_id,
        region_name=snap.region_name(host.region_id),
        description=host.description or "",
        owner=host.owner or "",
        kind=host.kind or "managed",
        # A link whose monitored server is gone (where the database does not
        # enforce ON DELETE SET NULL) reads as no link.
        monitored_host_id=host.monitored_host_id
        if host.monitored_host_id in snap.monitored_names
        else None,
        monitored_host_name=snap.monitored_names.get(host.monitored_host_id)
        if host.monitored_host_id is not None
        else None,
        source=host.source,
        created_by=host.created_by,
        verified=host.verified_at is not None,
        verified_at=host.verified_at,
        verified_by=host.verified_by,
        created_at=host.created_at,
        updated_at=host.updated_at,
    )


def _usage_in_range(
    usage_port: int, usage_protocol: str, opening: FirewallOpening
) -> bool:
    return usage_protocol == opening.protocol and opening.port_from <= usage_port <= opening.port_to


def opening_response(
    snap: Snapshot,
    opening: FirewallOpening,
    usages: Iterable[PortUsageResponse] = (),
) -> FirewallOpeningResponse:
    src = snap.hosts.get(opening.src_host_id) if opening.src_host_id is not None else None
    dst = snap.hosts[opening.dst_host_id]
    src_region = snap.opening_src_region(opening)
    in_use = any(
        u.host_id == opening.dst_host_id and _usage_in_range(u.port, u.protocol, opening)
        for u in usages
    )
    last = opening_last_probe(snap, opening)
    return FirewallOpeningResponse(
        id=opening.id,
        src_host_id=opening.src_host_id,
        src_host_name=src.name if src is not None else None,
        src_host_ip=src.ip if src is not None else None,
        src_cidr=opening.src_cidr,
        src_region_id=src_region,
        src_region_name=snap.region_name(src_region),
        dst_host_id=dst.id,
        dst_host_name=dst.name,
        dst_host_ip=dst.ip,
        dst_host_kind=dst.kind or "managed",
        dst_region_id=dst.region_id,
        dst_region_name=snap.region_name(dst.region_id),
        port_from=opening.port_from,
        port_to=opening.port_to,
        protocol=opening.protocol,
        status=opening.status,
        ticket_no=opening.ticket_no or "",
        requester=opening.requester or "",
        purpose=opening.purpose or "",
        in_use=in_use,
        last_probe=probe_summary(snap, last) if last is not None else None,
        source=opening.source,
        created_by=opening.created_by,
        verified=opening.verified_at is not None,
        verified_at=opening.verified_at,
        verified_by=opening.verified_by,
        created_at=opening.created_at,
        updated_at=opening.updated_at,
    )


def stored_usage_response(snap: Snapshot, usage: PortUsage) -> PortUsageResponse:
    host = snap.hosts[usage.host_id]
    return PortUsageResponse(
        id=usage.id,
        host_id=host.id,
        host_name=host.name,
        host_ip=host.ip,
        port=usage.port,
        protocol=usage.protocol,
        service_name=usage.service_name,
        description=usage.description or "",
        owner=usage.owner or "",
        source=usage.source,
        created_by=usage.created_by,
        verified=usage.verified_at is not None,
        verified_at=usage.verified_at,
        verified_by=usage.verified_by,
        created_at=usage.created_at,
        updated_at=usage.updated_at,
    )


# ── Discovery: what UniBridge itself connects to ────────────────────────────


@dataclass(frozen=True)
class _Found:
    host: str
    port: int
    service_name: str
    auto_kind: str
    description: str = ""


def _match_host(snap: Snapshot, name_or_ip: str) -> NetworkHost | None:
    return snap.find_host(name_or_ip)


_GATEWAY_CACHE_SECONDS = 30.0
_gateway_cache: tuple[float, dict[str, Any]] | None = None


async def _gateway_upstream_list() -> dict[str, Any]:
    global _gateway_cache
    now = time.monotonic()
    if _gateway_cache is not None and now - _gateway_cache[0] < _GATEWAY_CACHE_SECONDS:
        return _gateway_cache[1]
    data = await asyncio.wait_for(
        apisix_client.list_resources("upstreams"), timeout=GATEWAY_DISCOVERY_TIMEOUT_SECONDS
    )
    _gateway_cache = (now, data)
    return data


def clear_gateway_cache() -> None:
    global _gateway_cache
    _gateway_cache = None


async def _gateway_upstreams() -> list[_Found]:
    data = await _gateway_upstream_list()
    found: list[_Found] = []
    for item in data.get("items", []):
        if not isinstance(item, dict):
            continue
        label = str(item.get("name") or item.get("id") or "")
        for address in apisix_client.upstream_node_addresses(item.get("nodes")):
            parts = split_host_port(address)
            if parts is None or parts[1] is None:
                continue
            found.append(
                _Found(parts[0], parts[1], f"gateway upstream '{label}'", "gateway_upstream")
            )
    return found


async def discover_usages(
    db: AsyncSession, snap: Snapshot, *, include_gateway: bool = True
) -> tuple[list[PortUsageResponse], list[str]]:
    """Usages UniBridge knows from its own config, on registered hosts only.

    Sources: database connections, S3 endpoints, node_exporter/dcgm-exporter
    scrape targets, external-service metrics endpoints and APISIX upstream
    nodes. Each is connected to from the platform host, so with one set its
    ``reachable_from_platform`` is filled in. Returns the usages and the
    sources that could not be read (only the gateway can fail).
    """
    found: list[_Found] = []
    failed: list[str] = []

    for conn in (await db.execute(select(DBConnection))).scalars().all():
        if conn.host and conn.port:
            found.append(
                _Found(conn.host, int(conn.port), f"{conn.db_type} DB '{conn.alias}'", "db",
                       f"database {conn.database}")
            )
    for conn in (await db.execute(select(S3Connection))).scalars().all():
        if not conn.endpoint_url:
            continue
        parsed = urlparse(conn.endpoint_url)
        if parsed.hostname:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            found.append(_Found(parsed.hostname, port, f"S3 '{conn.alias}'", "s3"))
    for mon in (await db.execute(select(MonitoredHost))).scalars().all():
        for address, kind, label in (
            (mon.address, "node_exporter", "node_exporter"),
            (mon.gpu_address, "dcgm", "dcgm-exporter"),
        ):
            parts = split_host_port(address)
            if parts is not None and parts[1] is not None:
                found.append(_Found(parts[0], parts[1], f"{label} ('{mon.name}')", kind,
                                    "scraped by Prometheus"))
    for svc in (await db.execute(select(MonitoredService))).scalars().all():
        parts = split_host_port(svc.address)
        if parts is not None and parts[1] is not None:
            found.append(_Found(parts[0], parts[1], f"metrics '{svc.name}'", "metrics",
                                "scraped by Prometheus"))
    if include_gateway:
        try:
            found.extend(await _gateway_upstreams())
        except Exception as exc:  # noqa: BLE001 — discovery is best effort
            logger.warning("Network map could not read APISIX upstreams: %s", exc)
            failed.append("gateway_upstream")

    platform = snap.platform_host
    seen: set[tuple[int, int, str]] = set()
    out: list[PortUsageResponse] = []
    for item in found:
        host = _match_host(snap, item.host)
        if host is None:
            continue
        key = (host.id, item.port, item.service_name)
        if key in seen:
            continue
        seen.add(key)
        reachable: bool | None = None
        if platform is not None:
            status = _decide(snap, _endpoint_for_host(snap, platform), _endpoint_for_host(snap, host),
                             item.port, "tcp")[0]
            reachable = None if status == "unknown" else status == "open"
        out.append(
            PortUsageResponse(
                id=None,
                host_id=host.id,
                host_name=host.name,
                host_ip=host.ip,
                port=item.port,
                protocol="tcp",
                service_name=item.service_name,
                description=item.description,
                source="auto",
                auto_kind=item.auto_kind,
                reachable_from_platform=reachable,
                verified=True,
            )
        )
    return out, failed


_AUTO_KIND_LABELS = {
    "db": "database",
    "s3": "S3 storage",
    "node_exporter": "node_exporter",
    "dcgm": "dcgm-exporter",
    "metrics": "metrics endpoint",
    "gateway_upstream": "gateway upstream",
}


def redact_discovered(usages: list[PortUsageResponse]) -> list[PortUsageResponse]:
    """Discovered usages with UniBridge's own names (DB aliases, upstreams…) removed."""
    return [
        u.model_copy(update={
            "service_name": f"{_AUTO_KIND_LABELS.get(u.auto_kind or '', 'service')} (discovered)",
            "description": "",
        })
        if u.source == "auto"
        else u
        for u in usages
    ]


async def all_usages(
    db: AsyncSession, snap: Snapshot, *, include_gateway: bool = True
) -> tuple[list[PortUsageResponse], list[str]]:
    auto, failed = await discover_usages(db, snap, include_gateway=include_gateway)
    stored = [stored_usage_response(snap, u) for u in snap.usages]
    return stored + auto, failed


# ── Access check ────────────────────────────────────────────────────────────


@dataclass
class _Endpoint:
    input: str
    host: NetworkHost | None
    ip: str | None
    region_id: int | None


def _endpoint_for_host(snap: Snapshot, host: NetworkHost) -> _Endpoint:
    return _Endpoint(input=host.name, host=host, ip=host.ip, region_id=host.region_id)


def resolve_endpoint(snap: Snapshot, ref: str) -> _Endpoint:
    """A registered host (name or IP), or an unregistered IP placed by CIDR."""
    host = snap.find_host(ref)
    if host is not None:
        return _Endpoint(input=ref, host=host, ip=host.ip, region_id=host.region_id)
    addr = parse_ip(ref)
    if addr is None:
        raise NetworkMapError(404, f"No registered host named {ref!r}, and it is not an IP address")
    return _Endpoint(input=ref, host=None, ip=str(addr), region_id=snap.region_for_ip(str(addr)))


def _covers_src(opening: FirewallOpening, src: _Endpoint) -> bool:
    if opening.src_host_id is not None:
        return src.host is not None and opening.src_host_id == src.host.id
    net = _network(opening.src_cidr or "")
    addr = parse_ip(src.ip)
    return net is not None and addr is not None and addr.version == net.version and addr in net


def _opening_matches(
    opening: FirewallOpening, src: _Endpoint, dst: _Endpoint, port: int, protocol: str
) -> bool:
    if dst.host is None or opening.dst_host_id != dst.host.id:
        return False
    if opening.protocol != protocol or not (opening.port_from <= port <= opening.port_to):
        return False
    return _covers_src(opening, src)


def _decide(
    snap: Snapshot, src: _Endpoint, dst: _Endpoint, port: int, protocol: str
) -> tuple[str, str, list[FirewallOpening]]:
    if src.region_id is not None and src.region_id == dst.region_id:
        return "open", "same_region", []
    candidates = snap.openings_by_dst.get(dst.host.id, []) if dst.host is not None else []
    matching = [o for o in candidates if _opening_matches(o, src, dst, port, protocol)]
    open_ones = [o for o in matching if o.status == "open"]
    if open_ones:
        return "open", "opening", open_ones
    # Someone else's server: only a recorded opening speaks for it.
    if is_external(src.host) or is_external(dst.host):
        return "unknown", "external_host", matching
    # Without both regions, "no opening" does not prove it is blocked.
    if src.region_id is None or dst.region_id is None:
        return "unknown", "unknown_region", matching
    if matching:
        return "blocked", "opening_closed", matching
    return "blocked", "no_opening", []


def _endpoint_view(snap: Snapshot, ep: _Endpoint) -> AccessEndpoint:
    return AccessEndpoint(
        input=ep.input,
        host_id=ep.host.id if ep.host is not None else None,
        host_name=ep.host.name if ep.host is not None else None,
        ip=ep.ip,
        region_id=ep.region_id,
        region_name=snap.region_name(ep.region_id),
    )


def _describe(ep: AccessEndpoint) -> str:
    name = f"{ep.host_name} ({ep.ip})" if ep.host_name else (ep.ip or ep.input)
    return f"{name} / region {ep.region_name or '미지정'}"


def request_draft(src: AccessEndpoint, dst: AccessEndpoint, port: int, protocol: str) -> str:
    return "\n".join(
        [
            "[방화벽 해제 신청]",
            f"출발지: {_describe(src)}",
            f"목적지: {_describe(dst)}",
            f"프로토콜/포트: {protocol.upper()} {port}",
            "방향: 출발지 → 목적지 (단방향)",
            "사용 목적: (작성)",
        ]
    )


_MESSAGES = {
    "same_region": "Same region: hosts in one region reach each other on every port.",
    "opening": "Open through a recorded firewall opening.",
    "opening_closed": "A firewall opening was recorded for this but is marked closed.",
    "no_opening": "Different regions and no firewall opening is recorded: request one.",
    "unknown_region": "A host is not placed in a region, so access cannot be judged.",
    "external_host": (
        "An external host (not ours) is involved and no open opening covers this: "
        "the map cannot tell whether it is reachable."
    ),
}


async def check_access(
    db: AsyncSession,
    snap: Snapshot,
    src_ref: str,
    dst_ref: str,
    port: int,
    protocol: str,
    *,
    redact: bool = False,
) -> AccessCheckResponse:
    src = resolve_endpoint(snap, src_ref)
    dst = resolve_endpoint(snap, dst_ref)
    status, reason, openings = _decide(snap, src, dst, port, protocol)
    usages: list[PortUsageResponse] = []
    if dst.host is not None:
        everything, _failed = await all_usages(db, snap)
        usages = [
            u for u in everything
            if u.host_id == dst.host.id and u.port == port and u.protocol == protocol
        ]
    shown = redact_discovered(usages) if redact else usages
    src_view, dst_view = _endpoint_view(snap, src), _endpoint_view(snap, dst)
    probe = None
    if src.host is not None and dst.host is not None:
        probe = next(
            (
                p for p in snap.probes
                if p.src_host_id == src.host.id and p.dst_host_id == dst.host.id
                and p.port == port and p.protocol == protocol
            ),
            None,
        )
    return AccessCheckResponse(
        status=status,
        reason=reason,
        message=_MESSAGES[reason],
        src=src_view,
        dst=dst_view,
        port=port,
        protocol=protocol,
        openings=[opening_response(snap, o, usages) for o in openings],
        usages=shown,
        request_draft=request_draft(src_view, dst_view, port, protocol)
        if status == "blocked"
        else None,
        probe=probe_response(snap, probe) if probe is not None else None,
    )


# ── Overview ────────────────────────────────────────────────────────────────


async def overview(db: AsyncSession, snap: Snapshot) -> NetworkOverview:
    usages, failed = await all_usages(db, snap)
    links: dict[tuple[int | None, int | None], RegionLink] = {}
    unused = 0
    for opening in snap.openings:
        dst = snap.hosts.get(opening.dst_host_id)
        if dst is None:
            continue
        key = (snap.opening_src_region(opening), dst.region_id)
        link = links.setdefault(key, RegionLink(src_region_id=key[0], dst_region_id=key[1]))
        link.openings += 1
        if opening.status == "open":
            link.open_openings += 1
            label = port_label(opening.protocol, opening.port_from, opening.port_to)
            if label not in link.ports:
                link.ports.append(label)
            if not any(
                u.host_id == dst.id and _usage_in_range(u.port, u.protocol, opening) for u in usages
            ):
                unused += 1
        if opening.verified_at is None:
            link.unverified += 1
    for link in links.values():
        link.ports.sort()
    unverified = (
        sum(1 for h in snap.hosts.values() if h.verified_at is None)
        + sum(1 for o in snap.openings if o.verified_at is None)
        + sum(1 for u in snap.usages if u.verified_at is None)
    )
    return NetworkOverview(
        regions=[region_response(snap, r) for r in sorted(snap.regions.values(), key=lambda r: r.name)],
        links=sorted(
            links.values(),
            key=lambda link: (link.src_region_id or 0, link.dst_region_id or 0),
        ),
        host_count=sum(1 for h in snap.hosts.values() if not is_external(h)),
        external_host_count=sum(1 for h in snap.hosts.values() if is_external(h)),
        unassigned_hosts=sum(
            1 for h in snap.hosts.values() if h.region_id is None and not is_external(h)
        ),
        opening_count=len(snap.openings),
        unverified_count=unverified,
        unused_openings=unused,
        unreachable_usages=sum(1 for u in usages if u.reachable_from_platform is False),
        probe_mismatches=sum(1 for p in snap.probes if probe_mismatch(snap, p) is not None),
        platform_host_id=snap.settings.platform_host_id,
        main_host_id=snap.settings.main_host_id,
        auto_sources_failed=failed,
    )


# ── Probes: measured connection attempts ────────────────────────────────────

# A connection that got an answer passed the firewall, listener or not.
PASSED_RESULTS = frozenset({"open", "refused"})


def probe_expectation(snap: Snapshot, probe: NetworkProbe) -> tuple[str, str]:
    src, dst = snap.hosts[probe.src_host_id], snap.hosts[probe.dst_host_id]
    status, reason, _ = _decide(
        snap, _endpoint_for_host(snap, src), _endpoint_for_host(snap, dst), probe.port, probe.protocol
    )
    return status, reason


def probe_mismatch(snap: Snapshot, probe: NetworkProbe) -> str | None:
    """Where the measurement contradicts the records, and how (memoized per snapshot)."""
    if probe.id in snap._mismatch:
        return snap._mismatch[probe.id]
    expected, _reason = probe_expectation(snap, probe)
    result: str | None = None
    if expected == "open" and probe.result == "timeout":
        result = "recorded_open_but_blocked"
    elif expected == "blocked" and probe.result in PASSED_RESULTS:
        result = "passes_without_opening"
    if probe.id is not None:
        snap._mismatch[probe.id] = result
    return result


def probe_response(snap: Snapshot, probe: NetworkProbe) -> ProbeResponse:
    src, dst = snap.hosts[probe.src_host_id], snap.hosts[probe.dst_host_id]
    expected, reason = probe_expectation(snap, probe)
    mismatch = None
    if expected == "open" and probe.result == "timeout":
        mismatch = "recorded_open_but_blocked"
    elif expected == "blocked" and probe.result in PASSED_RESULTS:
        mismatch = "passes_without_opening"
    return ProbeResponse(
        id=probe.id,
        src_host_id=src.id,
        src_host_name=src.name,
        src_host_ip=src.ip,
        src_region_name=snap.region_name(src.region_id),
        dst_host_id=dst.id,
        dst_host_name=dst.name,
        dst_host_ip=dst.ip,
        dst_region_name=snap.region_name(dst.region_id),
        port=probe.port,
        protocol=probe.protocol,
        result=probe.result,
        latency_ms=probe.latency_ms,
        error=probe.error or "",
        reported_by=probe.reported_by,
        observed_at=probe.observed_at,
        expected=expected,
        expected_reason=reason,
        mismatch=mismatch,
    )


def probe_summary(snap: Snapshot, probe: NetworkProbe) -> ProbeSummary:
    return ProbeSummary(
        id=probe.id,
        src_host_name=snap.hosts[probe.src_host_id].name,
        port=probe.port,
        result=probe.result,
        observed_at=probe.observed_at,
        mismatch=probe_mismatch(snap, probe),
    )


def source_covers_host(opening: FirewallOpening, host: NetworkHost) -> bool:
    if opening.src_host_id is not None:
        return opening.src_host_id == host.id
    net = _network(opening.src_cidr or "")
    addr = parse_ip(host.ip)
    return net is not None and addr is not None and addr.version == net.version and addr in net


def _source_covers(snap: Snapshot, opening: FirewallOpening, host: NetworkHost) -> bool:
    return source_covers_host(opening, host)


def opening_last_probe(snap: Snapshot, opening: FirewallOpening) -> NetworkProbe | None:
    """The most recent probe that went through this opening's path."""
    best: NetworkProbe | None = None
    for probe in snap.probes_by_dst.get((opening.dst_host_id, opening.protocol), []):
        if not (opening.port_from <= probe.port <= opening.port_to):
            continue
        src = snap.hosts.get(probe.src_host_id)
        if src is None or not _source_covers(snap, opening, src):
            continue
        if best is None or probe.observed_at > best.observed_at:
            best = probe
    return best


# A range wider than this is probed on its first port and on ports with a
# known service, not port by port.
PROBE_RANGE_EXPAND_MAX = 16


def probe_targets(
    snap: Snapshot, src: NetworkHost, usages: list[PortUsageResponse]
) -> list[ProbeTarget]:
    """What ``src`` should test: every recorded opening whose source covers it
    (closed ones too, to confirm they are closed)."""
    seen: set[tuple[int, int, str]] = set()
    out: list[ProbeTarget] = []
    src_ep = _endpoint_for_host(snap, src)
    for opening in snap.openings:
        if opening.dst_host_id == src.id or not _source_covers(snap, opening, src):
            continue
        dst = snap.hosts.get(opening.dst_host_id)
        if dst is None:
            continue
        if opening.port_to - opening.port_from < PROBE_RANGE_EXPAND_MAX:
            ports = list(range(opening.port_from, opening.port_to + 1))
        else:
            ports = sorted(
                {opening.port_from}
                | {
                    u.port for u in usages
                    if u.host_id == dst.id and _usage_in_range(u.port, u.protocol, opening)
                }
            )
        for port in ports:
            key = (dst.id, port, opening.protocol)
            if key in seen:
                continue
            seen.add(key)
            expected, reason, _ = _decide(
                snap, src_ep, _endpoint_for_host(snap, dst), port, opening.protocol
            )
            out.append(
                ProbeTarget(
                    dst_host_id=dst.id,
                    dst_host_name=dst.name,
                    dst_ip=dst.ip,
                    port=port,
                    protocol=opening.protocol,
                    expected=expected,
                    expected_reason=reason,
                    opening_id=opening.id,
                )
            )
    return out


# ── Port finder ─────────────────────────────────────────────────────────────
#
# "I am bringing up a service that these sources must reach: which ports work
# today without a firewall request?" A port works for a destination host when
# every source reaches it (same region, or an open opening covering that
# source) and nothing listens there yet.

PORT_MIN, PORT_MAX = 1, 65535
FINDER_MAX_HOSTS = 300
FINDER_MAX_SPANS = 200
FINDER_SUGGESTIONS = 8

Span = tuple[int, int]


def merge_spans(spans: Iterable[Span]) -> list[Span]:
    out: list[Span] = []
    for lo, hi in sorted(spans):
        if out and lo <= out[-1][1] + 1:
            if hi > out[-1][1]:
                out[-1] = (out[-1][0], hi)
        else:
            out.append((lo, hi))
    return out


def intersect_spans(a: list[Span], b: list[Span]) -> list[Span]:
    out: list[Span] = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if lo <= hi:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def subtract_ports(spans: list[Span], ports: Iterable[int]) -> list[Span]:
    holes = sorted(set(ports))
    out: list[Span] = []
    k = 0
    for lo, hi in spans:
        while k < len(holes) and holes[k] < lo:
            k += 1
        start = lo
        m = k
        while m < len(holes) and holes[m] <= hi:
            if holes[m] > start:
                out.append((start, holes[m] - 1))
            start = holes[m] + 1
            m += 1
        if start <= hi:
            out.append((start, hi))
    return out


def span_count(spans: Iterable[Span]) -> int:
    return sum(hi - lo + 1 for lo, hi in spans)


def _in_spans(port: int, spans: list[Span]) -> bool:
    return any(lo <= port <= hi for lo, hi in spans)


def _reach(snap: Snapshot, src: _Endpoint, dst: NetworkHost, protocol: str) -> tuple[list[Span], bool]:
    """Ports ``src`` reaches on ``dst`` (merged spans), and whether by region."""
    if src.region_id is not None and src.region_id == dst.region_id:
        return [(PORT_MIN, PORT_MAX)], True
    return merge_spans(
        (o.port_from, o.port_to)
        for o in snap.openings_by_dst.get(dst.id, [])
        if o.status == "open" and o.protocol == protocol and _covers_src(o, src)
    ), False


def _opened_since(snap: Snapshot, src: _Endpoint, dst: NetworkHost, probe: NetworkProbe) -> bool:
    """An opening for this path was recorded or changed after the probe timed
    out: the timeout may predate the firewall change, so it is not evidence."""
    for o in snap.openings_by_dst.get(dst.id, []):
        if (
            o.status == "open"
            and o.protocol == probe.protocol
            and o.port_from <= probe.port <= o.port_to
            and _covers_src(o, src)
        ):
            changed = o.updated_at or o.created_at
            if changed is not None and changed > probe.observed_at:
                return True
    return False


def resolve_sources(
    snap: Snapshot, refs: Iterable[str], region_ids: Iterable[int]
) -> list[_Endpoint]:
    """Host names/IPs plus every host of the given regions, each once."""
    out: list[_Endpoint] = []
    seen_hosts: set[int] = set()
    seen_ips: set[str] = set()

    def add(ep: _Endpoint) -> None:
        if ep.host is not None:
            if ep.host.id in seen_hosts:
                return
            seen_hosts.add(ep.host.id)
        elif ep.ip is not None:
            if ep.ip in seen_ips:
                return
            seen_ips.add(ep.ip)
        out.append(ep)

    for ref in refs:
        add(resolve_endpoint(snap, ref))
    for region_id in region_ids:
        region = snap.regions.get(region_id)
        if region is None:
            raise NetworkMapError(404, f"Region {region_id} not found")
        members = sorted(
            (h for h in snap.hosts.values() if h.region_id == region_id), key=lambda h: h.name
        )
        if not members:
            raise NetworkMapError(422, f"Region {region.name!r} has no registered hosts")
        for host in members:
            add(_endpoint_for_host(snap, host))
    if not out:
        raise NetworkMapError(422, "Give at least one source (src or src_region)")
    return out


def _finder_host(
    snap: Snapshot,
    dst: NetworkHost,
    sources: list[_Endpoint],
    protocol: str,
    port: int | None,
    usages: list[PortUsageResponse],
) -> PortFinderHost:
    reach: list[Span] = [(PORT_MIN, PORT_MAX)]
    any_port = True
    for src in sources:
        spans, by_region = _reach(snap, src, dst, protocol)
        any_port = any_port and by_region
        reach = intersect_spans(reach, spans)
        if not reach:
            break

    used: dict[int, FinderUsedPort] = {}
    for u in usages:
        if u.protocol == protocol and u.port not in used:
            used[u.port] = FinderUsedPort(
                port=u.port,
                service_name=u.service_name,
                kind="auto" if u.source == "auto" else "recorded",
                auto_kind=u.auto_kind,
            )

    # Whether something listens: the newest probe that got through the firewall
    # (open = a listener, refused = none), from any source. A timeout says
    # nothing about the service, so it never hides an earlier "open".
    latest_passed: dict[int, NetworkProbe] = {}
    by_id = {s.host.id: s for s in sources if s.host is not None}
    refused_from: dict[int, list[str]] = {}
    blocked: list[FinderBlockedPort] = []
    for probe in snap.probes_by_dst.get((dst.id, protocol), []):
        if probe.result in PASSED_RESULTS:
            prev = latest_passed.get(probe.port)
            if prev is None or probe.observed_at > prev.observed_at:
                latest_passed[probe.port] = probe
        src = by_id.get(probe.src_host_id)
        if src is None or src.host is None or not _in_spans(probe.port, reach):
            continue
        if probe.result == "refused":
            refused_from.setdefault(probe.port, []).append(src.host.name)
        elif probe.result == "timeout" and not _opened_since(snap, src, dst, probe):
            blocked.append(
                FinderBlockedPort(
                    port=probe.port, src_host_name=src.host.name, observed_at=probe.observed_at
                )
            )
    for p, probe in latest_passed.items():
        if probe.result == "open" and p not in used:
            used[p] = FinderUsedPort(port=p, service_name="", kind="listening")

    blocked_ports = {b.port for b in blocked}
    taken = set(used) | blocked_ports
    free = subtract_ports(reach, taken)
    free_count = span_count(free)

    def suggestion(p: int) -> PortSuggestion:
        names = sorted(refused_from.get(p, []))
        return PortSuggestion(
            port=p, evidence="probed_free" if names else "recorded", probed_from=names
        )

    suggestions: list[PortSuggestion] = []
    if not any_port:
        picked: list[int] = []
        if port is not None and _in_spans(port, free):
            picked.append(port)
        # Measured-free ports first, then the lowest free ports (nearest the
        # requested one when given).
        for p in sorted(refused_from, key=lambda q: (abs(q - port) if port else 0, q)):
            if len(picked) >= FINDER_SUGGESTIONS:
                break
            if p not in picked and _in_spans(p, free):
                picked.append(p)
        if len(picked) < FINDER_SUGGESTIONS:
            pool: list[int] = []
            window = FINDER_SUGGESTIONS * 4
            for lo, hi in free:
                pool.extend(range(lo, min(hi, lo + window) + 1))
                if port is not None and lo <= port + window and hi >= port - window:
                    pool.extend(range(max(lo, port - window), min(hi, port + window) + 1))
            if port is not None:
                pool.sort(key=lambda q: (abs(q - port), q))
            for p in pool:
                if len(picked) >= FINDER_SUGGESTIONS:
                    break
                if p not in picked:
                    picked.append(p)
        suggestions = [suggestion(p) for p in picked]

    requested = None
    if port is not None:
        if port in blocked_ports:
            requested = "blocked"
        elif not _in_spans(port, reach):
            requested = "unreachable"
        elif port in used:
            requested = "used"
        else:
            requested = "free"

    shown_used = sorted(
        (u for u in used.values() if any_port or _in_spans(u.port, reach)), key=lambda u: u.port
    )
    return PortFinderHost(
        host=host_response(snap, dst),
        any_port=any_port,
        reachable=[PortSpan(port_from=lo, port_to=hi) for lo, hi in reach],
        reachable_count=span_count(reach),
        free=[PortSpan(port_from=lo, port_to=hi) for lo, hi in free[:FINDER_MAX_SPANS]],
        free_count=free_count,
        free_truncated=len(free) > FINDER_MAX_SPANS,
        used=shown_used,
        blocked=sorted(blocked, key=lambda b: (b.port, b.src_host_name)),
        suggestions=suggestions,
        requested_port=requested,
    )


async def find_ports(
    db: AsyncSession,
    snap: Snapshot,
    *,
    src_refs: Iterable[str],
    src_region_ids: Iterable[int],
    dst_ref: str | None,
    dst_region_id: int | None,
    protocol: str,
    port: int | None,
    redact: bool = False,
) -> PortFinderResponse:
    sources = resolve_sources(snap, src_refs, src_region_ids)
    if dst_region_id is not None and dst_region_id not in snap.regions:
        raise NetworkMapError(404, f"Region {dst_region_id} not found")
    source_host_ids = {s.host.id for s in sources if s.host is not None}
    if dst_ref:
        dst_ep = resolve_endpoint(snap, dst_ref)
        if dst_ep.host is None:
            raise NetworkMapError(404, f"Destination {dst_ref!r} is not a registered host")
        if is_external(dst_ep.host):
            raise NetworkMapError(
                422,
                f"Destination {dst_ref!r} is an external host: the port finder only "
                "suggests ports on servers we run",
            )
        candidates = [dst_ep.host]
    else:
        candidates = sorted(
            (
                h for h in snap.hosts.values()
                if h.id not in source_host_ids
                and not is_external(h)
                and (dst_region_id is None or h.region_id == dst_region_id)
            ),
            key=lambda h: h.name,
        )
    usages, _failed = await all_usages(db, snap)
    if redact:
        usages = redact_discovered(usages)
    usages_by_host: dict[int, list[PortUsageResponse]] = {}
    for u in usages:
        usages_by_host.setdefault(u.host_id, []).append(u)
    results = [
        _finder_host(snap, h, sources, protocol, port, usages_by_host.get(h.id, []))
        for h in candidates
    ]
    unplaced_destinations: list[str] = []
    if not dst_ref:
        # A host in no region is only reachable through openings naming it; say
        # which ones were left out for that reason.
        unplaced_destinations = sorted(
            r.host.name for r in results if r.host.region_id is None and r.reachable_count == 0
        )
        results = [
            r for r in results
            if r.free_count > 0 and (port is None or r.requested_port == "free")
        ]
        # No firewall request at all first, then measured-free, then most room.
        results.sort(
            key=lambda r: (
                not r.any_port,
                -sum(1 for s in r.suggestions if s.evidence == "probed_free"),
                -r.free_count,
                r.host.name,
            )
        )
    return PortFinderResponse(
        protocol=protocol,
        port=port,
        sources=[_endpoint_view(snap, s) for s in sources],
        # An external source is in no region by design, not waiting to be placed.
        unplaced_sources=[
            s.input for s in sources if s.region_id is None and not is_external(s.host)
        ],
        unplaced_destinations=unplaced_destinations[:FINDER_MAX_HOSTS],
        hosts=results[:FINDER_MAX_HOSTS],
        truncated=len(results) > FINDER_MAX_HOSTS,
    )
