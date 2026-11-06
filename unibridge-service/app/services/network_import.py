"""CSV bulk import for the network map: hosts, firewall openings, port usage.

Built for the sheets people already keep (or export from the company firewall
system): the header row may use English or Korean column names
(``HEADER_ALIASES``), unknown columns are ignored, and every line gets its own
verdict — ``create``, ``skip`` (already recorded, or not needed such as an
opening inside one region) or ``error`` — so a dry run shows exactly what an
import would do. The valid lines are written in one transaction.

Hosts: an optional ``kind`` column says ``managed`` (ours, the default) or
``external`` (someone else's: never placed in a region).

Openings: ``src`` is a registered host (name or IP), a CIDR, or an IP no host
has (recorded as that ``/32`` or ``/128``); ``dst`` must be a registered host,
or — with ``create_missing_destinations`` — an IP no host has, which becomes
an external host named ``dst-<ip>``; ``ports`` is ``5432``, ``8000-8010`` (or ``~``), or several separated by
commas or spaces (one opening each).
"""
from __future__ import annotations

import csv
import io
import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db_types import utcnow
from app.models import FirewallOpening, NetworkHost, PortUsage
from app.schemas import (
    NetworkHostCreate,
    NetworkImportRequest,
    NetworkImportResponse,
    NetworkImportRow,
    PortUsageCreate,
)
from app.services import network_map
from app.services.network_map import Snapshot

MAX_ROWS = 5000
# Openings one ports cell may expand to, and openings one import may create.
MAX_SPANS_PER_LINE = 64
MAX_CREATED = 5000

HEADER_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "hosts": {
        "name": ("name", "host", "hostname", "이름", "호스트", "호스트명", "서버", "서버명"),
        "ip": ("ip", "ipaddress", "address", "주소", "ip주소", "아이피"),
        "region": ("region", "zone", "리전", "지역", "존", "망"),
        "owner": ("owner", "team", "담당자", "담당", "담당팀", "팀"),
        "description": ("description", "desc", "note", "설명", "비고", "용도"),
        # Only unambiguous names: a sheet's own "구분"/"type" column (WEB, DB, …)
        # must keep being ignored as before.
        "kind": ("kind", "hostkind", "hosttype", "호스트구분", "호스트종류"),
    },
    "openings": {
        "src": ("src", "source", "from", "srcip", "sourceip", "출발지", "출발", "출발지ip", "소스", "source주소"),
        "dst": ("dst", "destination", "to", "dstip", "destinationip", "목적지", "도착지", "목적지ip", "대상", "대상서버"),
        "ports": ("ports", "port", "dstport", "포트", "목적지포트", "서비스포트"),
        "protocol": ("protocol", "proto", "프로토콜"),
        "status": ("status", "state", "상태"),
        "ticket_no": ("ticketno", "ticket", "requestno", "신청번호", "요청번호", "티켓", "티켓번호", "결재번호"),
        "requester": ("requester", "requestedby", "신청자", "요청자"),
        "purpose": ("purpose", "reason", "용도", "목적", "사유", "설명", "비고"),
    },
    "port_usages": {
        "host": ("host", "hostname", "name", "ip", "호스트", "호스트명", "서버", "서버명"),
        "port": ("port", "포트"),
        "protocol": ("protocol", "proto", "프로토콜"),
        "service_name": ("servicename", "service", "서비스", "서비스명", "프로세스"),
        "owner": ("owner", "team", "담당자", "담당", "팀"),
        "description": ("description", "desc", "note", "설명", "비고"),
    },
}
REQUIRED: dict[str, tuple[str, ...]] = {
    "hosts": ("name", "ip"),
    "openings": ("src", "dst", "ports"),
    "port_usages": ("host", "port", "service_name"),
}
_OPEN_WORDS = {"", "open", "opened", "allow", "allowed", "active", "approved", "열림", "허용", "개방", "승인", "완료", "사용"}
_MANAGED_WORDS = {"", "managed", "internal", "ours", "관리", "내부", "자사"}
_EXTERNAL_WORDS = {"external", "thirdparty", "외부", "타사", "대외"}
_CLOSED_WORDS = {"closed", "close", "revoked", "expired", "deleted", "deny", "닫힘", "해제", "폐기", "만료", "삭제", "차단", "반납"}


class ImportFormatError(Exception):
    """The file as a whole cannot be read (header, size)."""


