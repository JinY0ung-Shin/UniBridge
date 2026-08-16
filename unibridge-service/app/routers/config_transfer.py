"""Administrative configuration export / import ("config transfer").

Serialises one deployment's admin configuration into a single JSON document and
replays it into another — or back into the same one after an etcd reset, which
drops every custom APISIX route and upstream (see README).

Two rules shape the whole module:

* **Secrets never leave.** Database passwords, S3 credentials, alert-channel
  header values and gateway service-key headers are dropped on export, and an
  import never writes a credential it did not receive: it updates the
  non-secret fields of an existing resource and leaves the stored secret alone.
* **Boot-provisioned state is not transferable.** The fixed APISIX routes and
  upstreams (``PROTECTED_*_IDS``), the ``consumer-restriction`` plugin (rebuilt
  from the API-key table on every boot) and the system roles (reseeded from
  ``ALL_PERMISSIONS``) are excluded from the export and skipped on import.

An import is planned and applied by the same code path: ``dry_run`` computes
every action and every validation error but returns before the first write.
"""
from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    ALL_PERMISSIONS,
    CurrentUser,
    invalidate_permission_cache,
    require_permission,
)
from app.database import get_db
from app.models import (
    AlertChannel,
    AlertSettings,
    DBConnection,
    MonitoredHost,
    MonitoredService,
    NASConnection,
    Permission,
    QueryTemplate,
    ResourceOwner,
    Role,
    RolePermission,
    S3Connection,
)
from app.routers import admin as admin_router
from app.routers import alerts as alerts_router
from app.routers import nas as nas_router
from app.routers import query as query_router
from app.routers import s3 as s3_router
from app.routers import servers as servers_router
from app.schemas import (
    AlertChannelCreate,
    AlertSettingsUpdate,
    ConfigExportResponse,
    ConfigImportRequest,
    ConfigImportResponse,
    ConfigImportResultRow,
    ConfigImportSummary,
    DBConnectionUpdate,
    MonitoredHostCreate,
    MonitoredServiceCreate,
    NasConnectionCreate,
    PermissionCreate,
    QueryTemplateCreate,
    RoleCreate,
    S3ConnectionUpdate,
    SystemConfigUpdate,
)
from app.services import apisix_client, server_monitor
from app.services.apisix_system_resources import (
    PROTECTED_ROUTE_IDS,
    PROTECTED_UPSTREAM_IDS,
)
from app.services.audit import log_admin_action
from app.services.connection_manager import connection_manager
from app.services.nas_manager import nas_manager
from app.services.s3_manager import s3_manager
from app.services.settings_manager import settings_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/config", tags=["Config"])

EXPORT_VERSION = 1

# Import payloads are held in memory and expanded into ORM objects; anything
# larger than this is not a hand-curated config document.
MAX_IMPORT_BYTES = 10 * 1024 * 1024

# Stands in for a secret the export refused to carry. On import it is never
# written: the live value is reused when the target already has one, otherwise
# the field is dropped so a placeholder can't masquerade as a credential.
SECRET_PLACEHOLDER = "<excluded>"

# APISIX maintains these itself; replaying them on PUT is meaningless.
_APISIX_VOLATILE_FIELDS = frozenset({"create_time", "update_time"})

# proxy-rewrite header operations that carry upstream service keys. APISIX
# stores them under operator-chosen header names, so the whole map is secret
# (the same reasoning the audit redactor applies).
_SECRET_HEADER_OPS = ("set", "add")

SECTION_UPSTREAMS = "upstreams"
SECTION_ROUTES = "routes"
SECTION_DB_CONNECTIONS = "db_connections"
SECTION_S3_CONNECTIONS = "s3_connections"
SECTION_NAS_CONNECTIONS = "nas_connections"
SECTION_ROLES = "roles"
SECTION_DB_PERMISSIONS = "db_permissions"
SECTION_QUERY_TEMPLATES = "query_templates"
SECTION_MONITORED_HOSTS = "monitored_hosts"
SECTION_MONITORED_SERVICES = "monitored_services"
SECTION_ALERT_SETTINGS = "alert_settings"
SECTION_ALERT_CHANNELS = "alert_channels"
SECTION_ALERT_RECIPIENTS = "alert_recipients"
SECTION_SYSTEM_SETTINGS = "system_settings"

# Document order — what the UI lists, grouped by screen.
EXPORT_SECTIONS = (
    SECTION_UPSTREAMS,
    SECTION_ROUTES,
    SECTION_DB_CONNECTIONS,
    SECTION_S3_CONNECTIONS,
    SECTION_NAS_CONNECTIONS,
    SECTION_ROLES,
    SECTION_DB_PERMISSIONS,
    SECTION_QUERY_TEMPLATES,
    SECTION_MONITORED_HOSTS,
    SECTION_MONITORED_SERVICES,
    SECTION_ALERT_SETTINGS,
    SECTION_ALERT_CHANNELS,
    SECTION_ALERT_RECIPIENTS,
    SECTION_SYSTEM_SETTINGS,
)

# Apply order — a dependency order, independent of the request's order:
# upstreams before the routes that reference them, roles before the per-DB
# grants that key off them, channels before the alert settings that link one.
IMPORT_ORDER = (
    SECTION_UPSTREAMS,
    SECTION_ROUTES,
    SECTION_ROLES,
    SECTION_DB_PERMISSIONS,
    SECTION_DB_CONNECTIONS,
    SECTION_S3_CONNECTIONS,
    SECTION_NAS_CONNECTIONS,
    SECTION_QUERY_TEMPLATES,
    SECTION_MONITORED_HOSTS,
    SECTION_MONITORED_SERVICES,
    SECTION_ALERT_CHANNELS,
    SECTION_ALERT_SETTINGS,
    SECTION_ALERT_RECIPIENTS,
    SECTION_SYSTEM_SETTINGS,
)

KNOWN_SECTIONS = frozenset(EXPORT_SECTIONS)

# Name shown for a result row that describes a whole section (a singleton
# settings row, or a section the file didn't carry) rather than one item.
_SECTION_ROW = "-"
_SINGLETON_ROW = "global"

EXPORT_NOTES = (
    "secrets are never exported: database passwords, S3 credentials, "
    "alert channel header values and gateway service-key headers are omitted",
    "api keys / consumers are not exported",
    "built-in routes and upstreams are provisioned at boot and are not exported",
    "the consumer-restriction plugin is stripped from exported routes; "
    "it is rebuilt from the API key table on every boot",
    f"excluded secret values are marked {SECRET_PLACEHOLDER!r} where the field is kept",
)


# ── Shared helpers ──────────────────────────────────────────────────────────


