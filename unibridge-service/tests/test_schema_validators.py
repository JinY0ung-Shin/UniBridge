"""Field validation in app/schemas.py: what each request model accepts, how it
normalizes the value and the exact error a client gets back (422 detail).

The shared field types (Annotated + AfterValidator) must keep the order of the
old per-model validators: length limits on the raw value, then the helper."""
import pytest
from pydantic import ValidationError

from app import schemas as S


def _errors(model, **data):
    with pytest.raises(ValidationError) as exc_info:
        model(**data)
    return [(tuple(e["loc"]), e["type"], e["msg"]) for e in exc_info.value.errors()]


# ── Query templates ─────────────────────────────────────────────────────────

_TEMPLATE = {"path": "a/b", "name": "n", "database": "db", "sql": "select 1"}


def test_template_create_strips_text_and_normalizes_path():
    t = S.QueryTemplateCreate(
        path=" /team/q1/ ", name=" N ", description=" d ", database=" db ", sql=" select 1 "
    )
    assert (t.path, t.name, t.description, t.database, t.sql) == (
        "team/q1", "N", "d", "db", "select 1"
    )


@pytest.mark.parametrize("field", ["name", "database", "sql"])
def test_template_create_rejects_blank_required_text(field):
    assert _errors(S.QueryTemplateCreate, **{**_TEMPLATE, field: "   "}) == [
        ((), "value_error", f"Value error, {field} must not be empty")
    ]


def test_template_create_length_limits_apply_to_the_raw_value():
    # 101 characters with the padding, 99 without: the limit sees the padding.
    assert _errors(S.QueryTemplateCreate, **{**_TEMPLATE, "name": " " + "x" * 99 + " "}) == [
        (("name",), "string_too_long", "String should have at most 100 characters")
    ]
    assert _errors(S.QueryTemplateCreate, **{**_TEMPLATE, "name": ""}) == [
        (("name",), "string_too_short", "String should have at least 1 character")
    ]


def test_template_create_path_errors():
    assert _errors(S.QueryTemplateCreate, **{**_TEMPLATE, "path": "a/../b"}) == [
        (("path",), "value_error",
         "Value error, Template path must not contain empty, '.', or '..' segments")
    ]
    assert _errors(S.QueryTemplateCreate, **{**_TEMPLATE, "path": "   "}) == [
        (("path",), "value_error", "Value error, Template path must not be empty")
    ]


def test_template_update_blank_and_none():
    assert _errors(S.QueryTemplateUpdate, name="   ") == [
        ((), "value_error", "Value error, name must not be empty")
    ]
    t = S.QueryTemplateUpdate(name=None, description="   ", sql=" s ")
    assert (t.name, t.description, t.sql) == (None, "", "s")
    assert t.model_fields_set == {"name", "description", "sql"}


def test_agent_template_strips_and_rejects_blank():
    t = S.QueryTemplateAgentCreate(name=" n ", database=" db ", sql=" select 1 ", description=" d ")
    assert (t.name, t.database, t.sql, t.description) == ("n", "db", "select 1", "d")
    assert _errors(S.QueryTemplateAgentCreate, name="n", database="db", sql="  ") == [
        ((), "value_error", "Value error, sql must not be empty")
    ]
    assert S.QueryTemplateAgentUpdate(description="  x ").description == "x"
    assert _errors(S.QueryTemplateAgentUpdate, sql="   ") == [
        ((), "value_error", "Value error, sql must not be empty")
    ]
    assert _errors(S.QueryTemplateAgentUpdate, description=None) == [
        ((), "value_error",
         "Value error, description must not be null; use an empty string to clear it")
    ]


@pytest.mark.parametrize(
    "model, base",
    [
        (S.QueryTemplateCreate, _TEMPLATE),
        (S.QueryTemplateUpdate, {}),
        (S.QueryTemplateAgentCreate, {"name": "n", "database": "db", "sql": "select 1"}),
        (S.QueryTemplateAgentUpdate, {"sql": "select 1"}),
    ],
)
def test_sample_params_checks_every_template_model(model, base):
    # Keys and values are kept as given (no stripping of a dict).
    assert model(**base, sample_params={" k ": " v "}).sample_params == {" k ": " v "}
    assert model(**base, sample_params=None).sample_params is None
    assert _errors(model, **base, sample_params={"a": "x" * 20_000}) == [
        (("sample_params",), "value_error",
         "Value error, sample_params must be 16384 bytes or fewer as JSON")
    ]


def test_saved_query_and_exposed_route_strip():
    assert S.SavedQueryCreate(name=" q ", sql_text=" s ", description=" d ").model_dump() == {
        "name": "q", "database_alias": None, "sql_text": " s ", "description": "d"
    }
    route = S.McpExposedRoute(route_id=" r1 ", description=" shown ")
    assert (route.route_id, route.description) == ("r1", "shown")
    assert _errors(S.McpExposedRoute, route_id=" " + "x" * 63 + " ") == [
        (("route_id",), "string_too_long", "String should have at most 64 characters")
    ]