def _norm(name: str) -> str:
    return re.sub(r"[\s_\-./()]", "", name.strip().lower())


def _column_map(kind: str, header: list[str]) -> dict[str, int]:
    aliases = {_norm(a): field for field, names in HEADER_ALIASES[kind].items() for a in names}
    mapping: dict[str, int] = {}
    for index, raw in enumerate(header):
        field_name = aliases.get(_norm(raw))
        if field_name is not None and field_name not in mapping:
            mapping[field_name] = index
    missing = [f for f in REQUIRED[kind] if f not in mapping]
    if missing:
        raise ImportFormatError(
            f"The header row has no column for: {', '.join(missing)} "
            f"(accepted names: {', '.join(HEADER_ALIASES[kind][missing[0]])})"
        )
    return mapping


def parse_rows(kind: str, text: str) -> list[tuple[int, dict[str, str]]]:
    """(line number, {field: value}) for each non-empty data line."""
    text = text.lstrip("﻿")
    try:
        delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",\t;").delimiter
    except csv.Error:
        delimiter = ","
    # Only the delimiter is guessed: a guessed quote character (e.g. "'" from a
    # value like 'ops') would silently merge the following lines into one field.
    reader = csv.reader(io.StringIO(text), delimiter=delimiter, quotechar='"')
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ImportFormatError("The file is empty") from exc
    mapping = _column_map(kind, header)
    rows: list[tuple[int, dict[str, str]]] = []
    for values in reader:
        if not any(v.strip() for v in values):
            continue
        line = reader.line_num
        rows.append(
            (line, {f: (values[i].strip() if i < len(values) else "") for f, i in mapping.items()})
        )
        if len(rows) > MAX_ROWS:
            raise ImportFormatError(f"At most {MAX_ROWS} lines per import")
    return rows