def _json_list(raw: str | None) -> list[Any] | None:
    """Decode a JSON-array text column, treating unusable content as unset."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) else None


def _json_object(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_not_found(exc: Exception) -> bool:
    """Whether an APISIX client error means "this resource does not exist"."""
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 404:
        return True
    message = str(exc).lower()
    return "404" in message or "not found" in message


async def _get_apisix_resource(resource: str, resource_id: str) -> dict[str, Any] | None:
    try:
        return await apisix_client.get_resource(resource, resource_id)
    except Exception as exc:
        if _is_not_found(exc):
            return None
        raise


# ── Export ──────────────────────────────────────────────────────────────────


def _export_route(route: dict[str, Any]) -> dict[str, Any]:
    """Strip a route down to what is safe and meaningful to replay.

    ``consumer-restriction`` is dropped because it is derived from the API-key
    table and replayed at boot; service-key header values are replaced with the
    placeholder because they are upstream credentials.
    """
    item = {k: v for k, v in route.items() if k not in _APISIX_VOLATILE_FIELDS}
    plugins = item.get("plugins")
    if not isinstance(plugins, dict):
        return item

    plugins = copy.deepcopy(plugins)
    plugins.pop("consumer-restriction", None)
    proxy_rewrite = plugins.get("proxy-rewrite")
    headers = proxy_rewrite.get("headers") if isinstance(proxy_rewrite, dict) else None
    if isinstance(headers, dict):
        for op in _SECRET_HEADER_OPS:
            values = headers.get(op)
            if isinstance(values, dict) and values:
                headers[op] = {name: SECRET_PLACEHOLDER for name in values}
                item["secrets_excluded"] = True
    item["plugins"] = plugins
    return item


async def _export_gateway() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        upstream_listing = await apisix_client.list_resources("upstreams")
        route_listing = await apisix_client.list_resources("routes")
    except Exception as exc:
        logger.exception("Config export: failed to read gateway configuration")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to read gateway configuration from APISIX: {exc}",
        ) from exc

    upstreams = [
        {k: v for k, v in item.items() if k not in _APISIX_VOLATILE_FIELDS}
        for item in upstream_listing.get("items", [])
        if str(item.get("id")) not in PROTECTED_UPSTREAM_IDS
    ]
    routes = [
        _export_route(item)
        for item in route_listing.get("items", [])
        if str(item.get("id")) not in PROTECTED_ROUTE_IDS
    ]
    return upstreams, routes


async def _export_db_connections(db: AsyncSession) -> list[dict[str, Any]]:
    result = await db.execute(select(DBConnection).order_by(DBConnection.alias))
    return [
        {
            "alias": conn.alias,
            "db_type": conn.db_type,
            "host": conn.host,
            "port": conn.port,
            "database": conn.database,
            "username": conn.username,
            "protocol": conn.protocol,
            "secure": conn.secure,
            "pool_size": conn.pool_size,
            "max_overflow": conn.max_overflow,
            "query_timeout": conn.query_timeout,
            # The password is never exported, so every item is partial.
            "secrets_excluded": True,
        }
        for conn in result.scalars().all()
    ]


async def _export_s3_connections(db: AsyncSession) -> list[dict[str, Any]]:
    result = await db.execute(select(S3Connection).order_by(S3Connection.alias))
    return [
        {
            "alias": conn.alias,
            "endpoint_url": conn.endpoint_url,
            "region": conn.region,
            "default_bucket": conn.default_bucket,
            "allowed_buckets": _json_list(conn.allowed_buckets),
            "use_ssl": conn.use_ssl,
            # Access key id and secret access key are never exported.
            "secrets_excluded": True,
        }
        for conn in result.scalars().all()
    ]


async def _export_nas_connections(db: AsyncSession) -> list[dict[str, Any]]:
    """NAS connections hold no credentials, so they export complete."""
    result = await db.execute(select(NASConnection).order_by(NASConnection.alias))
    return [
        {
            "alias": conn.alias,
            "base_path": conn.base_path,
            "read_only": conn.read_only,
            "max_download_bytes": conn.max_download_bytes,
            "show_hidden": conn.show_hidden,
            "follow_symlinks": conn.follow_symlinks,
        }
        for conn in result.scalars().all()
    ]


async def _export_roles(db: AsyncSession) -> list[dict[str, Any]]:
    result = await db.execute(select(Role).order_by(Role.name))
    roles = list(result.scalars().all())
    grants = await db.execute(select(RolePermission.role_id, RolePermission.permission))
    by_role: dict[int, list[str]] = {}
    for role_id, permission in grants.all():
        by_role.setdefault(role_id, []).append(permission)
    return [
        {
            "name": role.name,
            "description": role.description or "",
            "is_system": bool(role.is_system),
            "permissions": sorted(by_role.get(role.id, [])),
        }
        for role in roles
    ]


async def _export_db_permissions(db: AsyncSession) -> list[dict[str, Any]]:
    result = await db.execute(
        select(Permission).order_by(Permission.role, Permission.db_alias)
    )
    return [
        {
            "role": perm.role,
            "db_alias": perm.db_alias,
            "allow_select": bool(perm.allow_select),
            "allow_insert": bool(perm.allow_insert),
            "allow_update": bool(perm.allow_update),
            "allow_delete": bool(perm.allow_delete),
            "allowed_tables": _json_list(perm.allowed_tables),
        }
        for perm in result.scalars().all()
    ]


async def _export_query_templates(db: AsyncSession) -> list[dict[str, Any]]:
    result = await db.execute(select(QueryTemplate).order_by(QueryTemplate.path))
    return [
        query_router._template_audit_snapshot(template)
        for template in result.scalars().all()
    ]


async def _export_monitored_hosts(db: AsyncSession) -> list[dict[str, Any]]:
    result = await db.execute(select(MonitoredHost).order_by(MonitoredHost.name))
    return [servers_router._audit_snapshot(host) for host in result.scalars().all()]


async def _export_monitored_services(db: AsyncSession) -> list[dict[str, Any]]:
    result = await db.execute(select(MonitoredService).order_by(MonitoredService.name))
    return [
        servers_router._service_audit_snapshot(service)
        for service in result.scalars().all()
    ]


async def _export_alert_channels(db: AsyncSession) -> list[dict[str, Any]]:
    """Channels export with their webhook URL but never their header values.

    Header names are kept (with the placeholder as value) so an operator can see
    which secrets have to be re-entered after an import.
    """
    result = await db.execute(select(AlertChannel).order_by(AlertChannel.name))
    items: list[dict[str, Any]] = []
    for channel in result.scalars().all():
        headers = _json_object(channel.headers)
        item: dict[str, Any] = {
            "name": channel.name,
            "webhook_url": channel.webhook_url,
            "payload_template": channel.payload_template,
            "recipient_item_template": channel.recipient_item_template,
            "headers": {name: SECRET_PLACEHOLDER for name in headers} if headers else None,
            "enabled": bool(channel.enabled),
        }
        if headers:
            item["secrets_excluded"] = True
        items.append(item)
    return items


def _default_alert_settings() -> AlertSettings:
    """A detached settings row carrying the column defaults.

    Used when a deployment has never saved alert settings. Export runs under a
    read-only permission, so it must not take the alerts router's
    get-or-create path — nothing here is added to the session, and the values
    come from the model itself rather than a second copy of the defaults.
    """
    row = AlertSettings()
    for column in AlertSettings.__table__.columns:
        default = column.default
        if default is not None and default.is_scalar:
            setattr(row, column.name, default.arg)
    return row


async def _export_alert_settings(db: AsyncSession) -> dict[str, Any]:
    result = await db.execute(select(AlertSettings).where(AlertSettings.id == 1))
    settings_row = result.scalar_one_or_none() or _default_alert_settings()
    item = alerts_router._settings_audit_snapshot(settings_row)
    # Row ids are not portable across deployments; the name is what re-links
    # the default mail channel on import.
    channel_name = None
    if settings_row.mail_channel_id is not None:
        channel = await db.get(AlertChannel, settings_row.mail_channel_id)
        channel_name = channel.name if channel is not None else None
    item["mail_channel_name"] = channel_name
    return item


async def _export_alert_recipients(db: AsyncSession) -> list[dict[str, Any]]:
    result = await db.execute(
        select(ResourceOwner).order_by(
            ResourceOwner.resource_type, ResourceOwner.resource_id
        )
    )
    return [
        {
            "resource_type": owner.resource_type,
            "resource_id": owner.resource_id,
            "emails": alerts_router._parse_emails(owner.emails),
            "alerts_enabled": bool(owner.alerts_enabled),
        }
        for owner in result.scalars().all()
    ]


@router.get("/export", response_model=ConfigExportResponse)
async def export_config(
    _user: CurrentUser = Depends(require_permission("admin.config.read")),
    db: AsyncSession = Depends(get_db),
) -> ConfigExportResponse:
    """Export the administrative configuration as one portable JSON document."""
    upstreams, routes = await _export_gateway()
    sections = {
        SECTION_UPSTREAMS: upstreams,
        SECTION_ROUTES: routes,
        SECTION_DB_CONNECTIONS: await _export_db_connections(db),
        SECTION_S3_CONNECTIONS: await _export_s3_connections(db),
        SECTION_NAS_CONNECTIONS: await _export_nas_connections(db),
        SECTION_ROLES: await _export_roles(db),
        SECTION_DB_PERMISSIONS: await _export_db_permissions(db),
        SECTION_QUERY_TEMPLATES: await _export_query_templates(db),
        SECTION_MONITORED_HOSTS: await _export_monitored_hosts(db),
        SECTION_MONITORED_SERVICES: await _export_monitored_services(db),
        SECTION_ALERT_SETTINGS: await _export_alert_settings(db),
        SECTION_ALERT_CHANNELS: await _export_alert_channels(db),
        SECTION_ALERT_RECIPIENTS: await _export_alert_recipients(db),
        SECTION_SYSTEM_SETTINGS: settings_manager.get_all(),
    }
    return ConfigExportResponse(
        unibridge_export_version=EXPORT_VERSION,
        exported_at=datetime.now(timezone.utc).isoformat(),
        sections={name: sections[name] for name in EXPORT_SECTIONS},
        excluded={
            "builtin_routes": sorted(PROTECTED_ROUTE_IDS),
            "builtin_upstreams": sorted(PROTECTED_UPSTREAM_IDS),
            "notes": list(EXPORT_NOTES),
        },
    )


# ── Import: plumbing ────────────────────────────────────────────────────────


class _ImportContext:
    """Carries the session, the actor and the growing result list.

    A dry run flows through exactly the same handlers; each one resolves and
    validates first, then returns before its first write when ``dry_run`` is
    set, so the preview reports the same actions and the same errors the apply
    would produce.
    """

    def __init__(self, db: AsyncSession, user: CurrentUser, dry_run: bool) -> None:
        self.db = db
        self.user = user
        self.dry_run = dry_run
        self.results: list[ConfigImportResultRow] = []

    def record(
        self, section: str, name: str, action: str, reason: str | None = None
    ) -> None:
        self.results.append(
            ConfigImportResultRow(
                section=section, name=name, action=action, reason=reason
            )
        )

    def applied(self, section: str) -> bool:
        """Whether a section actually wrote anything (used to trigger reloads)."""
        return any(
            row.section == section and row.action in ("create", "update")
            for row in self.results
        )

    async def audit(
        self,
        *,
        action: str,
        resource_type: str,
        resource_id: str,
        summary: str | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
    ) -> None:
        if self.dry_run:
            return
        await log_admin_action(
            self.db,
            actor=self.user.username,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            summary=summary,
            before=before,
            after=after,
        )


def _error_text(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or 'body'}: {err['msg']}"
            for err in exc.errors()[:5]
        )
    return (str(exc) or exc.__class__.__name__)[:500]


async def _apply_item(
    ctx: _ImportContext,
    section: str,
    name: str,
    coro: Awaitable[tuple[str, str | None]],
) -> None:
    """Run one item's handler, isolating its failure from the rest of the import.

    A handler either commits its own change or raises; on failure the session is
    rolled back so a half-built object can't leak into the next item's commit.
    """
    try:
        action, reason = await coro
    except Exception as exc:  # noqa: BLE001 — per-item isolation is the point
        logger.warning("Config import: %s/%s failed", section, name, exc_info=True)
        try:
            await ctx.db.rollback()
        except Exception:
            logger.exception("Config import: rollback failed after %s/%s", section, name)
        ctx.record(section, name, "error", _error_text(exc))
    else:
        ctx.record(section, name, action, reason)


def _dict_items(ctx: _ImportContext, section: str, raw: Any) -> list[tuple[int, dict]]:
    if not isinstance(raw, list):
        ctx.record(section, _SECTION_ROW, "error", "section must be a list")
        return []
    items: list[tuple[int, dict]] = []
    for index, item in enumerate(raw):
        if isinstance(item, dict):
            items.append((index, item))
        else:
            ctx.record(section, f"#{index}", "error", "item must be an object")
    return items


def _natural_key(
    ctx: _ImportContext, section: str, item: dict[str, Any], field: str, index: int
) -> str | None:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        ctx.record(section, f"#{index}", "error", f"missing {field}")
        return None
    return value.strip()


# ── Import: gateway ─────────────────────────────────────────────────────────


def _apisix_body(item: dict[str, Any]) -> dict[str, Any]:
    """The PUT body for an exported APISIX item: no id (it is in the URL), no
    server-maintained timestamps, no export-only markers."""
    dropped = _APISIX_VOLATILE_FIELDS | {"id", "secrets_excluded"}
    return copy.deepcopy({k: v for k, v in item.items() if k not in dropped})


def _restore_secret_headers(
    plugins: dict[str, Any], existing_plugins: dict[str, Any]
) -> list[str]:
    """Put the live service-key values back behind the exported placeholders.

    Returns the header names whose secret could not be recovered — those are
    dropped rather than written, because a route carrying the placeholder as its
    credential would fail against the upstream in a way nothing reports.
    """
    proxy_rewrite = plugins.get("proxy-rewrite")
    if not isinstance(proxy_rewrite, dict):
        return []
    headers = proxy_rewrite.get("headers")
    if not isinstance(headers, dict):
        return []

    existing_rewrite = existing_plugins.get("proxy-rewrite")
    existing_headers = (
        existing_rewrite.get("headers") if isinstance(existing_rewrite, dict) else None
    )
    existing_headers = existing_headers if isinstance(existing_headers, dict) else {}

    dropped: list[str] = []
    for op in _SECRET_HEADER_OPS:
        values = headers.get(op)
        if not isinstance(values, dict):
            continue
        live = existing_headers.get(op)
        live = live if isinstance(live, dict) else {}
        restored: dict[str, Any] = {}
        for name, value in values.items():
            if value != SECRET_PLACEHOLDER:
                restored[name] = value
            elif name in live:
                restored[name] = live[name]
            else:
                dropped.append(name)
        if restored:
            headers[op] = restored
        else:
            headers.pop(op, None)
    if not headers:
        proxy_rewrite.pop("headers", None)
    return dropped


def _prepare_route_body(
    body: dict[str, Any], existing: dict[str, Any] | None
) -> list[str]:
    """Reconcile an exported route with what the target already has.

    ``consumer-restriction`` is never taken from the file and never removed from
    the target: whatever the live route carries is carried over unchanged, which
    is the same contract ``main.py::_preserve_consumer_restriction`` keeps for
    the built-in routes.
    """
    plugins = body.get("plugins")
    plugins = plugins if isinstance(plugins, dict) else {}
    plugins.pop("consumer-restriction", None)

    existing_plugins = (existing or {}).get("plugins")
    existing_plugins = existing_plugins if isinstance(existing_plugins, dict) else {}
    restriction = existing_plugins.get("consumer-restriction")
    if restriction:
        plugins["consumer-restriction"] = restriction

    dropped = _restore_secret_headers(plugins, existing_plugins)
    if plugins:
        body["plugins"] = plugins
    else:
        body.pop("plugins", None)
    return dropped


async def _apply_upstream(
    ctx: _ImportContext, upstream_id: str, item: dict[str, Any]
) -> tuple[str, str | None]:
    if upstream_id in PROTECTED_UPSTREAM_IDS:
        return "skip", "builtin upstream (auto-provisioned)"

    existing = await _get_apisix_resource("upstreams", upstream_id)
    action = "update" if existing else "create"
    if ctx.dry_run:
        return action, None

    body = _apisix_body(item)
    result = await apisix_client.put_resource("upstreams", upstream_id, body)
    await ctx.audit(
        action=action,
        resource_type="upstream",
        resource_id=upstream_id,
        summary=body.get("name") or upstream_id,
        before=existing,
        after=result,
    )
    return action, None


async def _import_upstreams(ctx: _ImportContext, raw: Any) -> None:
    for index, item in _dict_items(ctx, SECTION_UPSTREAMS, raw):
        upstream_id = _natural_key(ctx, SECTION_UPSTREAMS, item, "id", index)
        if upstream_id is None:
            continue
        await _apply_item(
            ctx,
            SECTION_UPSTREAMS,
            upstream_id,
            _apply_upstream(ctx, upstream_id, item),
        )


async def _apply_route(
    ctx: _ImportContext, route_id: str, item: dict[str, Any]
) -> tuple[str, str | None]:
    if route_id in PROTECTED_ROUTE_IDS:
        return "skip", "builtin route (auto-provisioned)"

    existing = await _get_apisix_resource("routes", route_id)
    action = "update" if existing else "create"
    body = _apisix_body(item)
    dropped = _prepare_route_body(body, existing)
    reason = (
        "service key header value(s) not in export — re-enter in Gateway: "
        + ", ".join(dropped)
        if dropped
        else None
    )
    if ctx.dry_run:
        return action, reason

    result = await apisix_client.put_resource("routes", route_id, body)
    await ctx.audit(
        action=action,
        resource_type="route",
        resource_id=route_id,
        summary=body.get("uri"),
        before=existing,
        after=result,
    )
    return action, reason


async def _import_routes(ctx: _ImportContext, raw: Any) -> None:
    for index, item in _dict_items(ctx, SECTION_ROUTES, raw):
        route_id = _natural_key(ctx, SECTION_ROUTES, item, "id", index)
        if route_id is None:
            continue
        await _apply_item(
            ctx, SECTION_ROUTES, route_id, _apply_route(ctx, route_id, item)
        )


# ── Import: roles and grants ────────────────────────────────────────────────


async def _role_snapshot(db: AsyncSession, role: Role) -> dict[str, Any]:
    result = await db.execute(
        select(RolePermission.permission).where(RolePermission.role_id == role.id)
    )
    return {
        "name": role.name,
        "description": role.description or "",
        "is_system": role.is_system,
        "permissions": sorted(row[0] for row in result.all()),
    }


async def _apply_role(
    ctx: _ImportContext, name: str, item: dict[str, Any]
) -> tuple[str, str | None]:
    body = RoleCreate.model_validate({**item, "name": name})
    result = await ctx.db.execute(select(Role).where(Role.name == name))
    existing = result.scalar_one_or_none()

    if bool(item.get("is_system")) or (existing is not None and existing.is_system):
        return "skip", "system role (reseeded at boot)"
    # Mirrors the roles router: editing the caller's own role through an import
    # would be a privilege-escalation path around that guard.
    if name == ctx.user.role:
        return "skip", "cannot modify your own role"

    # role_permissions is unique on (role_id, permission), so a document that
    # repeats one must not turn into an IntegrityError for the whole role.
    granted = list(
        dict.fromkeys(perm for perm in body.permissions if perm in ALL_PERMISSIONS)
    )
    unknown = sorted(set(body.permissions) - set(ALL_PERMISSIONS))
    reason = f"unknown permissions ignored: {', '.join(unknown)}" if unknown else None
    action = "update" if existing else "create"
    if ctx.dry_run:
        return action, reason

    before = await _role_snapshot(ctx.db, existing) if existing is not None else None
    if existing is None:
        role = Role(name=name, description=body.description, is_system=False)
        ctx.db.add(role)
        await ctx.db.flush()
    else:
        role = existing
        role.description = body.description
        await ctx.db.execute(
            sa_delete(RolePermission).where(RolePermission.role_id == role.id)
        )
    for permission in granted:
        ctx.db.add(RolePermission(role_id=role.id, permission=permission))
    await ctx.db.commit()
    await invalidate_permission_cache()

    await ctx.audit(
        action=action,
        resource_type="role",
        resource_id=str(role.id),
        summary=role.name,
        before=before,
        after=await _role_snapshot(ctx.db, role),
    )
    return action, reason


async def _import_roles(ctx: _ImportContext, raw: Any) -> None:
    for index, item in _dict_items(ctx, SECTION_ROLES, raw):
        name = _natural_key(ctx, SECTION_ROLES, item, "name", index)
        if name is None:
            continue
        await _apply_item(ctx, SECTION_ROLES, name, _apply_role(ctx, name, item))


async def _apply_db_permission(
    ctx: _ImportContext, name: str, item: dict[str, Any]
) -> tuple[str, str | None]:
    body = PermissionCreate.model_validate(item)
    role_result = await ctx.db.execute(select(Role).where(Role.name == body.role))
    if role_result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Role '{body.role}' does not exist",
        )

    result = await ctx.db.execute(
        select(Permission).where(
            Permission.role == body.role, Permission.db_alias == body.db_alias
        )
    )
    perm = result.scalar_one_or_none()
    action = "update" if perm is not None else "create"
    if ctx.dry_run:
        return action, None

    before = admin_router._permission_audit_snapshot(perm) if perm is not None else None
    allowed_tables = (
        json.dumps(body.allowed_tables) if body.allowed_tables is not None else None
    )
    if perm is None:
        perm = Permission(role=body.role, db_alias=body.db_alias)
        ctx.db.add(perm)
    perm.allow_select = body.allow_select
    perm.allow_insert = body.allow_insert
    perm.allow_update = body.allow_update
    perm.allow_delete = body.allow_delete
    perm.allowed_tables = allowed_tables
    await ctx.db.commit()
    await ctx.db.refresh(perm)

    await ctx.audit(
        action=action,
        resource_type="permission",
        resource_id=str(perm.id),
        summary=name,
        before=before,
        after=admin_router._permission_audit_snapshot(perm),
    )
    return action, None


async def _import_db_permissions(ctx: _ImportContext, raw: Any) -> None:
    for index, item in _dict_items(ctx, SECTION_DB_PERMISSIONS, raw):
        role = _natural_key(ctx, SECTION_DB_PERMISSIONS, item, "role", index)
        if role is None:
            continue
        alias = _natural_key(ctx, SECTION_DB_PERMISSIONS, item, "db_alias", index)
        if alias is None:
            continue
        name = f"{role} @ {alias}"
        await _apply_item(
            ctx,
            SECTION_DB_PERMISSIONS,
            name,
            _apply_db_permission(ctx, name, item),
        )


# ── Import: connections ─────────────────────────────────────────────────────

_SECRETS_REQUIRED = "secrets required — create manually first"
_CREDENTIALS_UNCHANGED = "credentials unchanged"

# Credential fields the import must never take from a document, even a
# hand-edited one: secrets are re-entered through the resource's own admin UI.
_DB_SECRET_FIELDS = frozenset({"password"})
_S3_SECRET_FIELDS = frozenset({"access_key_id", "secret_access_key"})


def _update_fields(
    item: dict[str, Any], model: type, secret_fields: frozenset[str]
) -> dict[str, Any]:
    """The item's non-secret fields that ``model`` knows how to validate."""
    return {
        key: value
        for key, value in item.items()
        if key in model.model_fields and key not in secret_fields
    }