def test_mcp_remote_header_name():
    assert S.McpRemoteHeader(name=" X-Team ").name == "X-Team"
    assert _errors(S.McpRemoteHeader, name="Host") == [
        (("name",), "value_error",
         "Value error, header 'Host' is set by UniBridge and cannot be configured")
    ]


# ── Alerts ──────────────────────────────────────────────────────────────────

def test_alert_channel_webhook_url():
    assert _errors(
        S.AlertChannelCreate, name="c", webhook_url="ftp://x", payload_template="{}"
    ) == [(("webhook_url",), "value_error", "Value error, webhook_url must use http or https scheme")]
    assert S.AlertChannelUpdate(webhook_url=None).webhook_url is None
    assert _errors(S.AlertChannelUpdate, webhook_url="ftp://x") == [
        (("webhook_url",), "value_error", "Value error, webhook_url must use http or https scheme")
    ]
    assert _errors(S.AlertChannelUpdate, webhook_url="") == [
        (("webhook_url",), "string_too_short", "String should have at least 1 character")
    ]


def test_email_lists_are_deduped():
    raw = [" a@x ", "a@x", "", "b@x"]
    assert S.AlertSettingsUpdate(admin_emails=raw).admin_emails == ["a@x", "b@x"]
    assert S.AlertSettingsUpdate(admin_emails=None).admin_emails is None
    assert S.ResourceOwnerUpsert(emails=raw).emails == ["a@x", "b@x"]
    assert S.ResourceOwnerUpsert(emails=["  "]).emails == []
    assert S.ResourceOwnerUpsert(emails=None).emails is None
    assert _errors(S.RecipientTestRequest, mail_channel_id=1, emails=["  "]) == [
        (("emails",), "value_error", "Value error, emails must include at least one address")
    ]


# ── Monitored servers and services ──────────────────────────────────────────

def test_monitored_host_fields():
    host = S.MonitoredHostCreate(
        name=" h1 ",
        address=" 10.0.0.5:9100 ",
        labels={"env": "prod"},
        gpu_address="   ",
        disk_mountpoints=" / , /data ,/data/./x,/data",
    )
    assert (host.name, host.address, host.labels, host.gpu_address, host.disk_mountpoints) == (
        "h1", "10.0.0.5:9100", {"env": "prod"}, None, "/,/data,/data/x"
    )
    assert _errors(
        S.MonitoredHostCreate, name="-bad", address="http://x:1", labels={"job": "x"},
        gpu_address="nope", disk_mountpoints="relative",
    ) == [
        (("name",), "value_error",
         "Value error, name must start alphanumeric and contain only letters, digits, '.', '_', '-'"),
        (("address",), "value_error",
         "Value error, address must be host:port without a scheme "
         "(e.g. 10.0.0.5:39100, not http://...)"),
        (("labels",), "value_error", "Value error, label name 'job' is reserved for monitoring"),
        (("disk_mountpoints",), "value_error",
         "Value error, disk_mountpoints entries must be absolute POSIX paths"),
        (("gpu_address",), "value_error",
         "Value error, address must be in host:port form (e.g. 10.0.0.5:39100)"),
    ]


def test_monitored_host_update_none_and_order():
    upd = S.MonitoredHostUpdate(address=None, labels=None, gpu_address=None, disk_mountpoints=None)
    assert upd.model_dump(exclude_unset=True) == {
        "address": None, "labels": None, "gpu_address": None, "disk_mountpoints": None
    }
    assert _errors(S.MonitoredHostUpdate, address="x:0") == [
        (("address",), "value_error",
         "Value error, address port must be a number between 1 and 65535")
    ]
    # max_length sees the raw value: 256 characters fail even though the
    # helper would have collapsed the blank string to None.
    assert _errors(S.MonitoredHostUpdate, gpu_address=" " * 256) == [
        (("gpu_address",), "string_too_long", "String should have at most 255 characters")
    ]
    assert _errors(S.MonitoredHostUpdate, disk_warn_pct=90, disk_crit_pct=80) == [
        ((), "value_error", "Value error, disk_warn_pct must be less than or equal to disk_crit_pct")
    ]


def test_monitored_service_fields():
    svc = S.MonitoredServiceCreate(
        name=" s1 ", address="10.0.0.5:9100", metrics_path=" /metrics ", scheme=" HTTPS "
    )
    assert (svc.name, svc.metrics_path, svc.scheme) == ("s1", "/metrics", "https")
    assert _errors(
        S.MonitoredServiceCreate, name="s", address="x:1", metrics_path="metrics", scheme="ftp"
    ) == [
        (("metrics_path",), "value_error",
         "Value error, metrics_path must start with '/' (e.g. /metrics or /actuator/prometheus)"),
        (("scheme",), "value_error", "Value error, scheme must be 'http' or 'https'"),
    ]
    upd = S.MonitoredServiceUpdate(name=None, scheme=" HTTP ")
    assert (upd.name, upd.scheme) == (None, "http")
    assert _errors(S.MonitoredServiceUpdate, name="   ") == [
        (("name",), "value_error",
         "Value error, name must start alphanumeric and contain only letters, digits, '.', '_', '-'")
    ]