def parse_ports(value: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for part in re.split(r"[,\s;/]+", value.strip()):
        if not part:
            continue
        match = re.fullmatch(r"(\d{1,5})(?:\s*[-~]\s*(\d{1,5}))?", part)
        if not match:
            raise ValueError(f"not a port or port range: {part!r}")
        low = int(match.group(1))
        high = int(match.group(2) or low)
        if not (1 <= low <= high <= 65535):
            raise ValueError(f"port range out of 1-65535 or reversed: {part!r}")
        spans.append((low, high))
    if not spans:
        raise ValueError("no port given")
    if len(spans) > MAX_SPANS_PER_LINE:
        raise ValueError(
            f"at most {MAX_SPANS_PER_LINE} ports or ranges per line; use a range (8000-8100)"
        )
    return spans


def parse_status(value: str) -> str:
    word = value.strip().lower()
    if word in _OPEN_WORDS:
        return "open"
    if word in _CLOSED_WORDS:
        return "closed"
    raise ValueError(f"unknown status {value!r} (use open/closed, 열림/해제)")


def parse_kind(value: str) -> str:
    word = _norm(value)
    if word in _MANAGED_WORDS:
        return "managed"
    if word in _EXTERNAL_WORDS:
        return network_map.EXTERNAL
    raise ValueError(f"unknown kind {value!r} (use managed/external, 관리/외부)")


def parse_protocol(value: str) -> str:
    word = value.strip().lower() or "tcp"
    if word not in ("tcp", "udp"):
        raise ValueError(f"protocol must be tcp or udp, got {value!r}")
    return word


def _validation_message(exc: ValidationError) -> str:
    return "; ".join(str(err.get("msg", "")).removeprefix("Value error, ") for err in exc.errors())


@dataclass
class _Run:
    db: AsyncSession
    snap: Snapshot
    actor: str
    dry_run: bool
    create_missing_destinations: bool = False
    rows: list[NetworkImportRow] = field(default_factory=list)
    pending: list[Any] = field(default_factory=list)
    # External hosts this import adds for unregistered destination IPs (by IP),
    # and the openings that point at each until it has an id.
    new_hosts: dict[str, NetworkHost] = field(default_factory=dict)
    new_host_openings: list[tuple[FirewallOpening, NetworkHost]] = field(default_factory=list)

    def add(self, line: int, action: str, key: str, message: str = "") -> None:
        self.rows.append(NetworkImportRow(line=line, action=action, key=key, message=message))

    def stamp(self, row: Any) -> None:
        row.source = "import"
        row.created_by = self.actor
        row.verified_at = utcnow()
        row.verified_by = self.actor


def _import_hosts(run: _Run, rows: list[tuple[int, dict[str, str]]]) -> None:
    names = {h.name.lower() for h in run.snap.hosts.values()}
    ips = {network_map.parse_ip(h.ip) for h in run.snap.hosts.values()}
    regions = {r.name.lower(): r.id for r in run.snap.regions.values()}
    for line, row in rows:
        key = row.get("name", "")
        try:
            body = NetworkHostCreate(
                name=row.get("name", ""),
                ip=row.get("ip", ""),
                description=row.get("description", ""),
                owner=row.get("owner", ""),
                kind=parse_kind(row.get("kind", "")),
            )
        except ValidationError as exc:
            run.add(line, "error", key, _validation_message(exc))
            continue
        except ValueError as exc:
            run.add(line, "error", key, str(exc))
            continue
        if body.name.lower() in names:
            run.add(line, "skip", body.name, "a host with this name is already registered")
            continue
        if network_map.parse_ip(body.ip) in ips:
            run.add(line, "skip", body.name, f"IP {body.ip} is already registered")
            continue
        region_name = row.get("region", "").strip()
        external = body.kind == network_map.EXTERNAL
        if region_name and external:
            run.add(line, "error", body.name, "an external host is in no region: leave region empty")
            continue
        if region_name:
            region_id = regions.get(region_name.lower())
            if region_id is None:
                run.add(line, "error", body.name, f"unknown region {region_name!r}: define it first")
                continue
        else:
            region_id = None if external else run.snap.region_for_ip(body.ip)
        host = NetworkHost(
            name=body.name, ip=body.ip, region_id=region_id,
            description=body.description, owner=body.owner, kind=body.kind,
        )
        run.stamp(host)
        run.pending.append(host)
        names.add(body.name.lower())
        ips.add(network_map.parse_ip(body.ip))
        where = "external" if external else run.snap.region_name(region_id) or "no region"
        run.add(line, "create", body.name, f"{body.ip} · {where}")


def _source_for(run: _Run, ref: str) -> tuple[int | None, str | None]:
    """(src_host_id, src_cidr) for a host name/IP, a CIDR, or an unregistered IP."""
    ref = ref.strip()
    if "/" in ref:
        try:
            return None, str(ipaddress.ip_network(ref, strict=False))
        except ValueError as exc:
            raise ValueError(f"invalid CIDR {ref!r}") from exc
    host = run.snap.find_host(ref)
    if host is not None:
        return host.id, None
    addr = network_map.parse_ip(ref)
    if addr is None:
        raise ValueError(f"source {ref!r} is no registered host, IP or CIDR")
    return None, str(ipaddress.ip_network(addr))


def _destination_for(run: _Run, ref: str) -> tuple[NetworkHost, bool]:
    """(host, whether this import adds it) for an opening's destination."""
    host = run.snap.find_host(ref)
    if host is not None:
        return host, False
    addr = network_map.parse_ip(ref)
    if addr is not None and str(addr) in run.new_hosts:
        return run.new_hosts[str(addr)], True
    if not run.create_missing_destinations:
        hint = " (or let the import add missing destinations)" if addr is not None else ""
        raise ValueError(
            f"destination {ref!r} is not a registered host: import or add it first{hint}"
        )
    try:
        host = network_map.new_external_host(run.snap, ref)
    except network_map.NetworkMapError as exc:
        raise ValueError(str(exc.detail)) from exc
    if any(h.name.lower() == host.name.lower() for h in run.new_hosts.values()):
        raise ValueError(f"a host named {host.name!r} is already added by this import")
    return host, True


def _import_openings(run: _Run, rows: list[tuple[int, dict[str, str]]]) -> None:
    from app.routers.network import _validate_opening, set_src_key  # shared rules

    taken = {
        (o.src_key, o.dst_host_id, o.protocol, o.port_from, o.port_to) for o in run.snap.openings
    }
    for line, row in rows:
        label = f"{row.get('src', '')} → {row.get('dst', '')}:{row.get('ports', '')}"
        try:
            src_host_id, src_cidr = _source_for(run, row.get("src", ""))
            spans = parse_ports(row.get("ports", ""))
            protocol = parse_protocol(row.get("protocol", ""))
            status = parse_status(row.get("status", ""))
            dst, dst_is_new = _destination_for(run, row.get("dst", "").strip())
        except ValueError as exc:
            run.add(line, "error", label, str(exc))
            continue
        # Until the import saves a new host its openings key on its IP.
        dst_key: Any = ("new", dst.ip) if dst_is_new else dst.id
        for low, high in spans:
            key = f"{row.get('src', '')} → {dst.name} {network_map.port_label(protocol, low, high)}"
            opening = FirewallOpening(
                src_host_id=src_host_id, src_cidr=src_cidr,
                dst_host_id=None if dst_is_new else dst.id,
                port_from=low, port_to=high, protocol=protocol, status=status,
                ticket_no=row.get("ticket_no", "")[:100],
                requester=row.get("requester", "")[:100],
                purpose=row.get("purpose", "")[:500],
            )
            set_src_key(opening)
            if len(run.pending) >= MAX_CREATED:
                run.add(line, "error", key, f"at most {MAX_CREATED} openings per import")
                continue
            identity = (opening.src_key, dst_key, protocol, low, high)
            if identity in taken:
                run.add(line, "skip", key, "already recorded")
                continue
            try:
                _validate_opening(run.snap, opening, dst=dst if dst_is_new else None)
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
                same_region = "same region" in detail
                run.add(line, "skip" if same_region else "error", key,
                        "same region: already open, no opening needed" if same_region else detail)
                continue
            run.stamp(opening)
            run.pending.append(opening)
            taken.add(identity)
            message = status
            if dst_is_new:
                if dst.ip not in run.new_hosts:
                    run.stamp(dst)
                    run.new_hosts[dst.ip] = dst
                    message += f" · adds external host {dst.name}"
                run.new_host_openings.append((opening, dst))
            run.add(line, "create", key, message)


def _import_port_usages(run: _Run, rows: list[tuple[int, dict[str, str]]]) -> None:
    taken = {(u.host_id, u.port, u.protocol, u.service_name) for u in run.snap.usages}
    for line, row in rows:
        label = f"{row.get('host', '')}:{row.get('port', '')} {row.get('service_name', '')}"
        try:
            body = PortUsageCreate(
                host=row.get("host", "") or None,
                port=row.get("port", "") or 0,
                protocol=parse_protocol(row.get("protocol", "")),
                service_name=row.get("service_name", ""),
                description=row.get("description", "")[:255],
                owner=row.get("owner", "")[:255],
            )
        except (ValidationError, ValueError) as exc:
            message = _validation_message(exc) if isinstance(exc, ValidationError) else str(exc)
            run.add(line, "error", label, message)
            continue
        host = run.snap.find_host(body.host or "")
        if host is None:
            run.add(line, "error", label, f"host {body.host!r} is not registered")
            continue
        identity = (host.id, body.port, body.protocol, body.service_name)
        if identity in taken:
            run.add(line, "skip", label, "already recorded")
            continue
        usage = PortUsage(
            host_id=host.id, port=body.port, protocol=body.protocol,
            service_name=body.service_name, description=body.description, owner=body.owner,
        )
        run.stamp(usage)
        run.pending.append(usage)
        taken.add(identity)
        run.add(line, "create", label)


async def run_import(
    db: AsyncSession, body: NetworkImportRequest, *, actor_name: str
) -> NetworkImportResponse:
    try:
        rows = parse_rows(body.kind, body.csv)
    except ImportFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    run = _Run(
        db=db,
        snap=await network_map.load_snapshot(db),
        actor=actor_name,
        dry_run=body.dry_run,
        create_missing_destinations=body.kind == "openings" and body.create_missing_destinations,
    )
    {"hosts": _import_hosts, "openings": _import_openings, "port_usages": _import_port_usages}[
        body.kind
    ](run, rows)
    if not body.dry_run and run.pending:
        try:
            if run.new_hosts:
                db.add_all(run.new_hosts.values())
                await db.flush()
                for opening, host in run.new_host_openings:
                    opening.dst_host_id = host.id
            db.add_all(run.pending)
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail="A concurrent change conflicted with the import; run it again",
            ) from exc
    count = {a: sum(1 for r in run.rows if r.action == a) for a in ("create", "skip", "error")}
    return NetworkImportResponse(
        kind=body.kind,
        dry_run=body.dry_run,
        rows=run.rows,
        created=count["create"],
        skipped=count["skip"],
        errors=count["error"],
    )