async def _apply_db_connection(
    ctx: _ImportContext, alias: str, item: dict[str, Any]
) -> tuple[str, str | None]:
    result = await ctx.db.execute(
        select(DBConnection).where(DBConnection.alias == alias)
    )
    conn = result.scalar_one_or_none()
    if conn is None:
        # The export carries no password and the import refuses to accept one,
        # so a connection can only be created through the normal admin flow.
        return "skip", _SECRETS_REQUIRED

    # db_type is fixed at creation (DBConnectionUpdate has no such field): a
    # document describing a different backend under this alias is not an update.
    file_db_type = item.get("db_type")
    if isinstance(file_db_type, str) and file_db_type != conn.db_type:
        raise HTTPException(
            status_code=422,
            detail=(
                f"db_type mismatch (stored '{conn.db_type}', file '{file_db_type}') "
                "— recreate the connection manually"
            ),
        )

    provided = _update_fields(item, DBConnectionUpdate, _DB_SECRET_FIELDS)
    body = DBConnectionUpdate.model_validate(provided)
    protocol, secure = admin_router._validate_connection_options(
        conn.db_type,
        provided.get("protocol", conn.protocol),
        provided.get("secure", conn.secure),
    )
    if ctx.dry_run:
        return "update", _CREDENTIALS_UNCHANGED

    before = admin_router._connection_audit_snapshot(conn)
    conn.protocol = protocol
    conn.secure = secure
    for field, value in body.model_dump(exclude_unset=True).items():
        if field in ("protocol", "secure") or value is None:
            continue
        setattr(conn, field, value)
    await ctx.db.commit()
    await ctx.db.refresh(conn)

    try:
        await connection_manager.add_connection(conn)
    except Exception as exc:  # noqa: BLE001 — the row is saved either way
        logger.warning("Config import: engine recreation failed for '%s': %s", alias, exc)

    await ctx.audit(
        action="update",
        resource_type="db_connection",
        resource_id=alias,
        summary=f"{conn.db_type} {conn.host}:{conn.port}/{conn.database}",
        before=before,
        after=admin_router._connection_audit_snapshot(conn),
    )
    return "update", _CREDENTIALS_UNCHANGED