# ── S3 / NAS ────────────────────────────────────────────────────────────────

_S3 = {"alias": "s", "access_key_id": "k", "secret_access_key": "s"}


@pytest.mark.parametrize("model, base", [(S.S3ConnectionCreate, _S3), (S.S3ConnectionUpdate, {})])
def test_s3_endpoint_and_buckets(model, base):
    assert model(**base, endpoint_url="   ").endpoint_url is None
    assert model(**base, endpoint_url=None).endpoint_url is None
    assert model(**base, endpoint_url="http://minio:9000").endpoint_url == "http://minio:9000"
    assert _errors(model, **base, endpoint_url="ftp://x") == [
        (("endpoint_url",), "value_error", "Value error, endpoint_url must use http or https scheme")
    ]
    assert model(**base, allowed_buckets=[" a ", "a", "", "b"]).allowed_buckets == ["a", "b"]
    assert model(**base, allowed_buckets=["  "]).allowed_buckets is None
    assert _errors(model, **base, allowed_buckets=["b"] * 101) == [
        (("allowed_buckets",), "value_error",
         "Value error, allowed_buckets must not contain more than 100 entries")
    ]


def test_nas_base_path(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "NAS_ALLOWED_ROOTS", "/mnt/nas")
    assert S.NasConnectionCreate(alias="n", base_path="/mnt/nas/./a/").base_path == "/mnt/nas/a"
    assert S.NasConnectionUpdate(base_path=None).base_path is None
    assert _errors(S.NasConnectionUpdate, base_path="/etc") == [
        (("base_path",), "value_error", "Value error, base_path is not under an allowed NAS root")
    ]
    assert _errors(S.NasConnectionCreate, alias="n", base_path="/mnt/nas", read_only=False) == [
        (("read_only",), "value_error",
         "Value error, NAS connections are read-only; read_only must be true")
    ]


# ── Network map ─────────────────────────────────────────────────────────────

def test_region_fields():
    region = S.RegionCreate(name=" r1 ", cidrs=["10.0.0.1/24", "10.0.0.0/24", " 10.1.0.0/16 "])
    assert (region.name, region.cidrs) == ("r1", ["10.0.0.0/24", "10.1.0.0/16"])
    assert S.RegionCreate(name="r", cidrs=[]).cidrs == []
    assert _errors(S.RegionCreate, name="a/b", cidrs=["bad"]) == [
        (("name",), "value_error",
         "Value error, region name must be 1-64 characters without control characters or slashes"),
        (("cidrs",), "value_error", "Value error, invalid CIDR: 'bad'"),
    ]
    assert S.RegionUpdate(name=None, cidrs=None).model_dump() == {
        "name": None, "description": None, "cidrs": None
    }


def test_network_host_fields():
    host = S.NetworkHostCreate(name=" web1 ", ip=" 10.0.0.1 ")
    assert (host.name, host.ip) == ("web1", "10.0.0.1")
    assert _errors(S.NetworkHostCreate, name="10.0.0.1", ip="bad") == [
        (("name",), "value_error",
         "Value error, a host name must not be an IP address (put the address in ip)"),
        (("ip",), "value_error", "Value error, ip must be an IPv4 or IPv6 address, got 'bad'"),
    ]
    upd = S.NetworkHostUpdate(name=None, ip=None)
    assert (upd.name, upd.ip) == (None, None)
    assert _errors(S.NetworkHostUpdate, ip="  ") == [
        (("ip",), "value_error", "Value error, ip must be an IPv4 or IPv6 address, got '  '")
    ]


def test_firewall_opening_src_cidr():
    opening = S.FirewallOpeningCreate(src_cidr=" 10.0.0.1/8 ", dst_host="b", port_from=80)
    assert (opening.src_cidr, opening.port_to) == ("10.0.0.0/8", 80)
    assert _errors(S.FirewallOpeningUpdate, src_cidr="bad") == [
        (("src_cidr",), "value_error", "Value error, invalid CIDR: 'bad'")
    ]
    assert S.FirewallOpeningUpdate(src_cidr=None).src_cidr is None


def test_port_usage_service_name():
    assert S.PortUsageCreate(host="h", port=80, service_name=" svc ").service_name == "svc"
    assert S.PortUsageUpdate(service_name=" svc ").service_name == "svc"
    assert S.PortUsageUpdate(service_name=None).service_name is None
    for model, base in ((S.PortUsageCreate, {"host": "h", "port": 80}), (S.PortUsageUpdate, {})):
        assert _errors(model, **base, service_name="   ") == [
            (("service_name",), "value_error", "Value error, service_name must not be blank")
        ]
        assert _errors(model, **base, service_name=" " + "x" * 100) == [
            (("service_name",), "string_too_long", "String should have at most 100 characters")
        ]