async def _import_db_connections(ctx: _ImportContext, raw: Any) -> None:
    for index, item in _dict_items(ctx, SECTION_DB_CONNECTIONS, raw):
        alias = _natural_key(ctx, SECTION_DB_CONNECTIONS, item, "alias", index)
        if alias is None:
            continue
        await _apply_item(
            ctx,
            SECTION_DB_CONNECTIONS,
            alias,
            _apply_db_connection(ctx, alias, item),
        )


async def _apply_s3_connection(
    ctx: _ImportContext, alias: str, item: dict[str, Any]
) -> tuple[str, str | None]:
    result = await ctx.db.execute(
        select(S3Connection).where(S3Connection.alias == alias)
    )
    conn = result.scalar_one_or_none()
    if conn is None:
        return "skip", _SECRETS_REQUIRED

    provided = _update_fields(item, S3ConnectionUpdate, _S3_SECRET_FIELDS)
    body = S3ConnectionUpdate.model_validate(provided)
    # Same pre-check the S3 router runs: validate the post-update combination
    # before touching the ORM object.
    s3_router._check_default_bucket_allowed(
        (body.default_bucket or None)
        if "default_bucket" in provided
        else conn.default_bucket,
        body.allowed_buckets
        if "allowed_buckets" in provided
        else s3_router._stored_allowed_buckets(conn),
    )
    if ctx.dry_run:
        return "update", _CREDENTIALS_UNCHANGED

    before = s3_router._audit_snapshot(conn)
    if "endpoint_url" in provided:
        conn.endpoint_url = body.endpoint_url or None
    if body.region is not None:
        conn.region = body.region
    if "default_bucket" in provided:
        conn.default_bucket = body.default_bucket or None
    if "allowed_buckets" in provided:
        conn.allowed_buckets = (
            json.dumps(body.allowed_buckets) if body.allowed_buckets else None
        )
    if body.use_ssl is not None:
        conn.use_ssl = body.use_ssl
    await ctx.db.commit()
    await ctx.db.refresh(conn)

    try:
        await s3_manager.add_connection(conn)
    except Exception as exc:  # noqa: BLE001 — the row is saved either way
        logger.warning("Config import: S3 client rebuild failed for '%s': %s", alias, exc)

    await ctx.audit(
        action="update",
        resource_type="s3_connection",
        resource_id=alias,
        summary=conn.endpoint_url or conn.region,
        before=before,
        after=s3_router._audit_snapshot(conn),
    )
    return "update", "credentials unchanged"


async def _import_s3_connections(ctx: _ImportContext, raw: Any) -> None:
    for index, item in _dict_items(ctx, SECTION_S3_CONNECTIONS, raw):
        alias = _natural_key(ctx, SECTION_S3_CONNECTIONS, item, "alias", index)
        if alias is None:
            continue
        await _apply_item(
            ctx,
            SECTION_S3_CONNECTIONS,
            alias,
            _apply_s3_connection(ctx, alias, item),
        )


async def _apply_nas_connection(
    ctx: _ImportContext, alias: str, item: dict[str, Any]
) -> tuple[str, str | None]:
    # NAS connections carry no credentials, so they can be created outright —
    # but base_path still has to clear the configured allow-list roots.
    body = NasConnectionCreate.model_validate({**item, "alias": alias, "read_only": True})
    result = await ctx.db.execute(
        select(NASConnection).where(NASConnection.alias == alias)
    )
    conn = result.scalar_one_or_none()
    action = "update" if conn is not None else "create"
    if ctx.dry_run:
        return action, None

    before = nas_router._audit_snapshot(conn) if conn is not None else None
    if conn is None:
        conn = NASConnection(alias=alias, read_only=True)
        ctx.db.add(conn)
    conn.base_path = body.base_path
    conn.max_download_bytes = body.max_download_bytes
    conn.show_hidden = body.show_hidden
    conn.follow_symlinks = body.follow_symlinks
    await ctx.db.commit()
    await ctx.db.refresh(conn)

    try:
        await nas_manager.add_connection(conn)
    except Exception as exc:  # noqa: BLE001 — the row is saved either way
        logger.warning("Config import: NAS registration failed for '%s': %s", alias, exc)

    await ctx.audit(
        action=action,
        resource_type="nas_connection",
        resource_id=alias,
        summary=conn.base_path,
        before=before,
        after=nas_router._audit_snapshot(conn),
    )
    return action, None


async def _import_nas_connections(ctx: _ImportContext, raw: Any) -> None:
    for index, item in _dict_items(ctx, SECTION_NAS_CONNECTIONS, raw):
        alias = _natural_key(ctx, SECTION_NAS_CONNECTIONS, item, "alias", index)
        if alias is None:
            continue
        await _apply_item(
            ctx,
            SECTION_NAS_CONNECTIONS,
            alias,
            _apply_nas_connection(ctx, alias, item),
        )


# ── Import: query templates ─────────────────────────────────────────────────


async def _apply_query_template(
    ctx: _ImportContext, path: str, item: dict[str, Any]
) -> tuple[str, str | None]:
    body = QueryTemplateCreate.model_validate({**item, "path": path})
    # The read-only guarantee is enforced against the target's own connection,
    # so an import can never install a template the admin API would reject.
    connection = await admin_router._get_database_or_404(ctx.db, body.database)
    query_router._validate_read_only_template_sql(body.sql, connection.db_type)

    result = await ctx.db.execute(
        select(QueryTemplate).where(QueryTemplate.path == body.path)
    )
    template = result.scalar_one_or_none()
    action = "update" if template is not None else "create"
    if ctx.dry_run:
        return action, None

    before = (
        query_router._template_audit_snapshot(template) if template is not None else None
    )
    if template is None:
        template = QueryTemplate(path=body.path)
        ctx.db.add(template)
    template.name = body.name
    template.description = body.description
    template.db_alias = body.database
    template.sql = body.sql
    template.default_limit = body.default_limit
    template.timeout = body.timeout
    template.enabled = body.enabled
    await ctx.db.commit()
    await ctx.db.refresh(template)

    await ctx.audit(
        action=action,
        resource_type="query_template",
        resource_id=template.path,
        summary=template.name,
        before=before,
        after=query_router._template_audit_snapshot(template),
    )
    return action, None


async def _import_query_templates(ctx: _ImportContext, raw: Any) -> None:
    for index, item in _dict_items(ctx, SECTION_QUERY_TEMPLATES, raw):
        path = _natural_key(ctx, SECTION_QUERY_TEMPLATES, item, "path", index)
        if path is None:
            continue
        await _apply_item(
            ctx,
            SECTION_QUERY_TEMPLATES,
            path,
            _apply_query_template(ctx, path, item),
        )


# ── Import: monitoring registries ───────────────────────────────────────────


async def _apply_monitored_host(
    ctx: _ImportContext, name: str, item: dict[str, Any]
) -> tuple[str, str | None]:
    body = MonitoredHostCreate.model_validate({**item, "name": name})
    await servers_router._validate_effective_disk_thresholds(
        ctx.db, warn=body.disk_warn_pct, crit=body.disk_crit_pct
    )
    result = await ctx.db.execute(select(MonitoredHost).where(MonitoredHost.name == name))
    host = result.scalar_one_or_none()
    action = "update" if host is not None else "create"
    if ctx.dry_run:
        return action, None

    before = servers_router._audit_snapshot(host) if host is not None else None
    had_gpu = bool(host.gpu_address) if host is not None else False
    if host is None:
        host = MonitoredHost(name=name)
        ctx.db.add(host)
    host.address = body.address
    host.enabled = body.enabled
    host.description = body.description
    host.labels = json.dumps(body.labels, ensure_ascii=False) if body.labels else None
    host.disk_mountpoints = body.disk_mountpoints
    host.gpu_address = body.gpu_address
    host.disk_warn_pct = body.disk_warn_pct
    host.disk_crit_pct = body.disk_crit_pct
    host.cpu_warn_pct = body.cpu_warn_pct
    host.mem_warn_pct = body.mem_warn_pct
    host.gpu_util_warn_pct = body.gpu_util_warn_pct
    host.gpu_mem_warn_pct = body.gpu_mem_warn_pct
    await ctx.db.commit()
    await ctx.db.refresh(host)

    # Same cleanup the servers router does: a host that stops being scraped must
    # not linger as a stale "down".
    if not host.enabled:
        await servers_router._clear_host_alert_state(ctx.db, host.name)
        await ctx.db.commit()
    elif had_gpu and not host.gpu_address:
        await servers_router._clear_gpu_alert_state(ctx.db, host.name)
        await ctx.db.commit()

    await ctx.audit(
        action=action,
        resource_type="monitored_host",
        resource_id=host.name,
        summary=host.address,
        before=before,
        after=servers_router._audit_snapshot(host),
    )
    return action, None


async def _import_monitored_hosts(ctx: _ImportContext, raw: Any) -> None:
    for index, item in _dict_items(ctx, SECTION_MONITORED_HOSTS, raw):
        name = _natural_key(ctx, SECTION_MONITORED_HOSTS, item, "name", index)
        if name is None:
            continue
        await _apply_item(
            ctx,
            SECTION_MONITORED_HOSTS,
            name,
            _apply_monitored_host(ctx, name, item),
        )
    if not ctx.dry_run and ctx.applied(SECTION_MONITORED_HOSTS):
        await server_monitor.sync_targets_from_db(ctx.db)


async def _apply_monitored_service(
    ctx: _ImportContext, name: str, item: dict[str, Any]
) -> tuple[str, str | None]:
    body = MonitoredServiceCreate.model_validate({**item, "name": name})
    result = await ctx.db.execute(
        select(MonitoredService).where(MonitoredService.name == name)
    )
    service = result.scalar_one_or_none()
    action = "update" if service is not None else "create"
    if ctx.dry_run:
        return action, None

    before = (
        servers_router._service_audit_snapshot(service) if service is not None else None
    )
    if service is None:
        service = MonitoredService(name=name)
        ctx.db.add(service)
    service.address = body.address
    service.metrics_path = body.metrics_path
    service.scheme = body.scheme
    service.description = body.description
    service.enabled = body.enabled
    await ctx.db.commit()
    await ctx.db.refresh(service)

    if not service.enabled:
        await servers_router._clear_service_alert_state(ctx.db, service.name)
        await ctx.db.commit()

    await ctx.audit(
        action=action,
        resource_type="monitored_service",
        resource_id=service.name,
        summary=service.address,
        before=before,
        after=servers_router._service_audit_snapshot(service),
    )
    return action, None


async def _import_monitored_services(ctx: _ImportContext, raw: Any) -> None:
    for index, item in _dict_items(ctx, SECTION_MONITORED_SERVICES, raw):
        name = _natural_key(ctx, SECTION_MONITORED_SERVICES, item, "name", index)
        if name is None:
            continue
        await _apply_item(
            ctx,
            SECTION_MONITORED_SERVICES,
            name,
            _apply_monitored_service(ctx, name, item),
        )
    if not ctx.dry_run and ctx.applied(SECTION_MONITORED_SERVICES):
        await server_monitor.sync_service_targets_from_db(ctx.db)


# ── Import: alerting ────────────────────────────────────────────────────────


def _channel_headers(
    item: dict[str, Any], existing: AlertChannel | None
) -> tuple[dict[str, str] | None, list[str]]:
    """Resolve exported header placeholders against the channel's live values."""
    headers = item.get("headers")
    if not isinstance(headers, dict):
        return None, []
    live = _json_object(existing.headers) if existing is not None else None
    live = live or {}
    resolved: dict[str, str] = {}
    dropped: list[str] = []
    for name, value in headers.items():
        if value != SECRET_PLACEHOLDER:
            resolved[str(name)] = str(value)
        elif name in live:
            resolved[str(name)] = str(live[name])
        else:
            dropped.append(str(name))
    return (resolved or None), dropped


async def _apply_alert_channel(
    ctx: _ImportContext, name: str, item: dict[str, Any]
) -> tuple[str, str | None]:
    result = await ctx.db.execute(select(AlertChannel).where(AlertChannel.name == name))
    channel = result.scalar_one_or_none()
    headers, dropped = _channel_headers(item, channel)
    # Validates the webhook URL through the same SSRF guard the channel API uses.
    body = AlertChannelCreate.model_validate(
        {**item, "name": name, "headers": headers}
    )
    action = "update" if channel is not None else "create"
    reason = (
        "header value(s) not in export — re-enter in Alert channels: "
        + ", ".join(dropped)
        if dropped
        else None
    )
    if ctx.dry_run:
        return action, reason

    before = alerts_router._channel_audit_snapshot(channel) if channel is not None else None
    if channel is None:
        channel = AlertChannel(name=name)
        ctx.db.add(channel)
    channel.webhook_url = body.webhook_url
    channel.payload_template = body.payload_template
    channel.recipient_item_template = body.recipient_item_template
    channel.headers = json.dumps(body.headers) if body.headers else None
    channel.enabled = body.enabled
    await ctx.db.commit()
    await ctx.db.refresh(channel)

    await ctx.audit(
        action=action,
        resource_type="alert_channel",
        resource_id=str(channel.id),
        summary=channel.name,
        before=before,
        after=alerts_router._channel_audit_snapshot(channel),
    )
    return action, reason


async def _import_alert_channels(ctx: _ImportContext, raw: Any) -> None:
    for index, item in _dict_items(ctx, SECTION_ALERT_CHANNELS, raw):
        name = _natural_key(ctx, SECTION_ALERT_CHANNELS, item, "name", index)
        if name is None:
            continue
        await _apply_item(
            ctx,
            SECTION_ALERT_CHANNELS,
            name,
            _apply_alert_channel(ctx, name, item),
        )


async def _apply_alert_settings(
    ctx: _ImportContext, item: dict[str, Any]
) -> tuple[str, str | None]:
    fields = {
        key: value
        for key, value in item.items()
        if key in AlertSettingsUpdate.model_fields
        and key != "mail_channel_id"
        and value is not None
    }
    body = AlertSettingsUpdate.model_validate(fields)

    # Channel ids are per-deployment; the name is what makes the link portable.
    channel_name = item.get("mail_channel_name")
    reason: str | None = None
    resolved_channel_id: int | None = None
    relink = False
    if isinstance(channel_name, str) and channel_name:
        result = await ctx.db.execute(
            select(AlertChannel).where(AlertChannel.name == channel_name)
        )
        channel = result.scalar_one_or_none()
        if channel is None:
            reason = f"mail channel '{channel_name}' not found — link left unchanged"
        else:
            resolved_channel_id = channel.id
            relink = True
    elif "mail_channel_id" in item and item.get("mail_channel_id") is None:
        relink = True  # explicitly "no default mail channel"
    elif item.get("mail_channel_id") is not None:
        reason = "mail_channel_name missing — link left unchanged"

    if ctx.dry_run:
        return "update", reason

    settings_row = await alerts_router._get_or_create_alert_settings(ctx.db, commit=False)
    before = alerts_router._settings_audit_snapshot(settings_row)
    if relink:
        settings_row.mail_channel_id = resolved_channel_id
    for field in body.model_fields_set:
        value = getattr(body, field)
        if field == "admin_emails":
            settings_row.admin_emails = json.dumps(value, ensure_ascii=False)
        else:
            setattr(settings_row, field, value)
    alerts_router._validate_settings_disk_thresholds(settings_row)
    await ctx.db.commit()
    await ctx.db.refresh(settings_row)

    await ctx.audit(
        action="update",
        resource_type="alert_settings",
        resource_id=_SINGLETON_ROW,
        before=before,
        after=alerts_router._settings_audit_snapshot(settings_row),
    )
    return "update", reason


async def _import_alert_settings(ctx: _ImportContext, raw: Any) -> None:
    if not isinstance(raw, dict):
        ctx.record(SECTION_ALERT_SETTINGS, _SECTION_ROW, "error", "section must be an object")
        return
    await _apply_item(
        ctx,
        SECTION_ALERT_SETTINGS,
        _SINGLETON_ROW,
        _apply_alert_settings(ctx, raw),
    )


async def _apply_alert_recipient(
    ctx: _ImportContext, name: str, resource_type: str, resource_id: str, item: dict[str, Any]
) -> tuple[str, str | None]:
    alerts_router._validate_resource_type(resource_type)
    emails = item.get("emails") or []
    if not isinstance(emails, list) or not all(isinstance(e, str) for e in emails):
        raise HTTPException(status_code=422, detail="emails must be a list of strings")
    alerts_enabled = item.get("alerts_enabled")
    alerts_enabled = True if alerts_enabled is None else bool(alerts_enabled)

    result = await ctx.db.execute(
        select(ResourceOwner).where(
            ResourceOwner.resource_type == resource_type,
            ResourceOwner.resource_id == resource_id,
        )
    )
    owner = result.scalar_one_or_none()
    action = "update" if owner is not None else "create"
    if ctx.dry_run:
        return action, None

    before = alerts_router._resource_owner_snapshot(owner)
    if owner is None:
        owner = ResourceOwner(resource_type=resource_type, resource_id=resource_id)
        ctx.db.add(owner)
    owner.emails = json.dumps(emails, ensure_ascii=False)
    owner.alerts_enabled = alerts_enabled
    await ctx.db.commit()
    await ctx.db.refresh(owner)

    await ctx.audit(
        action=action,
        resource_type="resource_owner",
        resource_id=name,
        summary=None,
        before=before,
        after={"emails": emails, "alerts_enabled": alerts_enabled},
    )
    return action, None


async def _import_alert_recipients(ctx: _ImportContext, raw: Any) -> None:
    for index, item in _dict_items(ctx, SECTION_ALERT_RECIPIENTS, raw):
        resource_type = _natural_key(
            ctx, SECTION_ALERT_RECIPIENTS, item, "resource_type", index
        )
        if resource_type is None:
            continue
        resource_id = _natural_key(
            ctx, SECTION_ALERT_RECIPIENTS, item, "resource_id", index
        )
        if resource_id is None:
            continue
        name = f"{resource_type}/{resource_id}"
        await _apply_item(
            ctx,
            SECTION_ALERT_RECIPIENTS,
            name,
            _apply_alert_recipient(ctx, name, resource_type, resource_id, item),
        )


# ── Import: system settings ─────────────────────────────────────────────────


async def _apply_system_settings(
    ctx: _ImportContext, item: dict[str, Any]
) -> tuple[str, str | None]:
    known = {
        key: value
        for key, value in item.items()
        if key in SystemConfigUpdate.model_fields and value is not None
    }
    body = SystemConfigUpdate.model_validate(known)
    ignored = sorted(set(item) - set(SystemConfigUpdate.model_fields))
    reason = f"unknown settings ignored: {', '.join(ignored)}" if ignored else None
    if ctx.dry_run:
        return "update", reason

    from app.middleware.rate_limiter import rate_limiter

    before = settings_manager.get_all()
    await settings_manager.update(ctx.db, **body.model_dump(exclude_none=True))
    rate_limiter.update_limits(
        rate_limit=settings_manager.rate_limit_per_minute,
        max_concurrent=settings_manager.max_concurrent_queries,
    )
    after = settings_manager.get_all()

    await ctx.audit(
        action="update",
        resource_type="system_settings",
        resource_id=_SINGLETON_ROW,
        before=before,
        after=after,
    )
    return "update", reason


async def _import_system_settings(ctx: _ImportContext, raw: Any) -> None:
    if not isinstance(raw, dict):
        ctx.record(SECTION_SYSTEM_SETTINGS, _SECTION_ROW, "error", "section must be an object")
        return
    await _apply_item(
        ctx,
        SECTION_SYSTEM_SETTINGS,
        _SINGLETON_ROW,
        _apply_system_settings(ctx, raw),
    )


_SECTION_IMPORTERS = {
    SECTION_UPSTREAMS: _import_upstreams,
    SECTION_ROUTES: _import_routes,
    SECTION_ROLES: _import_roles,
    SECTION_DB_PERMISSIONS: _import_db_permissions,
    SECTION_DB_CONNECTIONS: _import_db_connections,
    SECTION_S3_CONNECTIONS: _import_s3_connections,
    SECTION_NAS_CONNECTIONS: _import_nas_connections,
    SECTION_QUERY_TEMPLATES: _import_query_templates,
    SECTION_MONITORED_HOSTS: _import_monitored_hosts,
    SECTION_MONITORED_SERVICES: _import_monitored_services,
    SECTION_ALERT_CHANNELS: _import_alert_channels,
    SECTION_ALERT_SETTINGS: _import_alert_settings,
    SECTION_ALERT_RECIPIENTS: _import_alert_recipients,
    SECTION_SYSTEM_SETTINGS: _import_system_settings,
}


# ── Import endpoint ─────────────────────────────────────────────────────────


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def _too_large() -> HTTPException:
    return _unprocessable(
        f"Import document exceeds the {MAX_IMPORT_BYTES // (1024 * 1024)}MB limit"
    )


async def _read_body_within_limit(request: Request) -> bytes:
    """Buffer the request body, aborting as soon as it passes the cap.

    A declared Content-Length is rejected outright; a chunked upload that
    declares nothing is measured as it arrives, so an oversized document never
    gets fully accumulated in memory.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_IMPORT_BYTES:
        raise _too_large()

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_IMPORT_BYTES:
            raise _too_large()
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_import_request(request: Request) -> ConfigImportRequest:
    """Parse the request body, rejecting anything oversized before decoding it."""
    raw = await _read_body_within_limit(request)
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise _unprocessable(f"Request body is not valid JSON: {exc}") from exc
    try:
        return ConfigImportRequest.model_validate(payload)
    except ValidationError as exc:
        raise _unprocessable(_error_text(exc)) from exc


@router.post(
    "/import",
    response_model=ConfigImportResponse,
    # The body is read and validated by hand so an oversized document is
    # rejected before it is decoded; declare its schema for the docs.
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": ConfigImportRequest.model_json_schema()}
            },
        }
    },
)
async def import_config(
    request: Request,
    user: CurrentUser = Depends(require_permission("admin.config.write")),
    db: AsyncSession = Depends(get_db),
) -> ConfigImportResponse:
    """Apply an export document, or preview the plan when ``dry_run`` is set."""
    body = await _read_import_request(request)

    if body.data.get("unibridge_export_version") != EXPORT_VERSION:
        raise _unprocessable(
            f"Unsupported export version (expected {EXPORT_VERSION})"
        )
    file_sections = body.data.get("sections")
    if not isinstance(file_sections, dict):
        raise _unprocessable("data.sections is missing or not an object")
    unknown = [name for name in body.sections if name not in KNOWN_SECTIONS]
    if unknown:
        raise _unprocessable(f"Unknown section(s): {', '.join(sorted(set(unknown)))}")

    requested = set(body.sections)
    ctx = _ImportContext(db, user, body.dry_run)
    for section in IMPORT_ORDER:
        if section not in requested:
            continue
        if section not in file_sections:
            ctx.record(section, _SECTION_ROW, "skip", "section not in file")
            continue
        await _SECTION_IMPORTERS[section](ctx, file_sections[section])

    summary = ConfigImportSummary()
    for row in ctx.results:
        if row.action in ConfigImportSummary.model_fields:
            setattr(summary, row.action, getattr(summary, row.action) + 1)

    if not body.dry_run:
        await log_admin_action(
            db,
            actor=user.username,
            action="import",
            resource_type="config_import",
            resource_id=_SINGLETON_ROW,
            summary=(
                f"create={summary.create} update={summary.update} "
                f"skip={summary.skip} error={summary.error}"
            ),
            before=None,
            after={
                "sections": sorted(requested),
                "summary": summary.model_dump(),
            },
        )
    logger.info(
        "Config import (%s) by %s: %s",
        "dry-run" if body.dry_run else "applied",
        user.username,
        summary.model_dump(),
    )
    return ConfigImportResponse(
        dry_run=body.dry_run, results=ctx.results, summary=summary
    )
