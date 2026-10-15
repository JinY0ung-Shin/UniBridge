from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
BLUEGREEN_INFRA_COMPOSE_FILE = REPO_ROOT / "docker-compose.infra.yml"
BLUEGREEN_APP_COMPOSE_FILE = REPO_ROOT / "docker-compose.app.yml"
BLUEGREEN_EDGE_COMPOSE_FILE = REPO_ROOT / "docker-compose.edge.yml"
ENV_EXAMPLE_FILE = REPO_ROOT / ".env.example"
REALM_EXPORT_FILE = REPO_ROOT / "keycloak" / "realm-export.json"
PROMETHEUS_CONFIG_FILE = REPO_ROOT / "prometheus" / "prometheus.yml"
PROMETHEUS_RULES_DIR = REPO_ROOT / "prometheus" / "rules"
NGINX_CONFIG_FILE = REPO_ROOT / "unibridge-ui" / "nginx.conf"
EDGE_TEMPLATE_FILE = REPO_ROOT / "deploy" / "edge" / "default.conf.template"
EDGE_BIFROST_TEMPLATE_FILE = REPO_ROOT / "deploy" / "edge" / "bifrost-ui.conf.template"
DEPLOY_SCRIPT_FILE = REPO_ROOT / "scripts" / "deploy-bluegreen.sh"
BIFROST_DIR = REPO_ROOT / "bifrost"
BIFROST_TEST_SCRIPT_FILE = REPO_ROOT / "scripts" / "bifrost-test.sh"
UI_ENTRYPOINT_FILE = REPO_ROOT / "unibridge-ui" / "entrypoint.sh"
UI_DOCKERIGNORE_FILE = REPO_ROOT / "unibridge-ui" / ".dockerignore"
UI_DOCKERFILE = REPO_ROOT / "unibridge-ui" / "Dockerfile"
UI_BIFROST_SERVER_FILE = REPO_ROOT / "unibridge-ui" / "nginx-bifrost-ui.conf"
BACKUP_SCRIPT_FILE = REPO_ROOT / "backup" / "backup.sh"
RESTORE_SCRIPT_FILE = REPO_ROOT / "backup" / "restore.sh"
BACKUP_META_LIB_FILE = REPO_ROOT / "backup" / "lib" / "meta.sh"
BACKUP_POSTGRES_LIB_FILE = REPO_ROOT / "backup" / "lib" / "postgres.sh"

COMPOSE_SERVICE_LIMITS = {
    "etcd": {"memory": "256m", "cpus": "0.50"},
    "apisix": {"memory": "512m", "cpus": "1.00"},
    "keycloak-db": {"memory": "512m", "cpus": "0.50"},
    "keycloak": {"memory": "2g", "cpus": "1.00"},
    "unibridge-service": {"memory": "512m", "cpus": "1.00"},
    "prometheus": {"memory": "512m", "cpus": "0.50"},
    "litellm-db": {"memory": "512m", "cpus": "0.50"},
    "litellm": {"memory": "2g", "cpus": "1.00"},
    "unibridge-ui": {"memory": "128m", "cpus": "0.25"},
    "blackbox-exporter": {"memory": "128m", "cpus": "0.25"},
    "bifrost": {"memory": "2g", "cpus": "1.00"},
    "bifrost-tls": {"memory": "128m", "cpus": "0.25"},
    "llm-converter-bi": {"memory": "512m", "cpus": "0.50"},
}

DEFAULT_LOGGING = {
    "driver": "json-file",
    "options": {"max-size": "50m", "max-file": "5"},
}

REQUIRED_PROMETHEUS_ALERTS = {
    "APISIXHigh5xxRate",
    "UniBridgeServiceDown",
    "UniBridgeMetaDbDown",
    "KeycloakDbDown",
    "LiteLLMDbDown",
    "BifrostDown",
    "UniBridgeAuditWritesMissing",
}

FORBIDDEN_COMPOSE_PATTERNS = [
    "KC_BOOTSTRAP_ADMIN_PASSWORD=${KC_ADMIN_PASSWORD:-admin}",
    "POSTGRES_PASSWORD=${KC_DB_PASSWORD:-keycloak}",
    "KC_DB_PASSWORD=${KC_DB_PASSWORD:-keycloak}",
    "POSTGRES_PASSWORD=${LITELLM_DB_PASSWORD:-litellm}",
    "DATABASE_URL=postgresql://litellm:${LITELLM_DB_PASSWORD:-litellm}@litellm-db:5432/litellm",
]

REQUIRED_COMPOSE_SECRET_INTERPOLATIONS = {
    "ENCRYPTION_KEY=${ENCRYPTION_KEY:?ENCRYPTION_KEY is required}": 1,
    "APISIX_ADMIN_KEY=${APISIX_ADMIN_KEY:?APISIX_ADMIN_KEY is required}": 2,
    "KEYCLOAK_SERVICE_CLIENT_SECRET=${KEYCLOAK_SERVICE_CLIENT_SECRET:?KEYCLOAK_SERVICE_CLIENT_SECRET is required}": 2,
    "LITELLM_MASTER_KEY=${LITELLM_MASTER_KEY:?LITELLM_MASTER_KEY is required}": 2,
    "APISIX_INTERNAL_PROXY_SECRET=${APISIX_INTERNAL_PROXY_SECRET:?APISIX_INTERNAL_PROXY_SECRET"
    " is required — generate one with python3 -c 'import secrets;"
    " print(secrets.token_urlsafe(32))'}": 1,
    "BIFROST_ENCRYPTION_KEY=${BIFROST_ENCRYPTION_KEY:?BIFROST_ENCRYPTION_KEY is required}": 1,
    "BIFROST_ADMIN_PASSWORD=${BIFROST_ADMIN_PASSWORD:?BIFROST_ADMIN_PASSWORD is required}": 1,
    "BIFROST_TEST_VK=${BIFROST_TEST_VK:?BIFROST_TEST_VK is required}": 1,
}

REQUIRED_BLANK_ENV_SECRETS = {
    "ENCRYPTION_KEY",
    "JWT_SECRET",
    "ETCD_ROOT_PASSWORD",
    "APISIX_ADMIN_KEY",
    "APISIX_INTERNAL_PROXY_SECRET",
    "KC_ADMIN_PASSWORD",
    "KC_DB_PASSWORD",
    "KEYCLOAK_SERVICE_CLIENT_SECRET",
    "LITELLM_DB_PASSWORD",
    "LITELLM_MASTER_KEY",
    "BIFROST_ENCRYPTION_KEY",
    "BIFROST_ADMIN_PASSWORD",
    "BIFROST_TEST_VK",
}

BLUEGREEN_SHARED_ENV_SERVICES = ("unibridge-service", "llm-converter", "unibridge-ui")

# Keys whose value expression legitimately differs between docker-compose.yml
# (single stack) and docker-compose.app.yml (one project per color). Everything
# else must be identical, or the split production stack silently runs on
# different settings than the single-stack file documents.
COLOR_SPECIFIC_ENV = {
    # Route target pinned to the color APISIX currently serves, not this one.
    "APISIX_UNIBRIDGE_SERVICE_NODE",
    # Same, for the converter upstream.
    "APISIX_LLM_CONVERTER_NODE",
    # This container's own node identity; empty single-stack = always active.
    "UNIBRIDGE_SELF_NODE",
    # Public port: the edge proxy owns it in blue/green, the UI owns it alone
    # in the single stack.
    "UNIBRIDGE_UI_PORT",
    # nginx proxies to the per-color service alias in blue/green.
    "UNIBRIDGE_SERVICE_UPSTREAM",
}

COMPOSE_FILES = (
    COMPOSE_FILE,
    BLUEGREEN_INFRA_COMPOSE_FILE,
    BLUEGREEN_APP_COMPOSE_FILE,
    BLUEGREEN_EDGE_COMPOSE_FILE,
)

COMPOSE_VARIABLE_PATTERN = re.compile(r"\$\{([A-Z][A-Z0-9_]*)")

# Exported inline by scripts/deploy-bluegreen.sh before every `docker compose`
# invocation (compose_app / compose_edge / compose_infra), so they never need a
# .env entry. Keep in sync with that script.
DEPLOY_SCRIPT_PROVIDED_ENV = {
    "APP_COLOR",
    "UNIBRIDGE_UI_PORT",
    "APISIX_PROVISION_ON_START",
    "APISIX_UNIBRIDGE_SERVICE_NODE",
    "APISIX_LLM_CONVERTER_NODE",
    "UNIBRIDGE_NETWORK_NAME",
    "EDGE_CONFIG_PATH",
}

# Operator-supplied image overrides for pulling pre-built images instead of
# building locally. Deliberately undocumented knobs with safe defaults, so the
# "known somewhere else in the repo" rule below does not cover them.
COMPOSE_IMAGE_OVERRIDE_ENV = {
    "UNIBRIDGE_SERVICE_IMAGE",
    "LLM_CONVERTER_IMAGE",
    "UNIBRIDGE_UI_IMAGE",
}

REQUIRED_REALM_USERNAMES = {"service-account-apihub-service"}
FORBIDDEN_REALM_USERNAMES = {"apihub-admin", "apihub-dev", "apihub-viewer"}


def _parse_env_assignments(path: Path) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        assignments[key] = value
    return assignments


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _service_environment(compose: dict, service: str) -> dict[str, str]:
    """Parse a service's ``environment:`` list of ``KEY=value`` strings."""
    entries = compose["services"][service]["environment"]
    assert isinstance(entries, list), service
    parsed: dict[str, str] = {}
    for entry in entries:
        key, _, value = entry.partition("=")
        parsed[key] = value
    return parsed


def _env_example_names() -> set[str]:
    """Names .env.example defines, both live (``NAME=``) and commented-out."""
    names: set[str] = set()
    for raw_line in ENV_EXAMPLE_FILE.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#?\s*([A-Z][A-Z0-9_]*)=", raw_line.strip())
        if match:
            names.add(match.group(1))
    return names


def _names_mentioned_outside_compose(candidates: set[str]) -> set[str]:
    """Subset of ``candidates`` that any other tracked repo file mentions.

    A compose variable that appears nowhere else — not in app config, a script,
    a Dockerfile or the docs — is a dangling name: its ``${NAME:-default}``
    can never be overridden because nothing else knows it exists.
    """
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    compose_names = {path.name for path in COMPOSE_FILES}
    token_pattern = re.compile(r"[A-Z][A-Z0-9_]{2,}")
    found: set[str] = set()
    for relative in listing.stdout.split("\0"):
        if not relative or Path(relative).name in compose_names:
            continue
        path = REPO_ROOT / relative
        try:
            if not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        found |= candidates & set(token_pattern.findall(text))
        if found == candidates:
            break
    return found


def test_docker_compose_applies_operational_defaults_to_all_services() -> None:
    compose = _load_yaml(COMPOSE_FILE)
    services = compose["services"]

    missing_services = sorted(set(COMPOSE_SERVICE_LIMITS) - set(services))
    assert missing_services == []

    for service_name, expected_limits in COMPOSE_SERVICE_LIMITS.items():
        service = services[service_name]
        assert service.get("restart") == "unless-stopped", service_name
        assert service.get("logging") == DEFAULT_LOGGING, service_name
        assert service.get("mem_limit") == expected_limits["memory"], service_name
        assert service.get("cpus") == expected_limits["cpus"], service_name
        assert (
            service.get("deploy", {})
            .get("resources", {})
            .get("limits", {})
        ) == expected_limits, service_name

    assert services["unibridge-service"].get("init") is True
    assert services["unibridge-ui"].get("init") is True


def test_nginx_blocks_public_api_metrics_proxy() -> None:
    nginx_config = NGINX_CONFIG_FILE.read_text(encoding="utf-8")

    exact_block = "location = /_api/metrics"
    prefix_block = "location ^~ /_api/metrics/"
    api_proxy = "location /_api/"

    assert exact_block in nginx_config
    assert prefix_block in nginx_config
    assert nginx_config.index(exact_block) < nginx_config.index(api_proxy)
    assert nginx_config.index(prefix_block) < nginx_config.index(api_proxy)


def test_docker_compose_declares_ui_and_prometheus_healthchecks() -> None:
    services = _load_yaml(COMPOSE_FILE)["services"]

    ui_healthcheck = services["unibridge-ui"].get("healthcheck", {})
    prometheus_healthcheck = services["prometheus"].get("healthcheck", {})
    blackbox_healthcheck = services["blackbox-exporter"].get("healthcheck", {})

    assert ui_healthcheck["test"] == [
        "CMD-SHELL",
        "wget --no-check-certificate -q -O- https://127.0.0.1/healthz | grep -q '^ok$'",
    ]
    assert "/-/ready" in str(prometheus_healthcheck)
    assert "/-/healthy" in str(blackbox_healthcheck)


def test_bluegreen_ui_and_edge_healthchecks_use_ipv4_loopback() -> None:
    expected = [
        "CMD-SHELL",
        "wget --no-check-certificate -q -O- https://127.0.0.1/healthz | grep -q '^ok$'",
    ]
    app_services = _load_yaml(BLUEGREEN_APP_COMPOSE_FILE)["services"]
    edge_services = _load_yaml(BLUEGREEN_EDGE_COMPOSE_FILE)["services"]

    assert app_services["unibridge-ui"]["healthcheck"]["test"] == expected
    assert edge_services["edge"]["healthcheck"]["test"] == expected


def test_ui_docker_context_excludes_host_build_artifacts() -> None:
    ignored = {
        line.strip()
        for line in UI_DOCKERIGNORE_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }

    assert {"node_modules", "dist", "coverage"} <= ignored


def test_bluegreen_compose_splits_stateful_infra_from_app_tier() -> None:
    infra_services = set(_load_yaml(BLUEGREEN_INFRA_COMPOSE_FILE)["services"])
    app_services = set(_load_yaml(BLUEGREEN_APP_COMPOSE_FILE)["services"])
    edge_services = set(_load_yaml(BLUEGREEN_EDGE_COMPOSE_FILE)["services"])

    assert {
        "etcd",
        "apisix",
        "keycloak-db",
        "keycloak",
        "litellm-db",
        "litellm",
        "prometheus",
        "blackbox-exporter",
    } <= infra_services
    assert {"unibridge-service", "llm-converter", "unibridge-ui"} == app_services
    assert {"edge"} == edge_services
    assert not {"unibridge-service", "llm-converter", "unibridge-ui"} & infra_services


def test_bluegreen_app_uses_color_specific_targets_and_deferred_apisix_promotion() -> None:
    app_compose = _load_yaml(BLUEGREEN_APP_COMPOSE_FILE)
    app_services = app_compose["services"]
    service_env = app_services["unibridge-service"]["environment"]
    ui_env = app_services["unibridge-ui"]["environment"]
    converter = app_services["llm-converter"]

    # Default true so a manual `compose up` bootstraps routes rather than coming
    # up route-less; deploy-bluegreen.sh always passes the value explicitly and
    # sets it false for inactive colors (it is overridable via the env var).
    assert "APISIX_PROVISION_ON_START=${APISIX_PROVISION_ON_START:-true}" in service_env
    assert (
        "APISIX_UNIBRIDGE_SERVICE_NODE=${APISIX_UNIBRIDGE_SERVICE_NODE:-unibridge-service-${APP_COLOR}:8000}"
        in service_env
    )
    assert (
        "APISIX_LLM_CONVERTER_NODE=${APISIX_LLM_CONVERTER_NODE:-llm-converter-${APP_COLOR}:4001}"
        in service_env
    )
    assert "llm-converter-state:/var/lib/llm-converter" in converter["volumes"]
    assert app_compose["volumes"]["llm-converter-state"]["name"] == (
        "${LLM_CONVERTER_STATE_VOLUME:-unibridge_llm-converter-state}"
    )
    assert app_compose["volumes"]["prometheus-file-sd"] == {
        "external": True,
        "name": "${PROMETHEUS_FILE_SD_VOLUME:-unibridge_prometheus-file-sd}",
    }
    assert app_compose["volumes"]["unibridge-data"] == {
        "external": True,
        "name": "${UNIBRIDGE_DATA_VOLUME:-unibridge_unibridge-data}",
    }
    assert app_compose["volumes"]["llm-converter-state"] == {
        "external": True,
        "name": "${LLM_CONVERTER_STATE_VOLUME:-unibridge_llm-converter-state}",
    }
    assert (
        "UNIBRIDGE_SERVICE_UPSTREAM=${UNIBRIDGE_SERVICE_UPSTREAM:-unibridge-service-${APP_COLOR}}"
        in ui_env
    )


def test_bluegreen_app_env_matches_single_stack() -> None:
    single_stack = _load_yaml(COMPOSE_FILE)
    bluegreen_app = _load_yaml(BLUEGREEN_APP_COMPOSE_FILE)

    for service in BLUEGREEN_SHARED_ENV_SERVICES:
        expected = _service_environment(single_stack, service)
        actual = _service_environment(bluegreen_app, service)

        # No compose file uses `env_file:`, so a key missing from the
        # blue/green `environment:` list cannot be set in production at all.
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        assert not missing, (
            f"{service}: docker-compose.app.yml is missing environment keys "
            f"present in docker-compose.yml: {missing}"
        )
        assert not extra, (
            f"{service}: docker-compose.app.yml defines environment keys "
            f"absent from docker-compose.yml: {extra}"
        )

        drifted = sorted(
            key
            for key in expected
            if key not in COLOR_SPECIFIC_ENV and expected[key] != actual[key]
        )
        assert not drifted, (
            f"{service}: value expressions drifted between docker-compose.yml "
            f"and docker-compose.app.yml for {drifted} — "
            + "; ".join(
                f"{key}: root={expected[key]!r} app={actual[key]!r}"
                for key in drifted
            )
        )

        # Guard the allowlist itself: a key listed as color-specific that no
        # longer differs is stale and hides future drift.
        stale = sorted(
            key
            for key in COLOR_SPECIFIC_ENV & set(expected) & set(actual)
            if expected[key] == actual[key]
        )
        assert not stale, (
            f"{service}: COLOR_SPECIFIC_ENV entries no longer differ and should "
            f"be removed from the allowlist: {stale}"
        )


def test_compose_variable_references_are_defined() -> None:
    referenced: dict[str, list[str]] = {}
    for compose_path in COMPOSE_FILES:
        text = compose_path.read_text(encoding="utf-8")
        for name in COMPOSE_VARIABLE_PATTERN.findall(text):
            referenced.setdefault(name, []).append(compose_path.name)

    known = (
        _env_example_names()
        | DEPLOY_SCRIPT_PROVIDED_ENV
        | COMPOSE_IMAGE_OVERRIDE_ENV
    )
    candidates = set(referenced) - known
    known |= _names_mentioned_outside_compose(candidates)

    dangling = sorted(set(referenced) - known)
    assert not dangling, (
        "compose files reference variables that exist nowhere else in the repo "
        "(typo, or a knob renamed on one side only): "
        + "; ".join(
            f"{name} in {sorted(set(referenced[name]))}" for name in dangling
        )
    )


def test_readme_states_compose_v2_required_for_resource_limits() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "Docker Compose v2" in readme
    assert "Compose v2" in readme and "deploy.resources.limits" in readme


def test_prometheus_scrapes_service_and_loads_alert_rules() -> None:
    config = _load_yaml(PROMETHEUS_CONFIG_FILE)
    scrape_jobs = {
        job["job_name"]: job
        for job in config.get("scrape_configs", [])
    }

    assert "/etc/prometheus/rules/*.yml" in config.get("rule_files", [])
    assert scrape_jobs["unibridge-service"]["metrics_path"] == "/metrics"
    assert scrape_jobs["unibridge-service"]["static_configs"] == [
        {"targets": ["unibridge-service:8000"]}
    ]
    assert scrape_jobs["infra-db-tcp"]["metrics_path"] == "/probe"
    assert scrape_jobs["infra-db-tcp"]["params"] == {"module": ["tcp_connect"]}
    # The LLM monitoring page and Grafana add Bifrost's counters to LiteLLM's,
    # picking them out by this job name; bifrost/config.json whitelists /metrics.
    assert scrape_jobs["bifrost"]["metrics_path"] == "/metrics"
    assert scrape_jobs["bifrost"]["static_configs"] == [{"targets": ["bifrost:8080"]}]
    assert "scheme" not in scrape_jobs["bifrost"]  # plain HTTP inside the network


def test_prometheus_alert_rules_cover_gateway_service_database_and_audit() -> None:
    rule_files = sorted(PROMETHEUS_RULES_DIR.glob("*.yml"))
    loaded_rule_files = [_load_yaml(path) for path in rule_files]
    alerts = {
        rule["alert"]: rule
        for rule_file in loaded_rule_files
        for group in rule_file.get("groups", [])
        for rule in group.get("rules", [])
        if "alert" in rule
    }

    assert REQUIRED_PROMETHEUS_ALERTS <= set(alerts)
    assert "apisix_http_status" in alerts["APISIXHigh5xxRate"]["expr"]
    assert "unibridge_query_duration_seconds_count" in alerts["UniBridgeAuditWritesMissing"]["expr"]
    assert "unibridge_audit_log_write_total" in alerts["UniBridgeAuditWritesMissing"]["expr"]
    assert alerts["BifrostDown"]["expr"] == 'up{job="bifrost"} == 0'


def test_docker_compose_does_not_contain_insecure_password_fallbacks() -> None:
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
    present_patterns = [
        pattern for pattern in FORBIDDEN_COMPOSE_PATTERNS if pattern in compose_text
    ]

    assert present_patterns == [], (
        "docker-compose.yml still contains insecure password fallback patterns: "
        f"{present_patterns}"
    )


def test_docker_compose_requires_runtime_secrets_without_fallbacks() -> None:
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
    missing_patterns = [
        pattern
        for pattern, expected_count in REQUIRED_COMPOSE_SECRET_INTERPOLATIONS.items()
        if compose_text.count(pattern) != expected_count
    ]

    assert missing_patterns == [], (
        "docker-compose.yml is missing required secret interpolation(s): "
        f"{missing_patterns}"
    )


def test_env_example_leaves_required_secrets_blank() -> None:
    env_assignments = _parse_env_assignments(ENV_EXAMPLE_FILE)
    non_blank_required = {
        key: env_assignments.get(key)
        for key in sorted(REQUIRED_BLANK_ENV_SECRETS)
        if env_assignments.get(key, "") != ""
    }

    assert non_blank_required == {}, (
        ".env.example should leave required deployment secrets blank: "
        f"{non_blank_required}"
    )


def test_realm_export_keeps_only_service_account_user() -> None:
    realm_export = json.loads(REALM_EXPORT_FILE.read_text(encoding="utf-8"))
    usernames = {
        user["username"]
        for user in realm_export.get("users", [])
        if "username" in user
    }

    missing_required = sorted(REQUIRED_REALM_USERNAMES - usernames)
    unexpected_users = sorted(FORBIDDEN_REALM_USERNAMES & usernames)

    assert missing_required == [], (
        "keycloak/realm-export.json is missing required usernames: "
        f"{missing_required}"
    )
    assert unexpected_users == [], (
        "keycloak/realm-export.json still contains forbidden usernames: "
        f"{unexpected_users}"
    )


def test_edge_template_strips_consumer_identity_and_keeps_keepalive() -> None:
    template = EDGE_TEMPLATE_FILE.read_text(encoding="utf-8")

    # Client-supplied consumer identity must be cleared at the trust boundary so
    # only APISIX can assert it downstream.
    assert 'proxy_set_header X-Consumer-Username "";' in template
    assert 'proxy_set_header X-Consumer-Custom-Id "";' in template
    assert 'proxy_set_header X-UniBridge-Internal-Proxy "";' in template

    # Connection header is driven by a map (keep-alive for normal requests,
    # upgrade only for real WebSocket), not an unconditional "upgrade".
    assert "map $http_upgrade $connection_upgrade" in template
    assert "proxy_set_header Connection $connection_upgrade;" in template
    assert 'proxy_set_header Connection "upgrade";' not in template


def test_edge_template_streams_responses_unbuffered() -> None:
    template = EDGE_TEMPLATE_FILE.read_text(encoding="utf-8")
    proxy_location = template.split("location / {", 1)[1].split("\n    }", 1)[0]

    # LLM event streams must reach the client as they are sent. nginx buffers
    # proxied responses by default, and the UI nginx behind the edge does not
    # pass X-Accel-Buffering on, so the edge has to switch buffering off itself.
    assert "proxy_buffering off;" in proxy_location


def _edge_proxy_directives(template: str) -> list[str]:
    """The directives of a template's ``location /`` block, comments left out."""
    body = template.split("\n    location / {\n", 1)[1].split("\n    }\n", 1)[0]
    return [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _seconds(directive: str, block: str) -> int:
    match = re.search(rf"\b{directive} (\d+)s;", block)
    assert match, directive
    return int(match.group(1))


def test_edge_bifrost_server_differs_from_the_default_one_in_its_certificate() -> None:
    default = EDGE_TEMPLATE_FILE.read_text(encoding="utf-8")
    bifrost = EDGE_BIFROST_TEMPLATE_FILE.read_text(encoding="utf-8")

    # Past the TLS handshake it is the same hop: the trust boundary, the active
    # color, and the Host header the UI nginx picks its Bifrost server by.
    timeouts = ("proxy_read_timeout ", "proxy_send_timeout ")
    assert [
        line for line in _edge_proxy_directives(bifrost) if not line.startswith(timeouts)
    ] == [line for line in _edge_proxy_directives(default) if not line.startswith(timeouts)]
    for directive in (
        "listen 443 ssl",
        "client_max_body_size 100m;",
        "ssl_protocols TLSv1.2 TLSv1.3;",
        "ssl_prefer_server_ciphers on;",
        "server_tokens off;",
        "resolver 127.0.0.11 valid=5s ipv6=off;",
    ):
        assert directive in default and directive in bifrost, directive
    # Not the hop that cuts the live update socket the UI nginx keeps open.
    [(_, ui_socket)] = [
        location
        for location in _nginx_locations(UI_BIFROST_SERVER_FILE.read_text(encoding="utf-8"))
        if location[0] == "= /ws"
    ]
    bifrost_location = " ".join(_edge_proxy_directives(bifrost))
    for directive in ("proxy_read_timeout", "proxy_send_timeout"):
        assert _seconds(directive, bifrost_location) >= _seconds(directive, ui_socket), directive

    # Bifrost sends its own security headers, as the UI nginx's server for the
    # name expects; a second copy with another value would conflict.
    assert not re.search(r"^\s*add_header\b", bifrost, re.M)
    assert re.search(r"^\s*add_header Strict-Transport-Security\b", default, re.M)

    # A certificate of its own, for that name only: every other name and IP
    # addresses stay on the default server.
    assert "ssl_certificate /etc/nginx/ssl/tls-bifrost.crt;" in bifrost
    assert "ssl_certificate_key /etc/nginx/ssl/tls-bifrost.key;" in bifrost
    assert "ssl_certificate /etc/nginx/ssl/tls.crt;" in default
    assert re.search(r"^\s*server_name __BIFROST_UI_HOSTNAME__;$", bifrost, re.M)
    assert not re.search(r"^\s*listen\b.*default_server", bifrost, re.M)
    assert re.search(r"^\s*listen 443 ssl default_server;$", default, re.M)
    # A name over 46 characters must not stop nginx. Both files end up in one
    # http block, where nginx refuses the setting twice.
    bucket = re.search(r"^server_names_hash_bucket_size (\d+);$", bifrost, re.M)
    assert bucket and int(bucket.group(1)) >= 512
    assert "server_names_hash_bucket_size" not in default


def test_bifrost_ui_certificate_falls_back_to_the_unibridge_pair() -> None:
    # Whatever ends TLS for BIFROST_UI_HOSTNAME presents the pair: the edge on
    # blue-green, the UI nginx on a single stack (and in each color). With
    # neither variable set the name keeps the certificate UniBridge has.
    cert = "${TLS_BIFROST_CERT_PATH:-${TLS_CERT_PATH:-./certs/tls.crt}}:/etc/nginx/ssl/tls-bifrost.crt:ro"
    key = "${TLS_BIFROST_KEY_PATH:-${TLS_KEY_PATH:-./certs/tls.key}}:/etc/nginx/ssl/tls-bifrost.key:ro"
    for path, service in (
        (BLUEGREEN_EDGE_COMPOSE_FILE, "edge"),
        (COMPOSE_FILE, "unibridge-ui"),
        (BLUEGREEN_APP_COMPOSE_FILE, "unibridge-ui"),
    ):
        volumes = _load_yaml(path)["services"][service]["volumes"]
        assert cert in volumes and key in volumes, path.name
        assert "${TLS_CERT_PATH:-./certs/tls.crt}:/etc/nginx/ssl/tls.crt:ro" in volumes, path.name
        assert "${TLS_KEY_PATH:-./certs/tls.key}:/etc/nginx/ssl/tls.key:ro" in volumes, path.name

    server = UI_BIFROST_SERVER_FILE.read_text(encoding="utf-8")
    assert "ssl_certificate /etc/nginx/ssl/tls-bifrost.crt;" in server
    assert "ssl_certificate_key /etc/nginx/ssl/tls-bifrost.key;" in server


def test_backup_uses_current_metadata_store_instead_of_sqlite_only() -> None:
    backup_script = BACKUP_SCRIPT_FILE.read_text(encoding="utf-8")
    restore_script = RESTORE_SCRIPT_FILE.read_text(encoding="utf-8")
    meta_lib = BACKUP_META_LIB_FILE.read_text(encoding="utf-8")

    assert 'source "$HERE/lib/meta.sh"' in backup_script
    assert 'source "$HERE/lib/meta.sh"' in restore_script
    assert 'backup_unibridge_meta "$dest"' in backup_script
    assert "unibridge-meta.sql.gz" in meta_lib
    assert "backup_postgres" in meta_lib
    assert "restore_postgres" in meta_lib
    assert "unibridge-meta.db.gz" in meta_lib
    assert "backup_unibridge_meta_sqlite" in meta_lib


def test_backup_metadata_kind_matches_sqlalchemy_urls() -> None:
    cases = [
        (None, "postgres"),
        ("sqlite:///data/meta.db", "sqlite"),
        ("sqlite+aiosqlite:///data/meta.db", "sqlite"),
        ("postgresql://unibridge:pw@db:5432/unibridge", "postgres"),
        ("postgresql+asyncpg://unibridge:pw@db:5432/unibridge", "postgres"),
        ("postgres+asyncpg://unibridge:pw@db:5432/unibridge", "postgres"),
    ]

    for meta_db_url, expected in cases:
        env = os.environ.copy()
        if meta_db_url is None:
            env.pop("META_DB_URL", None)
        else:
            env["META_DB_URL"] = meta_db_url
        result = subprocess.run(
            [
                "bash",
                "-c",
                f"source {shlex.quote(str(BACKUP_META_LIB_FILE))}; unibridge_meta_kind",
            ],
            check=False,
            env=env,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected

    env = os.environ.copy()
    env["META_DB_URL"] = "mysql://user:pw@db/app"
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"source {shlex.quote(str(BACKUP_META_LIB_FILE))}; unibridge_meta_kind",
        ],
        check=False,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "unsupported META_DB_URL" in result.stderr


def test_deploy_script_guards_shared_sqlite_and_serializes() -> None:
    script = DEPLOY_SCRIPT_FILE.read_text(encoding="utf-8")

    # Refuses SQLite for blue/green unless explicitly overridden.
    assert "require_shared_db_safe" in script
    assert "ALLOW_SQLITE_BLUEGREEN" in script
    # Serializes mutating runs with a lock.
    assert "flock" in script
    assert "acquire_lock" in script
    # Normal app deploys must not recreate single-instance shared infra.
    assert "RECONCILE_INFRA_ON_DEPLOY" in script
    assert "require_existing_infra_healthy" in script
    # Forces re-provisioning if APISIX lost its core routes (etcd reset).
    assert "apisix_has_core_routes" in script
    # Also treats pre-auth-hardening routes as stale so API-key requests keep the
    # internal APISIX trust header after blue/green deploys that skip provisioning.
    assert 'APISIX_INTERNAL_PROXY_HEADER_NAME="X-UniBridge-Internal-Proxy"' in script
    assert "route_has_internal_proxy_header" in script
    for route_id, route_var in (
        ("query-api", "query_route"),
        ("query-template-write-api", "query_template_write_route"),
        ("s3-api", "s3_route"),
        ("nas-api", "nas_route"),
        ("usages-api", "usages_route"),
    ):
        assert f'apisix_get "routes/{route_id}"' in script
        assert f'route_has_internal_proxy_header "${route_var}" || return 1' in script
    # These routes proxy to something other than this app, so they carry no
    # internal-proxy header and are checked by topology instead — without them,
    # deploys that boot with provisioning off would never create the route.
    assert 'apisix_get "routes/prometheus-api"' in script
    assert 'json_contains_pair "$prometheus_route" "uri" "/api/prometheus/*" || return 1' in script
    assert (
        'json_contains_pair "$prometheus_route" "upstream_id" "prometheus" || return 1' in script
    )
    # The /api/llm routes follow LLM_GATEWAY (apisix_llm_routes_match; behaviour
    # pinned in test_deploy_holds_the_llm_routes_to_the_gateway_switch).
    llm_check = _shell_function(script, "apisix_llm_routes_match")
    assert "apisix_llm_routes_match || return 1" in _shell_function(script, "apisix_has_core_routes")
    for route_id in ("llm-proxy", "llm-metrics", "llm-models", "llm-messages", "llm-responses"):
        assert f"routes/{route_id}" in llm_check or route_id in llm_check
    for pair in (
        '"uri" "/api/llm/metrics"',
        '"uri" "/api/llm/v1/models"',
        '"upstream_id" "llm-converter"',
        '"upstream_id" "litellm"',
        '"upstream_id" "bifrost"',
    ):
        assert f'json_contains_pair "$route" {pair} || return 1' in llm_check
    for method in ("PUT", "PATCH", "DELETE"):
        assert (
            f'route_allows_method "$query_template_write_route" "{method}" || return 1'
            in script
        )
    assert (
        'json_contains_pair "$query_template_write_route" "uri" '
        '"/api/query/templates/*" || return 1' in script
    )
    assert (
        'json_contains_pair "$query_template_write_route" "upstream_id" '
        '"unibridge-service" || return 1' in script
    )
    # APISIX promotion PUTs retry instead of leaving colors half-switched.
    assert "apisix_put" in script

    # Edge config validation must not mutate/recreate the live edge before
    # APISIX promotion. Validate a detached candidate, then apply it afterward.
    prepare_edge_body = script.split("prepare_edge() {", 1)[1].split("\n}", 1)[0]
    assert 'render_edge_config "$color" "$EDGE_CANDIDATE_CONFIG"' in prepare_edge_body
    assert "compose_edge run --rm --no-deps edge nginx -t" in prepare_edge_body
    assert "compose_edge up" not in prepare_edge_body
    assert "switch_edge" in script
    assert "restore_previous_edge" in script
    assert "restore_apisix_after_edge_failure" in script
    assert "commit_active_color" in script
    assert 'mv "$temporary" "$STATE_FILE"' in script
    assert 'if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then' in script
    assert (
        'APISIX_UNIBRIDGE_SERVICE_NODE="unibridge-service-${provision_route_color}:8000"'
        in script
    )
    assert (
        'APISIX_LLM_CONVERTER_NODE="llm-converter-${provision_route_color}:4001"'
        in script
    )


def test_stale_route_repair_keeps_upstreams_on_active_color() -> None:
    shell = f"""
source {shlex.quote(str(DEPLOY_SCRIPT_FILE))}
printf '%s\n' \
  "$(provision_route_color blue green true)" \
  "$(provision_route_color blue green false)" \
  "$(provision_route_color blue '' true)"
"""
    result = subprocess.run(
        ["bash", "-c", shell],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["green", "blue", "blue"]


def test_compose_app_exports_pinned_route_nodes() -> None:
    shell = f"""
source {shlex.quote(str(DEPLOY_SCRIPT_FILE))}
docker() {{
  printf '%s|%s|%s|%s\n' \
    "$APP_COLOR" \
    "$APISIX_PROVISION_ON_START" \
    "$APISIX_UNIBRIDGE_SERVICE_NODE" \
    "$APISIX_LLM_CONVERTER_NODE"
}}
compose_app blue 3001 true green config
"""
    result = subprocess.run(
        ["bash", "-c", shell],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "blue|true|unibridge-service-green:8000|llm-converter-green:4001"
    )


def _run_require_existing_infra_healthy(inspect_states: dict[str, str]) -> subprocess.CompletedProcess:
    """Run require_existing_infra_healthy with compose/docker stubbed.

    ``inspect_states`` maps a service name to the ``status|health|restart|exit``
    line the stubbed ``docker inspect`` prints for it. Every listed service gets
    a container id (``cid-<service>``) so the "not running" branch never fires.
    """
    services = "\n".join(inspect_states)
    cases = "\n".join(
        f"    *cid-{shlex.quote(service)}) printf '%s\\n' {shlex.quote(state)} ;;"
        for service, state in inspect_states.items()
    )
    shell = f"""
source {shlex.quote(str(DEPLOY_SCRIPT_FILE))}
compose_infra() {{
  case "$1" in
    config) printf '%s\\n' {shlex.quote(services)} ;;
    ps) printf 'cid-%s\\n' "${{@: -1}}" ;;
  esac
}}
docker() {{
  case "$1" in
    network) return 0 ;;
    inspect)
      case "${{@: -1}}" in
{cases}
      esac
      ;;
  esac
}}
require_existing_infra_healthy
"""
    return subprocess.run(["bash", "-c", shell], check=False, text=True, capture_output=True)


def test_require_existing_infra_healthy_accepts_completed_one_shot_bootstrap() -> None:
    # etcd-init is `restart: "no"`: it exits 0 once etcd auth is bootstrapped
    # and drops off a plain `compose ps -q`. A deploy must not read that as an
    # infra outage — before this, every blue/green deploy aborted on it.
    result = _run_require_existing_infra_healthy(
        {
            "etcd": "running|healthy|unless-stopped|0",
            "etcd-init": "exited|none|no|0",
            "apisix": "running|none|unless-stopped|0",
        }
    )

    assert result.returncode == 0, result.stderr


def test_require_existing_infra_healthy_rejects_crashed_or_failed_services() -> None:
    # A long-running service that exited is still an outage, and so is a
    # one-shot that failed — only `restart: "no"` + exit 0 is the healthy shape.
    crashed = _run_require_existing_infra_healthy(
        {"etcd": "exited|none|unless-stopped|0", "etcd-init": "exited|none|no|0"}
    )
    assert crashed.returncode == 1
    assert "infra service is not ready: etcd" in crashed.stderr

    failed_bootstrap = _run_require_existing_infra_healthy(
        {"etcd": "running|healthy|unless-stopped|0", "etcd-init": "exited|none|no|1"}
    )
    assert failed_bootstrap.returncode == 1
    assert "infra service is not ready: etcd-init" in failed_bootstrap.stderr

    # A color's llm-converter forwards to Bifrost under LLM_GATEWAY=bifrost but
    # lives in another compose project, so no depends_on can hold it back: this
    # check is what refuses an app deploy until Bifrost is healthy (#95).
    booting = _run_require_existing_infra_healthy({"bifrost": "running|starting|unless-stopped|0"})
    assert booting.returncode == 1
    assert "infra service is not ready: bifrost" in booting.stderr


def _run_deploy_color_with_standby_env(
    tmp_path: Path, docker_inspect_body: str, standby: str = "green"
):
    """Drive deploy_color blue over an active green standby with stubbed effects.

    ``docker_inspect_body`` becomes the body of the stubbed ``docker inspect``,
    which is what standby_is_disarmed reads. compose_app logs its color,
    provision flag and pinned route color as ``CALL|...`` so the caller can tell
    the target's own start apart from a standby recreate.
    """
    shell = f"""
source {shlex.quote(str(DEPLOY_SCRIPT_FILE))}
active_color() {{ printf '%s' {shlex.quote(standby)}; }}
require_shared_db_safe() {{ :; }}
require_existing_infra_healthy() {{ :; }}
ensure_shared_app_volumes() {{ :; }}
wait_apisix_admin() {{ return 1; }}
wait_color() {{ :; }}
prepare_edge() {{ :; }}
promote_apisix() {{ :; }}
switch_edge() {{ return 0; }}
commit_active_color() {{ :; }}
start_litellm_if_stopped() {{ :; }}
stop_litellm_when_unused() {{ :; }}
compose_app() {{ printf 'CALL|%s|%s|%s\n' "$1" "$3" "$4"; }}
docker() {{
{docker_inspect_body}
}}
deploy_color blue
"""
    env = os.environ.copy()
    env["ENV_FILE"] = str(tmp_path / "absent.env")
    env["STOP_OLD_AFTER_PROMOTE"] = "false"
    env.pop("APISIX_ADMIN_KEY", None)
    env.pop("LLM_GATEWAY", None)
    for name in ("BIFROST_UI_HOSTNAME", "TLS_BIFROST_CERT_PATH", "TLS_BIFROST_KEY_PATH"):
        env.pop(name, None)
    return subprocess.run(
        ["bash", "-c", shell],
        check=False,
        env=env,
        text=True,
        capture_output=True,
    )


def test_disarm_skips_recreating_a_standby_that_is_already_disarmed(tmp_path: Path) -> None:
    result = _run_deploy_color_with_standby_env(
        tmp_path,
        """
  [[ "$1" == "inspect" ]] || exit 40
  printf '%s\n' 'APP_COLOR=green' 'APISIX_PROVISION_ON_START=false'
""",
    )

    assert result.returncode == 0, result.stderr
    calls = [line for line in result.stdout.splitlines() if line.startswith("CALL|")]
    # Only the target color is started; recreating the standby would re-run the
    # old image's `alembic upgrade head` against an already-migrated database.
    assert calls == ["CALL|blue|false|blue"]
    assert "already disarmed" in result.stdout


def test_disarm_recreates_a_standby_that_is_still_armed(tmp_path: Path) -> None:
    result = _run_deploy_color_with_standby_env(
        tmp_path,
        """
  [[ "$1" == "inspect" ]] || exit 40
  printf '%s\n' 'APP_COLOR=green' 'APISIX_PROVISION_ON_START=true'
""",
    )

    assert result.returncode == 0, result.stderr
    calls = [line for line in result.stdout.splitlines() if line.startswith("CALL|")]
    # Standby recreate pins the route color forward to the newly active color.
    assert calls == ["CALL|blue|false|blue", "CALL|green|false|blue"]


def test_disarm_recreates_when_the_standby_env_cannot_be_inspected(tmp_path: Path) -> None:
    result = _run_deploy_color_with_standby_env(tmp_path, "  return 1")

    assert result.returncode == 0, result.stderr
    calls = [line for line in result.stdout.splitlines() if line.startswith("CALL|")]
    assert calls == ["CALL|blue|false|blue", "CALL|green|false|blue"]


def test_disarm_does_not_abort_the_deploy_on_a_corrupt_previous_color(tmp_path: Path) -> None:
    # The disarm check runs after the promotion is already committed, so an
    # unusable $old must not exit the script and lose the success report.
    result = _run_deploy_color_with_standby_env(
        tmp_path, "  return 1", standby="purple"
    )

    assert result.returncode == 0, result.stderr
    assert "Active color: blue" in result.stdout


def test_rollback_refuses_to_recreate_an_unhealthy_inactive_color() -> None:
    shell = f"""
source {shlex.quote(str(DEPLOY_SCRIPT_FILE))}
active_color() {{ printf '%s' blue; }}
color_is_healthy() {{ return 1; }}
promote_color() {{ exit 20; }}
if rollback; then
  exit 10
fi
"""
    result = subprocess.run(
        ["bash", "-c", shell],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert "rollback was not attempted" in result.stderr
    assert "Active blue remains unchanged" in result.stderr


def test_rollback_only_promotes_an_already_healthy_color() -> None:
    shell = f"""
source {shlex.quote(str(DEPLOY_SCRIPT_FILE))}
active_color() {{ printf '%s' blue; }}
color_is_healthy() {{ return 0; }}
promote_color() {{ printf 'promoted:%s\n' "$1"; }}
rollback
"""
    result = subprocess.run(
        ["bash", "-c", shell],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "promoted:green"


def test_promote_and_rollback_do_not_reconcile_shared_infra() -> None:
    script = DEPLOY_SCRIPT_FILE.read_text(encoding="utf-8")
    promote_body = script.split("promote_color() {", 1)[1].split("\n}", 1)[0]
    rollback_body = script.split("rollback() {", 1)[1].split("\n}", 1)[0]

    assert "up_infra" not in promote_body
    assert "compose_app" not in rollback_body
    assert "color_is_healthy" in rollback_body


def test_existing_infra_check_is_read_only_and_accepts_healthy_services() -> None:
    shell = f"""
source {shlex.quote(str(DEPLOY_SCRIPT_FILE))}
NETWORK_NAME=test-network
compose_infra() {{
  if [[ "$1" == "config" && "$2" == "--services" ]]; then
    printf '%s\n' apisix postgres
  elif [[ "$1" == "ps" && "$2" == "-aq" ]]; then
    printf 'id-%s\n' "$3"
  else
    exit 30
  fi
}}
docker() {{
  if [[ "$1" == "network" && "$2" == "inspect" ]]; then
    return 0
  fi
  if [[ "$1" == "inspect" ]]; then
    # status|health|restart-policy|exit-code, the script's own inspect format.
    case "${{@: -1}}" in
      id-apisix) printf '%s\n' 'running|none|unless-stopped|0' ;;
      id-postgres) printf '%s\n' 'running|healthy|unless-stopped|0' ;;
      *) exit 31 ;;
    esac
    return 0
  fi
  exit 32
}}
require_existing_infra_healthy
"""
    result = subprocess.run(
        ["bash", "-c", shell],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr


def test_edge_switch_restores_live_config_when_start_fails(tmp_path: Path) -> None:
    live_config = tmp_path / "default.conf"
    candidate_config = tmp_path / "candidate.conf"
    previous_config = tmp_path / "previous.conf"
    live_config.write_text("old\n", encoding="utf-8")
    candidate_config.write_text("new\n", encoding="utf-8")

    shell = f"""
source {shlex.quote(str(DEPLOY_SCRIPT_FILE))}
EDGE_CONFIG={shlex.quote(str(live_config))}
EDGE_CANDIDATE_CONFIG={shlex.quote(str(candidate_config))}
EDGE_PREVIOUS_CONFIG={shlex.quote(str(previous_config))}
compose_calls=0
compose_edge() {{
  compose_calls=$((compose_calls + 1))
  if [[ "$1" == "up" && "$compose_calls" -eq 1 ]]; then
    return 1
  fi
  return 0
}}
if switch_edge; then
  exit 10
fi
[[ "$(<"$EDGE_CONFIG")" == "old" ]]
"""
    result = subprocess.run(
        ["bash", "-c", shell],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert live_config.read_text(encoding="utf-8") == "old\n"


def test_active_color_state_write_is_atomic(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    env = os.environ.copy()
    env["BLUEGREEN_STATE_DIR"] = str(state_dir)
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"source {shlex.quote(str(DEPLOY_SCRIPT_FILE))}; write_active_color green",
        ],
        check=False,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert (state_dir / "bluegreen-active").read_text(encoding="utf-8") == "green\n"
    assert list(state_dir.glob("*.tmp.*")) == []


def test_active_color_write_failure_rolls_edge_and_apisix_back(tmp_path: Path) -> None:
    blocked_state_dir = tmp_path / "not-a-directory"
    blocked_state_dir.write_text("blocked\n", encoding="utf-8")
    live_config = tmp_path / "default.conf"
    previous_config = tmp_path / "previous.conf"
    apisix_log = tmp_path / "apisix.log"
    live_config.write_text("new\n", encoding="utf-8")
    previous_config.write_text("old\n", encoding="utf-8")

    shell = f"""
source {shlex.quote(str(DEPLOY_SCRIPT_FILE))}
STATE_DIR={shlex.quote(str(blocked_state_dir))}
STATE_FILE="$STATE_DIR/bluegreen-active"
EDGE_CONFIG={shlex.quote(str(live_config))}
EDGE_PREVIOUS_CONFIG={shlex.quote(str(previous_config))}
compose_edge() {{ return 0; }}
promote_apisix() {{ printf '%s %s\n' "$1" "$2" > {shlex.quote(str(apisix_log))}; }}
if commit_active_color blue green; then
  exit 10
fi
[[ "$(<"$EDGE_CONFIG")" == "old" ]]
"""
    result = subprocess.run(
        ["bash", "-c", shell],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert live_config.read_text(encoding="utf-8") == "old\n"
    assert apisix_log.read_text(encoding="utf-8") == "green blue\n"


def test_ui_entrypoint_fails_loudly_on_bad_template() -> None:
    entrypoint = UI_ENTRYPOINT_FILE.read_text(encoding="utf-8")

    assert "set -eu" in entrypoint
    # Guards against leftover placeholders and invalid config before exec'ing nginx.
    assert "__UNIBRIDGE_SERVICE_UPSTREAM__" in entrypoint
    assert "nginx -t" in entrypoint


BIFROST_SERVICES = {"bifrost", "bifrost-tls", "llm-converter-bi"}
BIFROST_TEST_ROUTE_IDS = {"llm-bi-proxy", "llm-bi-messages", "llm-bi-responses", "llm-bi-models"}
BIFROST_TEST_SECRETS = ("BIFROST_ENCRYPTION_KEY", "BIFROST_ADMIN_PASSWORD", "BIFROST_TEST_VK")
# The exact gateway surface: Bifrost answers any other extension-less path with
# its UI's index.html (HTTP 200) and serves MCP under /v1/mcp/*.
BIFROST_TEST_EXPOSED_PATHS = {
    "/api/llm-bi/v1/chat/completions",
    "/api/llm-bi/v1/completions",
    "/api/llm-bi/v1/embeddings",
    "/api/llm-bi/v1/messages",
    "/api/llm-bi/v1/responses",
    "/api/llm-bi/v1/models",
}


def test_bifrost_services_run_by_default_and_are_identical_in_both_layouts() -> None:
    single = _load_yaml(COMPOSE_FILE)
    infra = _load_yaml(BLUEGREEN_INFRA_COMPOSE_FILE)

    for compose in (single, infra):
        services = compose["services"]
        assert BIFROST_SERVICES <= set(services)
        # Bifrost runs whatever LLM_GATEWAY says, so nothing may hide behind a
        # profile of its own: a profiled service is skipped by a plain `up` and
        # by `config --services`, which the blue/green infra health gate
        # iterates. Only LiteLLM drops out, under bifrost, through
        # litellm/compose-gate.yml (test_litellm_runs_only_on_litellm).
        assert not [name for name, service in services.items() if service.get("profiles")]
        # Only the TLS front is published beyond loopback. Bifrost's own
        # plain-HTTP port stays on 127.0.0.1, and the converter has none.
        assert services["bifrost"]["ports"] == [
            "127.0.0.1:${BIFROST_TEST_ADMIN_PORT:-18080}:8080"
        ]
        assert "ports" not in services["llm-converter-bi"]
        assert services["bifrost-tls"]["ports"] == ["${BIFROST_UI_PORT:-18443}:443"]

    # The split layout is what production runs; it must not drift from the
    # single-stack definition.
    for name in BIFROST_SERVICES:
        assert single["services"][name] == infra["services"][name], name

    # GOMEMLIMIT follows the memory limit (~90%) so the Go GC tightens before
    # the cgroup OOM-kills Bifrost; a limit raised alone would leave GC at the old one.
    bifrost = infra["services"]["bifrost"]
    assert bifrost["deploy"]["resources"]["limits"]["memory"] == bifrost["mem_limit"]
    limit_mib = int(bifrost["mem_limit"][:-1]) * {"g": 1024, "m": 1}[bifrost["mem_limit"][-1]]
    [gomemlimit] = [entry.split("=", 1)[1] for entry in bifrost["environment"] if entry.startswith("GOMEMLIMIT=")]
    assert gomemlimit.endswith("MiB")
    assert 0.85 <= int(gomemlimit[: -len("MiB")]) / limit_mib <= 0.92

    # Required like the other runtime secrets: compose fails fast when one is
    # missing (bifrost/entrypoint.sh still checks the format).
    bifrost_env = _service_environment(infra, "bifrost")
    for secret in BIFROST_TEST_SECRETS:
        assert bifrost_env[secret] == f"${{{secret}:?{secret} is required}}", secret

    assert infra["volumes"]["bifrost-test-data"]["name"] == (
        "${BIFROST_TEST_DATA_VOLUME:-unibridge_bifrost-test-data}"
    )
    assert infra["volumes"]["llm-converter-bi-state"]["name"] == (
        "${LLM_CONVERTER_BI_STATE_VOLUME:-unibridge_llm-converter-bi-state}"
    )
    converter_bi = infra["services"]["llm-converter-bi"]
    # Its own response store: an id minted on one path must not resolve on the other.
    assert "llm-converter-bi-state:/var/lib/llm-converter" in converter_bi["volumes"]
    assert _service_environment(infra, "llm-converter-bi")["LITELLM_URL"] == "http://bifrost:8080"


def test_bifrost_image_includes_the_management_auth_bypass_fix() -> None:
    # v2.2.5 fixed an authentication bypass: a percent-encoded path such as
    # `..%2F` reached protected /api endpoints without credentials. bifrost-tls
    # and the BIFROST_UI_HOSTNAME server both forward /api, so an older tag
    # (say, from the June 2026 v1.5 branch) must not come back.
    for compose_file in (COMPOSE_FILE, BLUEGREEN_INFRA_COMPOSE_FILE):
        image = _load_yaml(compose_file)["services"]["bifrost"]["image"]
        repo, _, tag = image.partition(":")
        assert repo == "maximhq/bifrost", image
        release = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", tag)
        assert release, f"{compose_file.name}: pin an exact release tag, not {tag!r}"
        assert tuple(int(part) for part in release.groups()) >= (2, 2, 5), image


def _nginx_location_for(path: str, locations: list[tuple[str, str]]) -> str:
    """Pick the location nginx would use for ``path`` (exact, ^~, regex, prefix)."""
    exact = [spec for spec in (s for s, _ in locations) if spec == f"= {path}"]
    if exact:
        return exact[0]
    prefixes = [
        (spec, spec.split(None, 1)[1] if spec.startswith("^~ ") else spec)
        for spec, _ in locations
        if not spec.startswith(("=", "~"))
    ]
    matching = [(spec, prefix) for spec, prefix in prefixes if path.startswith(prefix)]
    longest = max(matching, key=lambda item: len(item[1]), default=None)
    if longest and longest[0].startswith("^~ "):
        return longest[0]
    for spec, _ in locations:
        if spec.startswith("~ ") and re.search(spec[2:], path):
            return spec
    assert longest, path
    return longest[0]


def test_bifrost_tls_proxy_forwards_only_the_ui_and_management_api() -> None:
    service = _load_yaml(COMPOSE_FILE)["services"]["bifrost-tls"]
    assert "./bifrost/tls-proxy.conf:/etc/nginx/conf.d/default.conf:ro" in service["volumes"]

    conf = (BIFROST_DIR / "tls-proxy.conf").read_text(encoding="utf-8")
    locations = [
        (spec.strip(), body)
        for spec, body in re.findall(r"^\s*location\s+([^{]+?)\s*\{(.*?)^\s*\}", conf, re.S | re.M)
    ]
    proxied = {spec for spec, body in locations if "proxy_pass" in body}

    # What the Bifrost UI itself loads: pages, static files, the management API
    # (Bifrost's admin login guards it) and the live-update socket.
    for path in (
        "/",
        "/login",
        "/workspace",
        "/workspace/logs",
        "/oauth/callback",
        "/assets/index-abc123.js",
        "/static/fonts/Geist-Variable.woff2",
        "/images/mcp.svg",
        "/favicon.ico",
        "/bifrost-logo-dark.webp",
        "/api/session/login",
        "/api/providers",
        "/ws",
    ):
        assert _nginx_location_for(path, locations) in proxied, path

    # Inference, MCP and the unauthenticated /metrics stay off this port: model
    # traffic enters through the gateway's /api/llm-bi routes only.
    blocked = {spec for spec, body in locations if "return 404" in body}
    for path in (
        *(uri.removeprefix("/api/llm-bi") for uri in BIFROST_TEST_EXPOSED_PATHS),
        "/v1/mcp/tool/execute",
        "/openai/v1/chat/completions",
        "/anthropic/v1/messages",
        "/genai/v1beta/models",
        "/mcp",
        "/metrics",
        "/health",
        "/workspacex",
    ):
        assert _nginx_location_for(path, locations) in blocked, path


def test_ui_bifrost_link_targets_the_published_tls_port() -> None:
    # The LLM Monitoring page's Bifrost button is https://HOST_IP:BIFROST_UI_PORT;
    # a default that differs from bifrost-tls's published port is a dead link.
    for path in (COMPOSE_FILE, BLUEGREEN_APP_COMPOSE_FILE):
        env = _service_environment(_load_yaml(path), "unibridge-ui")
        assert env["BIFROST_UI_PORT"] == "${BIFROST_UI_PORT:-18443}", path.name
    entrypoint = UI_ENTRYPOINT_FILE.read_text(encoding="utf-8")
    assert 'BIFROST_ADMIN_URL="https://${HOST_IP:-localhost}:${BIFROST_UI_PORT:-18443}"' in entrypoint
    assert 'BIFROST_ADMIN_URL: "$(json_escape "$BIFROST_ADMIN_URL")"' in entrypoint


def _nginx_locations(conf: str) -> list[tuple[str, str]]:
    """(match spec, whitespace-normalized body) of every location block, in order."""
    return [
        (spec.strip(), " ".join(body.split()))
        for spec, body in re.findall(
            r"^\s*location\s+([^{]+?)\s*\{(.*?)^\s*\}", conf, re.S | re.M
        )
    ]


def test_ui_bifrost_hostname_server_mirrors_the_tls_proxy_allowlist() -> None:
    # BIFROST_UI_HOSTNAME serves the Bifrost UI from the UI nginx on the
    # UniBridge port: it must expose exactly what bifrost-tls exposes, no more.
    tls_proxy = (BIFROST_DIR / "tls-proxy.conf").read_text(encoding="utf-8")
    server = UI_BIFROST_SERVER_FILE.read_text(encoding="utf-8")
    locations = _nginx_locations(server)
    assert [location for location in locations if location[0] != "= /_unibridge/sso"] == [
        location for location in _nginx_locations(tls_proxy) if location[0] != "= /healthz"
    ]
    # The one addition: the UniBridge sign-in handoff, which only this server
    # can do (the cookie must land on the Bifrost host name).
    [(_, sso)] = [location for location in locations if location[0] == "= /_unibridge/sso"]
    for directive in (
        'set $unibridge_upstream "__UNIBRIDGE_SERVICE_UPSTREAM__";',
        "proxy_pass http://$unibridge_upstream:8000/bifrost/sso$is_args$args;",
        "proxy_set_header Host $host;",
        'proxy_set_header X-Consumer-Username "";',
        'proxy_set_header X-UniBridge-Internal-Proxy "";',
    ):
        assert directive in sso, directive
    # Parsed for real (two empty lists would compare equal): everything the
    # allowlist does not name ends in the catch-all 404.
    assert ("^~ /api/", "proxy_pass http://$bifrost_upstream;") in locations
    assert locations[-1] == ("/", "return 404;")
    for directive in (
        'set $bifrost_upstream "bifrost:8080";',
        "proxy_set_header Upgrade $http_upgrade;",
        "proxy_set_header Connection $connection_upgrade;",
        "proxy_set_header X-Forwarded-Proto https;",
        "proxy_buffering off;",
        "error_page 497 =301 https://$http_host$request_uri;",
    ):
        assert directive in server, directive
        assert directive in tls_proxy, directive
    # Bifrost sends its own security headers; a second copy would conflict.
    assert not re.search(r"^\s*add_header\b", server, re.M)
    # At nginx's default 64-byte buckets a server_name over 46 characters stops
    # nginx from starting; the setting must sit at http level, before the server.
    bucket = re.search(r"^server_names_hash_bucket_size (\d+);$", server, re.M)
    assert bucket and int(bucket.group(1)) >= 512
    assert bucket.start() < server.index("server {")

    # Only that name reaches it: UniBridge's own server stays the default for
    # every other Host, IP addresses included.
    assert re.search(r"^\s*server_name __BIFROST_UI_HOSTNAME__;$", server, re.M)
    assert not re.search(r"^\s*listen\b.*default_server", server, re.M)
    nginx_config = NGINX_CONFIG_FILE.read_text(encoding="utf-8")
    assert re.search(r"^\s*listen 443 ssl default_server;$", nginx_config, re.M)


def test_ui_renders_the_bifrost_server_only_when_a_hostname_is_set() -> None:
    # Baked outside conf.d, so nginx loads it only once entrypoint.sh renders it.
    dockerfile = UI_DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY nginx-bifrost-ui.conf /etc/nginx/bifrost-ui.conf.template" in dockerfile

    entrypoint = UI_ENTRYPOINT_FILE.read_text(encoding="utf-8")
    guarded = re.findall(r'^if \[ -n "\$BIFROST_UI_HOSTNAME" \]; then\n(.*?)^fi$', entrypoint, re.S | re.M)
    [render] = [
        block
        for block in guarded
        if "/etc/nginx/bifrost-ui.conf.template > /etc/nginx/conf.d/bifrost-ui.conf" in block
    ]
    assert "s/__UNIBRIDGE_SERVICE_UPSTREAM__/" in render
    # The value is checked before anything is written (a DNS name, and not the
    # name UniBridge itself answers on), and a leftover placeholder fails the
    # start like the upstream ones.
    checks = entrypoint.index("! valid_hostname")
    assert checks < entrypoint.index("runtime-config.js")
    assert '"$(lowercase "${HOST_IP:-localhost}")"' in entrypoint[checks:entrypoint.index("runtime-config.js")]
    assert "__BIFROST_UI_HOSTNAME__" in entrypoint.split("Catch both a failed substitution")[1]
    assert 'BIFROST_UI_HOSTNAME: "$(json_escape "$BIFROST_UI_HOSTNAME")"' in entrypoint

    for path in (COMPOSE_FILE, BLUEGREEN_APP_COMPOSE_FILE):
        compose = _load_yaml(path)
        env = _service_environment(compose, "unibridge-ui")
        assert env["BIFROST_UI_HOSTNAME"] == "${BIFROST_UI_HOSTNAME:-}", path.name
        # The service's sign-in handoff (app/routers/bifrost_sso.py) needs the
        # same name and the Bifrost admin login, both optional so a stack
        # without them still starts (the button then shows Bifrost's login).
        service_env = _service_environment(compose, "unibridge-service")
        assert service_env["BIFROST_UI_HOSTNAME"] == "${BIFROST_UI_HOSTNAME:-}", path.name
        assert service_env["BIFROST_ADMIN_PASSWORD"] == "${BIFROST_ADMIN_PASSWORD:-}", path.name


def _shell_function(script: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) {{\n.*?^}}\n", script, re.S | re.M)
    assert match, name
    return match.group(0)


# BIFROST_UI_HOSTNAME values and whether they are a usable server_name: anything
# else could inject nginx config, and an IP address would take the requests
# UniBridge itself gets. unibridge-ui/entrypoint.sh and the deploy check alike.
BIFROST_UI_HOSTNAME_CASES = {
    "llm-proxy.example.com": True,
    "llm-proxy": True,
    "Bifrost-1.Corp.internal": True,
    "a" * 63 + ".example.com": True,
    "a" * 64 + ".example.com": False,
    ("a" * 50 + ".") * 5 + "com": False,
    "10.0.0.5": False,
    "llm-proxy.example.com:8443": False,
    "https://llm-proxy.example.com": False,
    "llm-proxy.example.com/x": False,
    "llm-proxy.example.com;": False,
    "llm proxy.example.com": False,
    "llm-proxy.example.com\nother.example.com": False,
    "*.example.com": False,
    "-llm.example.com": False,
    "llm-.example.com": False,
    "llm..example.com": False,
    "llm-proxy.example.com.": False,
    ".example.com": False,
    "": False,
}


def test_ui_entrypoint_accepts_only_dns_names_for_the_bifrost_hostname() -> None:
    valid_hostname = _shell_function(UI_ENTRYPOINT_FILE.read_text(encoding="utf-8"), "valid_hostname")
    for hostname, accepted in BIFROST_UI_HOSTNAME_CASES.items():
        result = subprocess.run(
            ["sh", "-c", valid_hostname + 'valid_hostname "$1"', "sh", hostname],
            capture_output=True,
            check=False,
        )
        assert (result.returncode == 0) is accepted, repr(hostname)


def _run_deploy_script(shell: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f"source {shlex.quote(str(DEPLOY_SCRIPT_FILE))}\n{shell}"],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "ENV_FILE": "/nonexistent", **env},
        check=False,
        text=True,
        capture_output=True,
    )


def test_deploy_takes_the_same_bifrost_hostnames_as_the_ui() -> None:
    for hostname, accepted in BIFROST_UI_HOSTNAME_CASES.items():
        if not hostname:
            continue
        # Under a UTF-8 locale too. The script pins LC_ALL=C for its A-Z ranges,
        # which C.UTF-8 (by code point) would not widen anyway; other locales may.
        result = _run_deploy_script(
            "bifrost_ui_hostname", BIFROST_UI_HOSTNAME=hostname, LANG="C.UTF-8", LC_ALL="C.UTF-8"
        )
        assert (result.returncode == 0) is accepted, repr(hostname)
        if accepted:
            # nginx compares server names in lowercase.
            assert result.stdout == hostname.lower()
        else:
            assert "must be a DNS host name" in result.stderr, repr(hostname)
    accented = _run_deploy_script(
        "bifrost_ui_hostname", BIFROST_UI_HOSTNAME="llm-próxy.example.com", LANG="C.UTF-8", LC_ALL="C.UTF-8"
    )
    assert accented.returncode != 0

    unset = _run_deploy_script("bifrost_ui_hostname")
    assert (unset.returncode, unset.stdout) == (0, "")
    # The name UniBridge itself answers on, in any case (localhost by default).
    for env in (
        {"BIFROST_UI_HOSTNAME": "UniBridge.Example.com", "HOST_IP": "unibridge.example.COM"},
        {"BIFROST_UI_HOSTNAME": "localhost"},
    ):
        same = _run_deploy_script("bifrost_ui_hostname", **env)
        assert same.returncode != 0 and "must differ from HOST_IP" in same.stderr, env


def test_edge_config_has_the_bifrost_server_only_with_a_hostname(tmp_path: Path) -> None:
    destination = tmp_path / "generated" / "default.conf"
    render = f"render_edge_config green {shlex.quote(str(destination))}"

    plain = _run_deploy_script(render)
    assert plain.returncode == 0, plain.stderr
    assert destination.read_text(encoding="utf-8") == EDGE_TEMPLATE_FILE.read_text(
        encoding="utf-8"
    ).replace("__ACTIVE_COLOR__", "green")

    named = _run_deploy_script(render, BIFROST_UI_HOSTNAME="LLM-Proxy.Example.Internal", HOST_IP="10.0.0.5")
    assert named.returncode == 0, named.stderr
    config = destination.read_text(encoding="utf-8")
    assert config.startswith(EDGE_TEMPLATE_FILE.read_text(encoding="utf-8").replace("__ACTIVE_COLOR__", "green"))
    assert config.count("\nserver {\n") == 2
    assert "server_name llm-proxy.example.internal;" in config
    assert config.count('set $unibridge_ui "unibridge-ui-green";') == 2
    assert not re.search(r"__[A-Z_]+__", config)

    # A name that is no usable server_name never reaches nginx or sed: deploy
    # refuses it up front, and promote and rollback switch without its server.
    for unusable in ("10.0.0.5", "llm-proxy.example.com;", "localhost"):
        left_out = _run_deploy_script(render, BIFROST_UI_HOSTNAME=unusable)
        assert left_out.returncode == 0, left_out.stderr
        assert destination.read_text(encoding="utf-8") == EDGE_TEMPLATE_FILE.read_text(
            encoding="utf-8"
        ).replace("__ACTIVE_COLOR__", "green"), unusable
    assert sorted(path.name for path in destination.parent.iterdir()) == ["default.conf"]


def _check_bifrost_ui_host(root: Path, mode: str, **env: str) -> subprocess.CompletedProcess:
    return _run_deploy_script(
        f"ROOT_DIR={shlex.quote(str(root))}\ncheck_bifrost_ui_host {mode}",
        **{"HOME": str(root / "home"), **env},
    )


BIFROST_UI_HOST = {"BIFROST_UI_HOSTNAME": "llm-proxy.example.internal", "HOST_IP": "10.0.0.5"}
BIFROST_UI_PAIR = {
    "TLS_BIFROST_CERT_PATH": "./certs/tls-bifrost.crt",
    "TLS_BIFROST_KEY_PATH": "./certs/tls-bifrost.key",
}


def test_deploy_refuses_and_a_switch_warns_about_the_bifrost_certificate(tmp_path: Path) -> None:
    host, named = BIFROST_UI_HOST, BIFROST_UI_PAIR
    missing_key = {**named, "TLS_BIFROST_KEY_PATH": "./certs/absent.key"}
    in_home = {
        "TLS_BIFROST_CERT_PATH": "~/pki/tls-bifrost.crt",
        "TLS_BIFROST_KEY_PATH": "~/pki/tls-bifrost.key",
    }

    # Neither set and no pair of its own anywhere: the UniBridge pair.
    (tmp_path / "certs").mkdir()
    for mode in ("deploy", "switch"):
        for env in ({}, host):
            result = _check_bifrost_ui_host(tmp_path, mode, **env)
            assert (result.returncode, result.stderr) == (0, ""), (mode, env)

    # In the checkout, in HOME, and under a directory named ~ in the checkout,
    # where compose puts ~/ when HOME is not set.
    for directory in (tmp_path / "certs", tmp_path / "home" / "pki", tmp_path / "~" / "pki"):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "tls-bifrost.crt").write_text("certificate\n", encoding="utf-8")
        (directory / "tls-bifrost.key").write_text("key\n", encoding="utf-8")
    # (env, what deploy does, what promote and rollback do, part of the message):
    # deploy refuses every problem; a switch refuses only what the edge cannot
    # start with at all and warns about the rest.
    cases = [
        ({}, "ok", "ok", ""),
        # The pair at the documented place, but nothing names it: the name
        # would quietly get the UniBridge certificate.
        (host, "refuse", "warn", "TLS_BIFROST_CERT_PATH=./certs/tls-bifrost.crt"),
        # Named relative to the checkout, as compose resolves it, absolute, or ~/.
        ({**host, **named}, "ok", "ok", ""),
        ({**host, **{name: str(tmp_path / value) for name, value in named.items()}}, "ok", "ok", ""),
        ({**host, **in_home}, "ok", "ok", ""),
        ({**host, **in_home, "HOME": ""}, "ok", "ok", ""),
        # Half a pair; on a switch nginx -t has the last word.
        ({**host, "TLS_BIFROST_CERT_PATH": named["TLS_BIFROST_CERT_PATH"]}, "refuse", "warn", "together"),
        # No file where the name is served: nginx cannot load it, and Docker
        # would leave an empty directory behind. Not served, the edge starts.
        ({**host, **missing_key}, "refuse", "refuse", "is not a file"),
        ({**host, **named, "TLS_BIFROST_CERT_PATH": "./certs"}, "refuse", "refuse", "is not a file"),
        (missing_key, "refuse", "warn", "is not a file"),
        # ~user, which compose versions resolve differently: like a missing file.
        ({**host, **named, "TLS_BIFROST_KEY_PATH": "~other/tls-bifrost.key"}, "refuse", "refuse", "~/"),
        ({**named, "TLS_BIFROST_KEY_PATH": "~other/tls-bifrost.key"}, "refuse", "warn", "~/"),
        # A volume name to compose, which then refuses every command.
        ({**named, "TLS_BIFROST_CERT_PATH": "certs/tls-bifrost.crt"}, "refuse", "refuse", "volume name"),
        # A name that is no usable server_name: a switch leaves its server out.
        ({"BIFROST_UI_HOSTNAME": "10.0.0.5"}, "refuse", "warn", "must be a DNS host name"),
    ]
    for env, on_deploy, on_switch, message in cases:
        for mode, expected in (("deploy", on_deploy), ("switch", on_switch)):
            result = _check_bifrost_ui_host(tmp_path, mode, **env)
            label = (mode, env, result.stderr)
            if expected == "ok":
                assert (result.returncode, result.stderr) == (0, ""), label
            elif expected == "warn":
                assert result.returncode == 0, label
                assert result.stderr.startswith("WARNING: ") and message in result.stderr, label
            else:
                assert result.returncode != 0, label
                assert result.stderr.startswith("ERROR: ") and message in result.stderr, label

    script = DEPLOY_SCRIPT_FILE.read_text(encoding="utf-8")
    # deploy checks before anything starts: the new color's UI mounts the pair too.
    deploy = _shell_function(script, "deploy_color")
    assert deploy.index("check_bifrost_ui_host deploy") < deploy.index("up_infra")
    assert deploy.index("check_bifrost_ui_host deploy") < deploy.index("compose_app")
    # promote, and rollback through it, before the edge is rendered or started.
    promote = _shell_function(script, "promote_color")
    assert promote.index("check_bifrost_ui_host switch") < promote.index("prepare_edge")
    assert "promote_color" in _shell_function(script, "rollback")


@pytest.mark.skipif(os.geteuid() == 0, reason="root may search every directory")
def test_bifrost_certificate_check_leaves_what_it_cannot_see_to_nginx(tmp_path: Path) -> None:
    # certbot keeps live/ and archive/ at 0700: the deploy user cannot look in,
    # but Docker's daemon and nginx run as root and read the pair. In either
    # mode that is a warning, and prepare_edge's nginx -t decides; a symlink
    # into such a directory is no different.
    (tmp_path / "certs").mkdir()
    (tmp_path / "certs" / "tls-bifrost.key").write_text("key\n", encoding="utf-8")
    hidden = tmp_path / "live"
    hidden.mkdir()
    for name in ("tls-bifrost.crt", "tls-bifrost.key"):
        (hidden / name).write_text("x\n", encoding="utf-8")
    (tmp_path / "certs" / "linked.crt").symlink_to(hidden / "tls-bifrost.crt")
    hidden.chmod(0)
    try:
        for env in (
            {
                **BIFROST_UI_HOST,
                "TLS_BIFROST_CERT_PATH": str(hidden / "tls-bifrost.crt"),
                "TLS_BIFROST_KEY_PATH": str(hidden / "tls-bifrost.key"),
            },
            {**BIFROST_UI_HOST, **BIFROST_UI_PAIR, "TLS_BIFROST_CERT_PATH": "./certs/linked.crt"},
        ):
            for mode in ("deploy", "switch"):
                result = _check_bifrost_ui_host(tmp_path, mode, **env)
                assert result.returncode == 0, (mode, env, result.stderr)
                assert result.stderr.startswith("WARNING: Cannot check "), (mode, env, result.stderr)
                assert "nginx -t decides" in result.stderr
    finally:
        hidden.chmod(0o755)


def test_ui_entrypoint_keeps_the_unibridge_pair_unless_nginx_loads_the_bifrost_one(tmp_path: Path) -> None:
    # On a single stack the UI nginx ends TLS. A pair nginx cannot load (a path
    # that names no file is an empty directory by the time it is mounted, a
    # compose file older than the image mounts nothing, the two may come from
    # different pairs) must not stop nginx and take the whole UI down.
    entrypoint = UI_ENTRYPOINT_FILE.read_text(encoding="utf-8")
    functions = _shell_function(entrypoint, "nginx_loads_pair") + _shell_function(entrypoint, "bifrost_tls_pair")
    # `nginx -t -q -c <config>` that keeps the config it was given and answers
    # as told; the real one is asked in a test below.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "nginx").write_text('#!/bin/sh\ncat "$4" > "$NGINX_SEEN"\n[ "$NGINX_LOADS" = yes ]\n', encoding="utf-8")
    (bin_dir / "nginx").chmod(0o755)
    seen = tmp_path / "seen.conf"
    scratch = tmp_path / "tmp"
    scratch.mkdir()

    def pair(*files: str, directory: str = "", loads: str = "yes") -> tuple[subprocess.CompletedProcess, Path]:
        ssl = tmp_path / f"ssl-{len(list(tmp_path.iterdir()))}"
        ssl.mkdir()
        for name in files:
            (ssl / name).write_text("x\n", encoding="utf-8")
        if directory:
            (ssl / directory).mkdir()
        result = subprocess.run(
            ["sh", "-c", functions + 'bifrost_tls_pair "$1"', "sh", str(ssl)],
            env={
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "BIFROST_UI_HOSTNAME": "llm-proxy.example.com",
                "NGINX_LOADS": loads,
                "NGINX_SEEN": str(seen),
                "TMPDIR": str(scratch),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        # The throwaway config is gone again.
        assert list(scratch.iterdir()) == []
        return result, ssl

    own, ssl = pair("tls-bifrost.crt", "tls-bifrost.key")
    assert (own.returncode, own.stdout, own.stderr) == (0, "tls-bifrost\n", "")
    # nginx was asked about that pair, and only on a config of its own.
    asked = seen.read_text(encoding="utf-8")
    assert f"ssl_certificate {ssl}/tls-bifrost.crt;" in asked
    assert f"ssl_certificate_key {ssl}/tls-bifrost.key;" in asked
    for fallback, _ in (
        # Both files, but nginx cannot load them: half of two pairs, garbage.
        pair("tls-bifrost.crt", "tls-bifrost.key", loads="no"),
        pair(),
        pair("tls-bifrost.crt"),
        pair("tls-bifrost.key", directory="tls-bifrost.crt"),
    ):
        assert (fallback.returncode, fallback.stdout) == (0, "tls\n")
        assert "WARNING" in fallback.stderr and "llm-proxy.example.com" in fallback.stderr

    guarded = re.findall(r'^if \[ -n "\$BIFROST_UI_HOSTNAME" \]; then\n(.*?)^fi$', entrypoint, re.S | re.M)
    [render] = [block for block in guarded if "bifrost-ui.conf.template" in block]
    assert 'BIFROST_TLS_PAIR="$(bifrost_tls_pair /etc/nginx/ssl)"' in render
    assert '-e "s#/etc/nginx/ssl/tls-bifrost\\.#/etc/nginx/ssl/$BIFROST_TLS_PAIR.#g"' in render
    # The substitution reaches both lines and nothing else.
    server = UI_BIFROST_SERVER_FILE.read_text(encoding="utf-8")
    assert server.count("/etc/nginx/ssl/tls-bifrost.") == 2


def _docker(*arguments: str, timeout: float) -> subprocess.CompletedProcess | None:
    """``docker <arguments>``, or None when the daemon does not answer in time."""
    try:
        return subprocess.run(
            ["docker", *arguments], capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return None


def _has_docker_image(image: str) -> bool:
    if shutil.which("docker") is None:
        return False
    inspected = _docker("image", "inspect", image, timeout=10)
    return inspected is not None and inspected.returncode == 0


NEEDS_NGINX_IMAGE = pytest.mark.skipif(
    shutil.which("openssl") is None or not _has_docker_image("nginx:alpine"),
    reason="needs openssl and a local nginx:alpine image",
)


def _self_signed_pair(directory: Path, name: str) -> tuple[Path, Path]:
    cert, key = directory / f"{name}.crt", directory / f"{name}.key"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", f"/CN={name}", "-keyout", str(key), "-out", str(cert),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    # Readable by the container's root also where the daemon remaps users.
    key.chmod(0o644)
    return cert, key


@NEEDS_NGINX_IMAGE
def test_edge_config_passes_nginx_t_with_and_without_the_bifrost_server(tmp_path: Path) -> None:
    # The templates are otherwise only read as text: nginx itself has to accept
    # them concatenated in one http block, with the longest DNS name (253
    # characters, far past the default server_names hash buckets).
    certs = tmp_path / "certs"
    certs.mkdir()
    cert, key = _self_signed_pair(certs, "tls")
    longest_name = ".".join(["a" * 63] * 3 + ["b" * 61])
    assert len(longest_name) == 253
    for hostname in ("", longest_name):
        config = tmp_path / ("bifrost.conf" if hostname else "plain.conf")
        render = _run_deploy_script(
            f"render_edge_config blue {shlex.quote(str(config))}", BIFROST_UI_HOSTNAME=hostname
        )
        assert render.returncode == 0, render.stderr
        mounts = {
            config: "/etc/nginx/conf.d/default.conf",
            cert: "/etc/nginx/ssl/tls.crt",
            key: "/etc/nginx/ssl/tls.key",
        }
        volumes = [f"{source}:{target}:ro" for source, target in mounts.items()]
        # The UniBridge pair, as compose mounts it while TLS_BIFROST_* are unset.
        volumes += [f"{cert}:/etc/nginx/ssl/tls-bifrost.crt:ro", f"{key}:/etc/nginx/ssl/tls-bifrost.key:ro"]
        nginx_t = _docker(
            "run", "--rm", "--network", "none",
            *[argument for volume in volumes for argument in ("-v", volume)],
            "nginx:alpine", "nginx", "-t",
            timeout=120,
        )
        if nginx_t is None:
            pytest.skip("docker did not answer")
        assert nginx_t.returncode == 0, (hostname, nginx_t.stderr)


@NEEDS_NGINX_IMAGE
def test_ui_entrypoint_asks_nginx_itself_whether_the_bifrost_pair_loads(tmp_path: Path) -> None:
    one, other, mixed = tmp_path / "one", tmp_path / "other", tmp_path / "mixed"
    for directory in (one, other, mixed):
        directory.mkdir()
    cert, _ = _self_signed_pair(one, "tls-bifrost")
    _, other_key = _self_signed_pair(other, "tls-bifrost")
    # A certificate with another pair's key: both files, but no pair.
    shutil.copy(cert, mixed / "tls-bifrost.crt")
    shutil.copy(other_key, mixed / "tls-bifrost.key")
    functions = _shell_function(UI_ENTRYPOINT_FILE.read_text(encoding="utf-8"), "nginx_loads_pair")
    ask = functions + (
        'for pair in /one/tls-bifrost /mixed/tls-bifrost /absent/tls-bifrost; do\n'
        '  if nginx_loads_pair "$pair"; then echo loads; else echo refused; fi\n'
        'done\n'
    )
    result = _docker(
        "run", "--rm", "--network", "none", "-v", f"{one}:/one:ro", "-v", f"{mixed}:/mixed:ro",
        "nginx:alpine", "sh", "-c", ask,
        timeout=120,
    )
    if result is None:
        pytest.skip("docker did not answer")
    assert (result.returncode, result.stdout.split()) == (0, ["loads", "refused", "refused"]), result.stderr


def test_bifrost_test_secrets_are_listed_blank_in_env_example() -> None:
    # Present, not just "not set to a value": the blank-secret check above
    # passes vacuously for a key .env.example forgot to list at all.
    assignments = _parse_env_assignments(ENV_EXAMPLE_FILE)
    for secret in BIFROST_TEST_SECRETS:
        assert secret in assignments, secret
        assert assignments[secret] == "", secret


def test_bifrost_config_authenticates_admin_api_and_inference_and_boots_offline() -> None:
    config = json.loads((BIFROST_DIR / "config.json").read_text(encoding="utf-8"))

    # Bifrost leaves its management API (/api/*) open until an admin login is
    # configured, so it must be on from the very first boot.
    governance = config["governance"]
    assert governance["auth_config"] == {
        "admin_username": "env.BIFROST_ADMIN_USERNAME",
        "admin_password": "env.BIFROST_ADMIN_PASSWORD",
        "is_enabled": True,
    }
    assert config["encryption_key"] == "env.BIFROST_ENCRYPTION_KEY"
    assert config["client"]["allow_direct_keys"] is False
    # Bifrost would otherwise forward any client header sent as x-bf-eh-<name>
    # to the model backends; every one of them is dropped.
    assert config["client"]["header_filter_config"] == {"denylist": ["*"]}

    # Inference answers only the virtual key APISIX injects (the LiteLLM
    # master-key pattern), so nothing reaching bifrost:8080 or the loopback
    # admin port off the gateway gets served. allow_all_providers keeps the
    # key valid for providers registered later.
    assert config["client"]["enforce_auth_on_inference"] is True
    (gateway_vk,) = governance["virtual_keys"]
    assert gateway_vk["value"] == "env.BIFROST_TEST_VK"
    assert gateway_vk["is_active"] is True
    assert gateway_vk["allow_all_providers"] is True
    assert "provider_configs" not in gateway_vk
    assert "mcp_configs" not in gateway_vk

    # An empty config store cannot boot without its datasheets, and getbifrost.ai
    # is unreachable air-gapped: both load from files mounted from bifrost/.
    pricing = config["framework"]["pricing"]
    for key, filename in (
        ("pricing_url", "pricing.json"),
        ("model_parameters_url", "model-parameters.json"),
    ):
        assert pricing[key] == f"file:///app/bundle/{filename}"
        json.loads((BIFROST_DIR / filename).read_text(encoding="utf-8"))
    assert pricing["mcp_library_sync_interval"] == 0

    service = _load_yaml(COMPOSE_FILE)["services"]["bifrost"]
    assert "./bifrost/config.json:/app/data/config.json:ro" in service["volumes"]
    assert "./bifrost:/app/bundle:ro" in service["volumes"]
    assert service["entrypoint"] == ["/bin/sh", "/app/bundle/entrypoint.sh"]


def test_bifrost_request_logs_keep_metadata_but_no_content() -> None:
    client = json.loads((BIFROST_DIR / "config.json").read_text(encoding="utf-8"))["client"]

    # logs.db keeps model, status, tokens, latency and the consumer of every
    # request, but no prompts, responses, parameters or raw bytes: with them it
    # grew with every conversation.
    assert client["enable_logging"] is True
    assert client["disable_content_logging"] is True
    # Otherwise a client's x-bf-disable-content-logging: false header (the raw
    # proxy routes pass x-bf-* headers through) would store its content anyway.
    assert client["allow_per_request_content_storage_override"] is False


def test_bifrost_entrypoint_fails_closed_without_secrets() -> None:
    base_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    valid = {
        "BIFROST_ENCRYPTION_KEY": "k" * 32,
        "BIFROST_ADMIN_USERNAME": "admin",
        "BIFROST_ADMIN_PASSWORD": "secret",
        "BIFROST_TEST_VK": "sk-bf-" + "v" * 32,
    }
    cases = (
        ({}, "BIFROST_ENCRYPTION_KEY"),
        ({**valid, "BIFROST_ADMIN_PASSWORD": ""}, "BIFROST_ADMIN_PASSWORD"),
        ({**valid, "BIFROST_TEST_VK": ""}, "BIFROST_TEST_VK"),
        ({**valid, "BIFROST_ENCRYPTION_KEY": "short"}, "at least 16 characters"),
        # Bifrost swaps a prefix-less config.json key for a random one.
        ({**valid, "BIFROST_TEST_VK": "v" * 40}, "sk-bf-"),
        ({**valid, "BIFROST_TEST_VK": "sk-bf-short"}, "sk-bf-"),
    )
    for env, expected in cases:
        result = subprocess.run(
            ["sh", str(BIFROST_DIR / "entrypoint.sh")],
            env={**base_env, **env},
            check=False,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 1, (sorted(env), result.stderr)
        assert expected in result.stderr


def test_bifrost_exposed_paths_match_the_provisioned_routes() -> None:
    # The tls-proxy test above blocks these paths; they must be the ones the
    # gateway really forwards, so the two cannot drift apart.
    from app.services.apisix_system_resources import BIFROST_ROUTE_IDS
    from app.services.bifrost_routes import served_endpoints

    assert set(BIFROST_ROUTE_IDS) == BIFROST_TEST_ROUTE_IDS
    assert {endpoint.split(" ", 1)[1] for endpoint in served_endpoints()} == BIFROST_TEST_EXPOSED_PATHS


def test_service_gets_the_bifrost_route_switch_and_virtual_key() -> None:
    for path in (COMPOSE_FILE, BLUEGREEN_APP_COMPOSE_FILE):
        env = _service_environment(_load_yaml(path), "unibridge-service")
        # On by default, like the Bifrost containers. Both optional: without a
        # usable key the service leaves /api/llm-bi alone and logs why.
        assert env["BIFROST_GATEWAY_ROUTES"] == "${BIFROST_GATEWAY_ROUTES:-true}", path.name
        assert env["BIFROST_TEST_VK"] == "${BIFROST_TEST_VK:-}", path.name


def _bifrost_test_program() -> str:
    script = BIFROST_TEST_SCRIPT_FILE.read_text(encoding="utf-8")
    return script.split("<<'PY'\n", 1)[1].rsplit("\nPY\n", 1)[0]


def test_bifrost_test_script_only_reads_what_the_service_provisions() -> None:
    import ast

    from app.services.apisix_system_resources import (
        BIFROST_NOT_FOUND_ROUTE_ID,
        BIFROST_ROUTE_IDS,
        BIFROST_UPSTREAM_IDS,
    )
    from app.services.consumer_restrictions import DENY_ALL_CONSUMER

    program = _bifrost_test_program()
    assigned = {
        node.targets[0].id: node.value
        for node in ast.parse(program).body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    }
    # unibridge-service is the one writer of these routes: the script sends
    # GETs only (no body, no method override).
    assert "method=" not in program and "data=" not in program
    assert ast.literal_eval(assigned["ROUTE_IDS"]) == BIFROST_ROUTE_IDS
    assert ast.literal_eval(assigned["UPSTREAM_IDS"]) == BIFROST_UPSTREAM_IDS
    assert ast.literal_eval(assigned["NOT_FOUND_ROUTE_ID"]) == BIFROST_NOT_FOUND_ROUTE_ID
    assert ast.literal_eval(assigned["DENY_ALL"]) == DENY_ALL_CONSUMER
    # Admin calls never go through an HTTP(S)_PROXY from .env.
    assert "urllib.request.ProxyHandler({})" in program
    assert "urllib.request.urlopen(" not in program

    # The old write commands point at the switch instead of doing anything.
    for command, value in (("up", "true"), ("down", "false")):
        result = subprocess.run(
            ["bash", str(BIFROST_TEST_SCRIPT_FILE), command],
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            check=False,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 2, command
        assert f"BIFROST_GATEWAY_ROUTES={value}" in result.stderr


def _bifrost_status(routes: dict[str, dict], **env: str) -> str:
    """Run ``bifrost-test.sh status`` against a canned admin API; return its verdict."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            assert self.headers["X-API-KEY"] == "admin-key"
            body = routes.get(self.path.removeprefix("/apisix/admin/"))
            payload = json.dumps({"value": body} if body is not None else {"message": "Key not found"})
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload.encode())

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            ["bash", str(BIFROST_TEST_SCRIPT_FILE), "status"],
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "ENV_FILE": "/nonexistent",
                "APISIX_ADMIN_HOST_URL": f"http://127.0.0.1:{server.server_port}",
                "APISIX_ADMIN_KEY": "admin-key",
                **env,
            },
            check=False,
            text=True,
            capture_output=True,
            timeout=30,
        )
    finally:
        server.shutdown()
        server.server_close()
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


BIFROST_VK = "sk-bf-" + "v" * 32


def _bifrost_installed(vk: str = BIFROST_VK) -> dict[str, dict]:
    from app.services import bifrost_routes
    from app.services.apisix_system_resources import BIFROST_NOT_FOUND_ROUTE_ID, BIFROST_ROUTE_IDS

    routes = {f"upstreams/{upstream_id}": body for upstream_id, body in bifrost_routes.UPSTREAMS.items()}
    for route_id in BIFROST_ROUTE_IDS:
        routes[f"routes/{route_id}"] = bifrost_routes.route(route_id, vk)
    routes[f"routes/{BIFROST_NOT_FOUND_ROUTE_ID}"] = bifrost_routes.not_found_route("on")
    return routes


def test_bifrost_test_status_says_whether_the_routes_match_the_switch() -> None:
    installed = _bifrost_installed()
    only_explanation = {"routes/llm-bi-not-found": installed["routes/llm-bi-not-found"]}

    assert _bifrost_status(installed, BIFROST_TEST_VK=BIFROST_VK).startswith("In step with")
    assert "routes are missing" in _bifrost_status({}, BIFROST_TEST_VK=BIFROST_VK)
    assert "inject a virtual key other than" in _bifrost_status(
        _bifrost_installed("sk-bf-" + "o" * 32), BIFROST_TEST_VK=BIFROST_VK
    )
    assert "still installed" in _bifrost_status(
        installed, BIFROST_GATEWAY_ROUTES="false", BIFROST_TEST_VK=BIFROST_VK
    )
    assert _bifrost_status(only_explanation, BIFROST_GATEWAY_ROUTES="off").startswith("In step with")
    assert "llm-bi-not-found says why" in _bifrost_status(installed, BIFROST_TEST_VK="")
    assert "installs llm-bi-not-found to say why" in _bifrost_status({}, BIFROST_TEST_VK="")
    assert "leaves the routes as they are" in _bifrost_status(
        installed, BIFROST_TEST_VK="sk-bf-" + "x" * 20 + "+y"
    )
    repointed = dict(installed)
    repointed["routes/llm-bi-models"] = {**installed["routes/llm-bi-models"], "upstream_id": "bifrost"}
    assert "point at another upstream" in _bifrost_status(repointed, BIFROST_TEST_VK=BIFROST_VK)


_DEPLOY_CORE_ROUTES = {
    "routes/query-api": {
        "uri": "/api/query/*",
        "plugins": {"proxy-rewrite": {"headers": {"set": {"X-UniBridge-Internal-Proxy": "s"}}}},
    },
    "routes/query-template-write-api": {
        "uri": "/api/query/templates/*",
        "methods": ["PUT", "PATCH", "DELETE"],
        "upstream_id": "unibridge-service",
        "plugins": {"proxy-rewrite": {"headers": {"set": {"X-UniBridge-Internal-Proxy": "s"}}}},
    },
    **{
        f"routes/{route_id}": {
            "plugins": {"proxy-rewrite": {"headers": {"set": {"X-UniBridge-Internal-Proxy": "s"}}}}
        }
        for route_id in ("s3-api", "nas-api", "usages-api")
    },
    "routes/prometheus-api": {"uri": "/api/prometheus/*", "upstream_id": "prometheus"},
    "routes/llm-proxy": {"uri": "/api/llm/*", "upstream_id": "litellm"},
    "routes/llm-admin": {"uri": "/api/llm-admin/*", "upstream_id": "litellm"},
    "routes/llm-metrics": {"uri": "/api/llm/metrics", "upstream_id": "litellm"},
    "routes/llm-messages": {"uri": "/api/llm/v1/messages", "upstream_id": "llm-converter"},
    "routes/llm-responses": {"uri": "/api/llm/v1/responses", "upstream_id": "llm-converter"},
    "routes/llm-models": {"uri": "/api/llm/v1/models", "upstream_id": "llm-converter"},
    "upstreams/litellm": {"scheme": "https", "nodes": {"litellm:4000": 1}},
}


def _deploy_has_core_routes(tmp_path: Path, resources: dict[str, dict], **env: str) -> bool:
    """``apisix_has_core_routes`` against canned admin responses (curl stubbed)."""
    root = tmp_path / "apisix"
    for path, body in {**_DEPLOY_CORE_ROUTES, **resources}.items():
        if body is None:  # absent: the admin API answers 404
            continue
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"value": body}, indent=2), encoding="utf-8")
    shell = f"""
source {shlex.quote(str(DEPLOY_SCRIPT_FILE))}
curl() {{
  local url="${{@: -1}}" code_only="" arg
  for arg in "$@"; do [[ "$arg" == "%{{http_code}}" ]] && code_only=1; done
  local path="${{url#*/apisix/admin/}}"
  local file={shlex.quote(str(root))}/"$path"
  if [[ "$path" == "routes" && -z "$code_only" ]]; then
    local route first=1
    printf '{{"list":['
    for route in {shlex.quote(str(root))}/routes/*; do
      [[ -n "$first" ]] || printf ','
      first=""
      cat "$route"
    done
    printf ']}}'
    return 0
  fi
  if [[ -f "$file" ]]; then
    if [[ -n "$code_only" ]]; then printf '200'; else cat "$file"; fi
    return 0
  fi
  if [[ -n "$code_only" ]]; then printf '404'; return 0; fi
  return 22
}}
if apisix_has_core_routes; then echo yes; else echo no; fi
"""
    result = subprocess.run(
        ["bash", "-c", shell],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "ENV_FILE": "/nonexistent",
            "APISIX_ADMIN_KEY": "admin-key",
            **env,
        },
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip() == "yes"


def test_deploy_holds_the_bifrost_routes_to_the_switch_and_key(tmp_path: Path) -> None:
    installed = _bifrost_installed()
    not_found = {"routes/llm-bi-not-found": installed["routes/llm-bi-not-found"]}
    on = {"BIFROST_TEST_VK": BIFROST_VK}
    off = {"BIFROST_GATEWAY_ROUTES": "false", "BIFROST_TEST_VK": BIFROST_VK}

    def check(resources: dict[str, dict], env: dict[str, str], case: str) -> bool:
        return _deploy_has_core_routes(tmp_path / case, resources, **env)

    assert check(installed, on, "on")
    # Upgrading from scripts/bifrost-test.sh: nothing, or the four routes
    # without the not-found route.
    assert not check({}, on, "on-none")
    assert not check(
        {path: body for path, body in installed.items() if path not in not_found}, on, "script-era"
    )
    # A rotated BIFROST_TEST_VK, and a route re-pointed elsewhere.
    assert not check(_bifrost_installed("sk-bf-" + "o" * 32), on, "rotated")
    repointed = dict(installed)
    repointed["routes/llm-bi-proxy"] = {**installed["routes/llm-bi-proxy"], "upstream_id": "litellm"}
    assert not check(repointed, on, "repointed")
    # llm-bi-proxy as installed before it rewrote the normalized path: once more.
    raw = {**installed, "routes/llm-bi-proxy": _raw_path_rewrite(installed["routes/llm-bi-proxy"])}
    assert not check(raw, on, "raw-path")

    assert check(not_found, off, "off")
    assert not check(installed, off, "off-installed")
    assert not check({}, off, "off-none")

    # No usable key: the service leaves the four routes alone and only puts up
    # the explanation, so that is all there is to hold.
    assert check(not_found, {"BIFROST_TEST_VK": ""}, "skip")
    assert check(installed, {"BIFROST_TEST_VK": "sk-bf-short"}, "skip-installed")
    assert not check({}, {"BIFROST_TEST_VK": ""}, "skip-none")


def _deploy_bifrost_state(**env: str) -> str:
    shell = f"source {shlex.quote(str(DEPLOY_SCRIPT_FILE))}; bifrost_routes_state"
    result = subprocess.run(
        ["bash", "-c", shell],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "ENV_FILE": "/nonexistent", **env},
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_deploy_takes_the_same_virtual_keys_as_the_service() -> None:
    from tests.test_bifrost_routes import VIRTUAL_KEY_CASES

    for value, usable in VIRTUAL_KEY_CASES:
        # A UTF-8 locale must not widen the script's A-Z ranges.
        state = _deploy_bifrost_state(BIFROST_TEST_VK=value, LANG="C.UTF-8", LC_ALL="C.UTF-8")
        assert state == ("on" if usable else "skip"), repr(value)


def _run_vk_drift_warning(container_env: str | None, **env: str) -> str:
    """``warn_bifrost_vk_drift`` with compose/docker stubbed; returns its stderr."""
    container = "" if container_env is None else "cid-bifrost"
    shell = f"""
source {shlex.quote(str(DEPLOY_SCRIPT_FILE))}
compose_infra() {{ [[ "$*" == "ps -q bifrost" ]] && printf '%s\n' {shlex.quote(container)}; }}
docker() {{ [[ "$1" == inspect ]] && printf '%s\n' {shlex.quote(container_env or "")}; }}
warn_bifrost_vk_drift
echo "exit=$?"
"""
    result = subprocess.run(
        ["bash", "-c", shell],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "ENV_FILE": "/nonexistent", **env},
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.stdout.strip() == "exit=0", (result.stdout, result.stderr)
    return result.stderr


def test_deploy_warns_when_bifrost_runs_with_another_virtual_key(tmp_path: Path) -> None:
    current = f"PATH=/usr/bin\nBIFROST_TEST_VK={BIFROST_VK}\nLOG_LEVEL=info"
    stale = "BIFROST_TEST_VK=sk-bf-" + "o" * 32
    on = {"BIFROST_TEST_VK": BIFROST_VK}

    assert _run_vk_drift_warning(current, **on) == ""
    warning = _run_vk_drift_warning(stale, **on)
    assert "runs with another BIFROST_TEST_VK" in warning
    # A command that works from any directory, with the env file the deploy used.
    assert f'-f "{REPO_ROOT}/docker-compose.infra.yml" up -d --wait bifrost' in warning
    assert "--env-file" not in warning
    env_file = tmp_path / "deploy.env"
    env_file.write_text(f"BIFROST_TEST_VK={BIFROST_VK}\n", encoding="utf-8")
    assert f'--env-file "{env_file}"' in _run_vk_drift_warning(stale, ENV_FILE=str(env_file))
    # Never echoes either key.
    assert BIFROST_VK not in warning and "o" * 32 not in warning
    # Nothing to compare: no container, routes switched off, or no usable key.
    assert _run_vk_drift_warning(None, **on) == ""
    assert _run_vk_drift_warning(stale, BIFROST_GATEWAY_ROUTES="false", **on) == ""
    assert _run_vk_drift_warning(stale, BIFROST_TEST_VK="") == ""


def test_deploy_checks_for_vk_drift_before_choosing_whether_to_provision() -> None:
    script = DEPLOY_SCRIPT_FILE.read_text(encoding="utf-8")
    deploy = _shell_function(script, "deploy_color")
    assert deploy.index("warn_bifrost_vk_drift") < deploy.index("apisix_has_core_routes")


@pytest.mark.parametrize(
    "value",
    ["true", "false", "False", "FALSE", "0", "1", "no", "yes", "off", "on", "f", "t", "n", "y"],
)
def test_deploy_reads_the_switch_like_the_service(value: str) -> None:
    from app.config import Settings

    state = _deploy_bifrost_state(BIFROST_GATEWAY_ROUTES=value, BIFROST_TEST_VK=BIFROST_VK)
    enabled = Settings(BIFROST_GATEWAY_ROUTES=value).BIFROST_GATEWAY_ROUTES
    assert state == ("on" if enabled else "off")


# ---------------------------------------------------------------------------
# /api/llm on LiteLLM or Bifrost (LLM_GATEWAY)
# ---------------------------------------------------------------------------


def _llm_on_bifrost(vk: str = BIFROST_VK, master_key: str = "sk-master") -> dict[str, dict]:
    """What boot provisioning leaves in APISIX for /api/llm with LLM_GATEWAY=bifrost."""
    from app.services import bifrost_routes, llm_routes
    from app.services.apisix_system_resources import LLM_NOT_FOUND_ROUTE_ID

    resources = {
        "upstreams/bifrost": bifrost_routes.UPSTREAMS["bifrost"],
        # LiteLLM does not run on Bifrost, so provisioning takes it out.
        "upstreams/litellm": None,
        "routes/llm-admin": None,
        "routes/llm-proxy": llm_routes.bifrost_proxy_route(vk),
        "routes/llm-metrics": llm_routes.bifrost_metrics_route(),
        f"routes/{LLM_NOT_FOUND_ROUTE_ID}": llm_routes.not_found_route(),
    }
    for route_id in llm_routes.CONVERTER_ROUTE_IDS:
        resources[f"routes/{route_id}"] = llm_routes.converter_route(
            route_id, master_key=master_key, virtual_key=vk, on_bifrost=True
        )
    return resources


def _llm_on_litellm(vk: str = BIFROST_VK, master_key: str = "sk-master") -> dict[str, dict]:
    from app.services import llm_routes

    resources = {
        "upstreams/litellm": llm_routes.LITELLM_UPSTREAM,
        "routes/llm-admin": llm_routes.llm_admin_route(),
        "routes/llm-proxy": llm_routes.litellm_proxy_route(master_key),
        "routes/llm-metrics": llm_routes.litellm_metrics_route(master_key),
    }
    for route_id in llm_routes.CONVERTER_ROUTE_IDS:
        resources[f"routes/{route_id}"] = llm_routes.converter_route(
            route_id, master_key=master_key, virtual_key=vk, on_bifrost=False
        )
    return resources


def _raw_path_rewrite(route: dict) -> dict:
    """``route`` as provisioned before routes to Bifrost rewrote the normalized path."""
    plugins = {**route["plugins"]}
    plugins["proxy-rewrite"] = {**plugins["proxy-rewrite"], "use_real_request_uri_unsafe": True}
    return {**route, "plugins": plugins}


def test_deploy_holds_the_llm_routes_to_the_gateway_switch(tmp_path: Path) -> None:
    bifrost = {"LLM_GATEWAY": "bifrost", "BIFROST_TEST_VK": BIFROST_VK}
    litellm = {"LLM_GATEWAY": "litellm", "BIFROST_TEST_VK": BIFROST_VK}

    def check(resources: dict[str, dict], env: dict[str, str], case: str) -> bool:
        # The /api/llm-bi routes stay out of the picture: they hold to their own switch.
        llm_bi = _bifrost_installed()
        return _deploy_has_core_routes(tmp_path / case, {**llm_bi, **resources}, **env)

    on_bifrost = _llm_on_bifrost()
    on_litellm = _llm_on_litellm()
    assert check(on_bifrost, bifrost, "bifrost")
    assert check(on_litellm, litellm, "litellm")
    # The unset switch is LiteLLM.
    assert check(on_litellm, {"BIFROST_TEST_VK": BIFROST_VK}, "default")

    # A flipped switch re-provisions, both ways.
    assert not check(on_litellm, bifrost, "to-bifrost")
    assert not check(on_bifrost, litellm, "to-litellm")
    # Back on LiteLLM, the Bifrost-era 404 route would shadow the catch-all.
    with_not_found = {**on_litellm, "routes/llm-not-found": on_bifrost["routes/llm-not-found"]}
    assert not check(with_not_found, litellm, "litellm-with-not-found")
    # On Bifrost: a rotated key, a missing 404 route, a route left on LiteLLM.
    assert not check(_llm_on_bifrost("sk-bf-" + "o" * 32), bifrost, "rotated")
    missing = {k: v for k, v in on_bifrost.items() if k != "routes/llm-not-found"}
    assert not check(missing, bifrost, "no-not-found")
    stale_metrics = {**on_bifrost, "routes/llm-metrics": on_litellm["routes/llm-metrics"]}
    assert not check(stale_metrics, bifrost, "stale-metrics")
    # A route to Bifrost forwarding the raw path lets %2e%2e reach Bifrost.
    for route_id in ("llm-proxy", "llm-metrics"):
        raw = {**on_bifrost, f"routes/{route_id}": _raw_path_rewrite(on_bifrost[f"routes/{route_id}"])}
        assert not check(raw, bifrost, f"raw-{route_id}")
    # LiteLLM's admin route left over from before re-provisions, which takes it
    # and the upstream out. So does an upstream left behind once no route uses
    # it; one that a route of someone else's still uses stays.
    with_admin = {**on_bifrost, "routes/llm-admin": on_litellm["routes/llm-admin"]}
    assert not check(with_admin, bifrost, "bifrost-with-llm-admin")
    left_upstream = {**on_bifrost, "upstreams/litellm": on_litellm["upstreams/litellm"]}
    assert not check(left_upstream, bifrost, "bifrost-left-upstream")
    used_upstream = {**left_upstream, "routes/custom": {"uri": "/custom/*", "upstream_id": "litellm"}}
    assert check(used_upstream, bifrost, "bifrost-used-upstream")
    # An APISIX service can hold it as well.
    service_listing = {"list": [{"value": {"id": "custom", "upstream_id": "litellm"}}]}
    used_by_service = {**left_upstream, "services": service_listing}
    assert check(used_by_service, bifrost, "bifrost-service-upstream")

    # Bifrost without a usable key: the service leaves /api/llm as it is.
    skip = {"LLM_GATEWAY": "bifrost", "BIFROST_TEST_VK": "sk-bf-short"}
    assert check(on_litellm, skip, "skip-litellm")
    assert check(on_bifrost, skip, "skip-bifrost")


def _deploy_llm_gateway_state(**env: str) -> str:
    shell = f"source {shlex.quote(str(DEPLOY_SCRIPT_FILE))}; llm_gateway_state"
    result = subprocess.run(
        ["bash", "-c", shell],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "ENV_FILE": "/nonexistent", **env},
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize("value", ["", "litellm", "LiteLLM", "bifrost", "Bifrost", " bifrost "])
@pytest.mark.parametrize("vk", [BIFROST_VK, "", "sk-bf-short"])
def test_deploy_reads_the_gateway_switch_like_the_service(value: str, vk: str) -> None:
    from app.config import Settings
    from app.services import llm_routes

    env = {"BIFROST_TEST_VK": vk}
    if value:
        env["LLM_GATEWAY"] = value
    gateway = Settings(**({"LLM_GATEWAY": value} if value else {})).LLM_GATEWAY
    assert _deploy_llm_gateway_state(**env) == llm_routes.gateway_state(gateway, vk)


def test_deploy_warns_about_vk_drift_while_llm_runs_on_bifrost() -> None:
    stale = "BIFROST_TEST_VK=sk-bf-" + "o" * 32
    on_bifrost = {"LLM_GATEWAY": "bifrost", "BIFROST_TEST_VK": BIFROST_VK}
    # Even with the /api/llm-bi routes switched off.
    warning = _run_vk_drift_warning(stale, BIFROST_GATEWAY_ROUTES="false", **on_bifrost)
    assert "runs with another BIFROST_TEST_VK" in warning
    assert _run_vk_drift_warning(stale, BIFROST_GATEWAY_ROUTES="false", BIFROST_TEST_VK=BIFROST_VK) == ""


def test_both_layouts_hand_the_gateway_switch_to_the_service_and_its_converter() -> None:
    for compose_file in (COMPOSE_FILE, BLUEGREEN_APP_COMPOSE_FILE):
        compose = _load_yaml(compose_file)
        # The UI too: the LiteLLM Admin button shows only on litellm.
        for service in ("unibridge-service", "llm-converter", "unibridge-ui"):
            env = _service_environment(compose, service)
            assert env["LLM_GATEWAY"] == "${LLM_GATEWAY:-litellm}", (compose_file.name, service)
    # The second converter sits in front of Bifrost whatever /api/llm runs on.
    for compose_file in (COMPOSE_FILE, BLUEGREEN_INFRA_COMPOSE_FILE):
        env = _service_environment(_load_yaml(compose_file), "llm-converter-bi")
        assert env["LLM_GATEWAY"] == "bifrost", compose_file.name


def test_ui_entrypoint_hands_the_gateway_to_the_page() -> None:
    entrypoint = UI_ENTRYPOINT_FILE.read_text(encoding="utf-8")
    assert 'LLM_GATEWAY: "$(json_escape "$LLM_GATEWAY")"' in entrypoint

    # Read like the service reads it; anything but bifrost is litellm.
    lowercase = _shell_function(entrypoint, "lowercase")
    normalize = re.search(r'^LLM_GATEWAY="\$\(lowercase .*?^fi\n', entrypoint, re.S | re.M)
    assert normalize
    cases = {"": "litellm", "litellm": "litellm", "Bifrost ": "bifrost", " BIFROST": "bifrost", "other": "litellm"}
    for value, expected in cases.items():
        result = subprocess.run(
            ["sh", "-c", lowercase + normalize.group(0) + 'printf %s "$LLM_GATEWAY"'],
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LLM_GATEWAY": value},
            check=False,
            text=True,
            capture_output=True,
        )
        assert result.stdout == expected, repr(value)


# ---------------------------------------------------------------------------
# LiteLLM runs only while /api/llm is on it (litellm/compose-gate.yml)
# ---------------------------------------------------------------------------

LITELLM_GATE_FILE = REPO_ROOT / "litellm" / "compose-gate.yml"


def test_litellm_runs_only_on_litellm() -> None:
    gate = _load_yaml(LITELLM_GATE_FILE)["services"]
    # One base service per value the switch takes: none profiled on litellm, a
    # profile nothing enables on bifrost.
    assert set(gate) == {"litellm", "bifrost"}
    assert not gate["litellm"].get("profiles")
    assert gate["bifrost"]["profiles"]

    for compose_file in (COMPOSE_FILE, BLUEGREEN_INFRA_COMPOSE_FILE):
        services = _load_yaml(compose_file)["services"]
        assert services["litellm"]["extends"] == {
            "file": "./litellm/compose-gate.yml",
            "service": "${LLM_GATEWAY:-litellm}",
        }, compose_file.name
        # LiteLLM's database goes with it (its volume keeps the data); nothing
        # else is gated.
        assert services["litellm-db"]["extends"] == services["litellm"]["extends"], compose_file.name
        assert [name for name, service in services.items() if "extends" in service] == [
            "litellm-db",
            "litellm",
        ], compose_file.name
    # Switching the gate's profile on for every command would run LiteLLM on Bifrost too.
    assert "COMPOSE_PROFILES" not in ENV_EXAMPLE_FILE.read_text(encoding="utf-8")


def _has_docker_compose() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0


def _compose_services(compose_file: Path, **env: str) -> subprocess.CompletedProcess:
    """``docker compose config --services`` with every required secret set and no .env."""
    required = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*):\?", compose_file.read_text(encoding="utf-8")))
    full_env = {key: value for key, value in os.environ.items() if key != "LLM_GATEWAY"}
    full_env.update({name: "x" * 40 for name in required})
    full_env.update(env)
    return subprocess.run(
        [
            "docker", "compose", "--env-file", os.devnull, "-p", "litellm-gate-test",
            "-f", str(compose_file), "config", "--services",
        ],
        env=full_env,
        check=False,
        text=True,
        capture_output=True,
    )


@pytest.mark.skipif(not _has_docker_compose(), reason="needs the docker compose CLI")
def test_compose_leaves_litellm_out_only_under_bifrost() -> None:
    for compose_file in (COMPOSE_FILE, BLUEGREEN_INFRA_COMPOSE_FILE):
        for env, runs in (({}, True), ({"LLM_GATEWAY": "litellm"}, True), ({"LLM_GATEWAY": "bifrost"}, False)):
            result = _compose_services(compose_file, **env)
            assert result.returncode == 0, (compose_file.name, env, result.stderr)
            services = set(result.stdout.split())
            assert ("litellm" in services) is runs, (compose_file.name, env)
            assert ("litellm-db" in services) is runs, (compose_file.name, env)
            # The rest stays, the single stack's dependents of LiteLLM included.
            assert {"bifrost", "llm-converter-bi"} <= services, (compose_file.name, env)
            if compose_file == COMPOSE_FILE:
                assert {"unibridge-service", "llm-converter"} <= services, env
        rejected = _compose_services(compose_file, LLM_GATEWAY="Bifrost")
        assert rejected.returncode != 0, compose_file.name
        assert 'service "Bifrost" not found in ./litellm/compose-gate.yml' in rejected.stderr


def test_single_stack_waits_for_each_gateway_but_requires_neither() -> None:
    services = _load_yaml(COMPOSE_FILE)["services"]
    # A dependency on a service left out of the stack fails every compose
    # command ("depends on undefined service") unless it is optional.
    for name, service in services.items():
        dependency = service.get("depends_on", {}).get("litellm")
        if dependency is not None:
            assert dependency.get("required") is False, name
    # The converter forwards to either gateway (LLM_GATEWAY): healthy, so it
    # does not come up before that name resolves (#95); optional, so one gateway
    # being out or broken does not hold the other's path down.
    assert services["llm-converter"]["depends_on"] == {
        "litellm": {"condition": "service_healthy", "required": False},
        "bifrost": {"condition": "service_healthy", "required": False},
    }


def test_converter_bi_waits_for_a_healthy_bifrost() -> None:
    # service_started let it come up before the bifrost name resolved (#95).
    for compose_file in (COMPOSE_FILE, BLUEGREEN_INFRA_COMPOSE_FILE):
        services = _load_yaml(compose_file)["services"]
        assert services["llm-converter-bi"]["depends_on"] == {
            "bifrost": {"condition": "service_healthy"}
        }, compose_file.name
        assert services["bifrost"]["healthcheck"]["test"], compose_file.name


BIFROST_GATEWAY_ENV = {"LLM_GATEWAY": "bifrost", "BIFROST_TEST_VK": BIFROST_VK}


def _run_litellm_step(
    tmp_path: Path,
    function: str,
    *,
    litellm_running: bool,
    litellm_db_running: bool = True,
    litellm_health: str = "healthy",
    listed_services: tuple[str, ...] = ("keycloak-db", "bifrost"),
    config_fails: bool = False,
    network: bool = True,
    upstream_absent: bool = True,
    converters: dict[str, str | None] | None = None,
    stop_fails: bool = False,
    **env: str,
) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run one of the deploy script's LiteLLM steps with compose, docker and APISIX stubbed.

    ``converters`` maps each running color to what its converter answers for
    ``settings.gateway``; None makes the question fail, as on an image from
    before LLM_GATEWAY. ``listed_services`` is what ``config --services``
    prints. Returns the run and the compose calls it made.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "compose.log"
    converters = converters or {}
    names = " ".join(shlex.quote(f"llm-converter-{color}") for color in converters)
    answers = "\n".join(
        f"        llm-converter-{color}) "
        + ("return 1 ;;" if answer is None else f"printf '%s\\n' {shlex.quote(answer)} ;;")
        for color, answer in converters.items()
    )
    ps_output = "printf 'cid-litellm\\n'" if litellm_running else ":"
    db_output = "printf 'cid-litellm-db\\n'" if litellm_db_running else ":"
    services = " ".join(shlex.quote(service) for service in listed_services)
    config_output = "return 1" if config_fails else f"printf '%s\\n' {services}"
    stop_status = "return 1" if stop_fails else ":"
    absent_status = "return 0" if upstream_absent else "return 1"
    network_status = "return 0" if network else "return 1"
    shell = f"""
source {shlex.quote(str(DEPLOY_SCRIPT_FILE))}
compose_infra() {{
  printf '%s\\n' "$*" >> {shlex.quote(str(log))}
  case "$1" in
    ps)
      case "${{@: -1}}" in
        litellm) {ps_output} ;;
        litellm-db) {db_output} ;;
      esac
      ;;
    config) {config_output} ;;
    stop) {stop_status} ;;
  esac
}}
apisix_absent() {{ {absent_status}; }}
docker() {{
  case "$1" in
    network) {network_status} ;;
    ps) printf '%s\\n' unibridge-infra-apisix-1 {names} ;;
    inspect) printf '%s\\n' {shlex.quote(litellm_health)} ;;
    exec)
      case "$2" in
{answers}
        *) return 1 ;;
      esac
      ;;
    *) return 1 ;;
  esac
}}
{function}
"""
    result = subprocess.run(
        ["bash", "-c", shell],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "ENV_FILE": "/nonexistent", **env},
        check=False,
        text=True,
        capture_output=True,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, calls


def test_deploy_stops_litellm_once_nothing_forwards_to_it(tmp_path: Path) -> None:
    result, calls = _run_litellm_step(
        tmp_path,
        "stop_litellm_when_unused",
        litellm_running=True,
        converters={"blue": "bifrost", "green": "bifrost"},
        **BIFROST_GATEWAY_ENV,
    )

    assert result.returncode == 0, result.stderr
    # Requests already inside LiteLLM get DRAIN_SECONDS (15) before the kill,
    # and its database goes after it.
    assert calls == [
        "ps -q litellm",
        "ps -q litellm-db",
        "config --services",
        "stop -t 15 litellm",
        "stop litellm-db",
    ]
    assert "Stopping LiteLLM" in result.stdout


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        # The standby from before the switch is the rollback target.
        ({"converters": {"blue": "bifrost", "green": "litellm"}}, "llm-converter-green still forwards"),
        # An image from before LLM_GATEWAY forwards to LiteLLM whatever its env says.
        ({"converters": {"green": None}}, "llm-converter-green still forwards"),
        # A route this service does not manage still uses the upstream.
        ({"upstream_absent": False}, "APISIX still has the litellm upstream"),
    ],
)
def test_deploy_keeps_litellm_while_something_still_needs_it(tmp_path: Path, kwargs, reason) -> None:
    result, calls = _run_litellm_step(
        tmp_path, "stop_litellm_when_unused", litellm_running=True, **kwargs, **BIFROST_GATEWAY_ENV
    )

    assert result.returncode == 0, result.stderr
    # LiteLLM keeps its database.
    assert calls == ["ps -q litellm", "ps -q litellm-db"]
    assert reason in result.stdout
    assert result.stderr == ""


def test_deploy_warns_when_the_litellm_kept_for_rollback_is_unhealthy(tmp_path: Path) -> None:
    # Out of the infra check while it is kept, so a dead one would only show at rollback.
    result, calls = _run_litellm_step(
        tmp_path,
        "stop_litellm_when_unused",
        litellm_running=True,
        litellm_health="unhealthy",
        converters={"green": "litellm"},
        **BIFROST_GATEWAY_ENV,
    )

    assert result.returncode == 0, result.stderr
    assert calls == ["ps -q litellm", "ps -q litellm-db"]
    assert "LiteLLM, kept for the reason above, is not healthy (unhealthy)" in result.stderr
    # LiteLLMDbDown is quiet on Bifrost, so the database is checked here too.
    assert "litellm-db, kept for the reason above, is not healthy (unhealthy)" in result.stderr


def test_deploy_warns_when_the_database_kept_for_rollback_is_down(tmp_path: Path) -> None:
    result, calls = _run_litellm_step(
        tmp_path,
        "stop_litellm_when_unused",
        litellm_running=True,
        litellm_db_running=False,
        converters={"green": "litellm"},
        **BIFROST_GATEWAY_ENV,
    )

    assert result.returncode == 0, result.stderr
    assert calls == ["ps -q litellm", "ps -q litellm-db"]
    assert "litellm-db, kept for the reason above, is not healthy (not running)" in result.stderr
    assert "LiteLLM, kept" not in result.stderr


def test_deploy_keeps_litellm_where_compose_does_not_apply_the_gate(tmp_path: Path) -> None:
    # Stopped there, it would fail the infra check of every later deploy.
    result, calls = _run_litellm_step(
        tmp_path,
        "stop_litellm_when_unused",
        litellm_running=True,
        listed_services=("keycloak-db", "litellm-db", "bifrost"),
        **BIFROST_GATEWAY_ENV,
    )

    assert result.returncode == 0, result.stderr
    assert calls == ["ps -q litellm", "ps -q litellm-db", "config --services"]
    assert "does not apply litellm/compose-gate.yml" in result.stderr


def test_deploy_keeps_litellm_when_it_cannot_read_the_infra_compose_file(tmp_path: Path) -> None:
    result, calls = _run_litellm_step(
        tmp_path, "stop_litellm_when_unused", litellm_running=True, config_fails=True, **BIFROST_GATEWAY_ENV
    )

    assert result.returncode == 0, result.stderr
    assert calls == ["ps -q litellm", "ps -q litellm-db", "config --services"]
    assert "could not evaluate the infra compose file" in result.stdout


@pytest.mark.parametrize(
    "env",
    [
        {"LLM_GATEWAY": "litellm", "BIFROST_TEST_VK": BIFROST_VK},
        {"BIFROST_TEST_VK": BIFROST_VK},
        # Without a usable key the service leaves /api/llm where it was, maybe on LiteLLM.
        {"LLM_GATEWAY": "bifrost", "BIFROST_TEST_VK": "sk-bf-short"},
    ],
)
def test_deploy_stops_litellm_only_on_bifrost(tmp_path: Path, env: dict[str, str]) -> None:
    result, calls = _run_litellm_step(tmp_path, "stop_litellm_when_unused", litellm_running=True, **env)

    assert result.returncode == 0, result.stderr
    assert calls == []


def test_deploy_has_nothing_to_stop_when_litellm_and_its_database_are_down(tmp_path: Path) -> None:
    result, calls = _run_litellm_step(
        tmp_path,
        "stop_litellm_when_unused",
        litellm_running=False,
        litellm_db_running=False,
        **BIFROST_GATEWAY_ENV,
    )

    assert result.returncode == 0, result.stderr
    assert calls == ["ps -q litellm", "ps -q litellm-db"]


def test_deploy_stops_a_litellm_db_left_running_on_its_own(tmp_path: Path) -> None:
    result, calls = _run_litellm_step(
        tmp_path, "stop_litellm_when_unused", litellm_running=False, **BIFROST_GATEWAY_ENV
    )

    assert result.returncode == 0, result.stderr
    assert calls == ["ps -q litellm", "ps -q litellm-db", "config --services", "stop litellm-db"]


def test_a_failed_litellm_stop_does_not_fail_the_deploy(tmp_path: Path) -> None:
    result, calls = _run_litellm_step(
        tmp_path, "stop_litellm_when_unused", litellm_running=True, stop_fails=True, **BIFROST_GATEWAY_ENV
    )

    assert result.returncode == 0
    assert "stop -t 15 litellm" in calls
    # Its database is left to it.
    assert "stop litellm-db" not in calls
    assert "could not stop LiteLLM" in result.stderr


@pytest.mark.parametrize("env", [{"LLM_GATEWAY": "litellm"}, {}])
def test_deploy_back_on_litellm_starts_it(tmp_path: Path, env: dict[str, str]) -> None:
    result, calls = _run_litellm_step(
        tmp_path, "start_litellm_if_stopped", litellm_running=False, BIFROST_TEST_VK=BIFROST_VK, **env
    )

    assert result.returncode == 0, result.stderr
    # Both named: a Compose that does not start a gated dependency on its own
    # still brings litellm-db up. Recreated on the current .env and image if
    # they changed while stopped.
    assert calls == ["ps -q litellm", "up -d --wait litellm-db litellm"]


def test_deploy_leaves_a_running_litellm_as_it_is(tmp_path: Path) -> None:
    result, calls = _run_litellm_step(
        tmp_path, "start_litellm_if_stopped", litellm_running=True, LLM_GATEWAY="litellm"
    )

    assert result.returncode == 0, result.stderr
    assert calls == ["ps -q litellm"]


def test_deploy_on_bifrost_does_not_start_litellm(tmp_path: Path) -> None:
    result, calls = _run_litellm_step(
        tmp_path, "start_litellm_if_stopped", litellm_running=False, **BIFROST_GATEWAY_ENV
    )

    assert result.returncode == 0, result.stderr
    assert calls == []


def test_deploy_without_the_network_leaves_litellm_to_the_infra_check(tmp_path: Path) -> None:
    # Starting LiteLLM would bring up a partial infra stack first.
    result, calls = _run_litellm_step(
        tmp_path, "start_litellm_if_stopped", litellm_running=False, network=False, LLM_GATEWAY="litellm"
    )

    assert result.returncode == 0, result.stderr
    assert calls == []


def test_promotion_warns_when_the_target_needs_a_stopped_litellm(tmp_path: Path) -> None:
    function = "warn_if_target_needs_stopped_litellm green"

    on_litellm, _ = _run_litellm_step(
        tmp_path / "stopped", function, litellm_running=False, converters={"green": "litellm"}
    )
    assert "llm-converter-green forwards to LiteLLM, which is not running" in on_litellm.stderr
    # An image from before LLM_GATEWAY forwards to LiteLLM too.
    old_image, _ = _run_litellm_step(
        tmp_path / "old-image", function, litellm_running=False, converters={"green": None}
    )
    assert "which is not running" in old_image.stderr

    running, _ = _run_litellm_step(
        tmp_path / "running", function, litellm_running=True, converters={"green": "litellm"}
    )
    on_bifrost, _ = _run_litellm_step(
        tmp_path / "bifrost", function, litellm_running=False, converters={"green": "bifrost"}
    )
    # A converter that is not running forwards nothing.
    no_converter, _ = _run_litellm_step(tmp_path / "no-converter", function, litellm_running=False)
    for result in (on_litellm, old_image, running, on_bifrost, no_converter):
        assert result.returncode == 0, result.stderr
    assert running.stderr == on_bifrost.stderr == no_converter.stderr == ""


def test_deploy_starts_and_stops_litellm_around_the_switch() -> None:
    script = DEPLOY_SCRIPT_FILE.read_text(encoding="utf-8")
    deploy = _shell_function(script, "deploy_color")
    # Refused before anything happens.
    assert deploy.index("require_usable_llm_gateway") < deploy.index("up_infra")
    start = deploy.index("start_litellm_if_stopped")
    # Up before the infra check that requires it, and before the new color
    # points /api/llm at it.
    assert start < deploy.index("require_existing_infra_healthy")
    assert start < deploy.index("compose_app")
    # Stopped only after the promotion, and after the standby was stopped or disarmed.
    stop = deploy.index("stop_litellm_when_unused")
    assert deploy.index("commit_active_color") < stop
    assert deploy.rindex("compose_app") < stop
    assert "stop_litellm_when_unused" in _shell_function(script, "stop_color")
    # Promotion and rollback stay routing-only: they only warn.
    for name in ("promote_color", "rollback"):
        body = _shell_function(script, name)
        for step in ("start_litellm_if_stopped", "stop_litellm_when_unused", "up_infra"):
            assert step not in body, (name, step)
    assert "warn_if_target_needs_stopped_litellm" in _shell_function(script, "promote_color")


@pytest.mark.parametrize(
    "env, accepted",
    [
        ({}, True),
        ({"LLM_GATEWAY": "litellm"}, True),
        ({"LLM_GATEWAY": "bifrost", "BIFROST_TEST_VK": BIFROST_VK}, True),
        # compose reads the value verbatim.
        ({"LLM_GATEWAY": "Bifrost", "BIFROST_TEST_VK": BIFROST_VK}, False),
        ({"LLM_GATEWAY": "bifrost ", "BIFROST_TEST_VK": BIFROST_VK}, False),
        ({"LLM_GATEWAY": "lite"}, False),
        # Bifrost without a usable key: /api/llm would stay where it was while
        # LiteLLM leaves the stack.
        ({"LLM_GATEWAY": "bifrost", "BIFROST_TEST_VK": "sk-bf-short"}, False),
        ({"LLM_GATEWAY": "bifrost"}, False),
    ],
)
def test_deploy_refuses_a_gateway_it_cannot_run_on(env: dict[str, str], accepted: bool) -> None:
    result = subprocess.run(
        ["bash", "-c", f"source {shlex.quote(str(DEPLOY_SCRIPT_FILE))}; require_usable_llm_gateway"],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "ENV_FILE": "/nonexistent", **env},
        check=False,
        text=True,
        capture_output=True,
    )

    assert (result.returncode == 0) is accepted, result.stderr
    if not accepted:
        assert "ERROR: LLM_GATEWAY" in result.stderr


@pytest.mark.parametrize("running", [True, False])
def test_postgres_restore_puts_back_only_the_consumer_it_stopped(tmp_path: Path, running: bool) -> None:
    # LiteLLM stays stopped on purpose while /api/llm runs on Bifrost, and a
    # restore of litellm-db must not start it.
    dump = tmp_path / "litellm-db.sql.gz"
    with gzip.open(dump, "wt", encoding="utf-8") as handle:
        handle.write("SELECT 1;\n")
    log = tmp_path / "compose.log"
    ps_output = "printf 'cid-litellm\\n'" if running else ":"
    shell = f"""
source {shlex.quote(str(BACKUP_POSTGRES_LIB_FILE))}
infra_compose() {{
  printf '%s\\n' "$*" >> {shlex.quote(str(log))}
  case "$1" in
    ps)
      case "${{@: -1}}" in
        litellm) {ps_output} ;;
        litellm-db) printf 'cid-litellm-db\\n' ;;
      esac
      ;;
    exec) cat >/dev/null ;;
  esac
}}
restore_postgres litellm-db litellm litellm {shlex.quote(str(dump))} litellm <<< 'RESTORE PG'
"""
    result = subprocess.run(
        ["bash", "-c", shell],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "BACKUP_STACK": "bluegreen"},
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    restore = "exec -T litellm-db psql -U litellm -d litellm -v ON_ERROR_STOP=1 --quiet"
    if running:
        assert calls == ["ps -q litellm-db", "ps -q litellm", "stop litellm", restore, "up -d --wait litellm"]
    else:
        assert calls == ["ps -q litellm-db", "ps -q litellm", restore]
        assert "leaving consumer litellm stopped" in result.stdout


def test_postgres_restore_refuses_a_database_that_is_not_running(tmp_path: Path) -> None:
    # litellm-db stays stopped under LLM_GATEWAY=bifrost; say so before the prompt.
    dump = tmp_path / "litellm-db.sql.gz"
    with gzip.open(dump, "wt", encoding="utf-8") as handle:
        handle.write("SELECT 1;\n")
    shell = f"""
source {shlex.quote(str(BACKUP_POSTGRES_LIB_FILE))}
infra_compose() {{ [[ "$1" != "exec" ]] || exit 30; }}
restore_postgres litellm-db litellm litellm {shlex.quote(str(dump))} litellm <<< 'RESTORE PG'
"""
    result = subprocess.run(
        ["bash", "-c", shell],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "BACKUP_STACK": "bluegreen"},
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "litellm-db is not running; start it first" in result.stderr
    assert "This will:" not in result.stderr


@pytest.mark.parametrize(
    "gateway, running, dumped",
    [
        ("bifrost", False, False),
        # Kept for a rollback, so it still changes and is backed up.
        ("bifrost", True, True),
        # Down on LiteLLM is an outage: the dump runs and fails the backup.
        ("litellm", False, True),
        ("litellm", True, True),
    ],
)
def test_backup_skips_litellm_db_only_while_it_is_off_on_bifrost(
    tmp_path: Path, gateway: str, running: bool, dumped: bool
) -> None:
    dest = tmp_path / "snapshots" / "2026-10-13_030000Z"
    dest.mkdir(parents=True)
    out = dest / "litellm-db.sql.gz"
    result = _run_litellm_db_backup(tmp_path, out, gateway=gateway, running=running)

    assert result.returncode == 0, result.stderr
    assert (f"DUMP litellm-db litellm litellm {out}" in result.stdout) is dumped
    if not dumped:
        # No earlier dump to carry forward.
        assert "skipped" in result.stdout
        assert not out.exists()
    assert "backup_litellm_db" in BACKUP_SCRIPT_FILE.read_text(encoding="utf-8")


def _run_litellm_db_backup(
    tmp_path: Path, out: Path, *, gateway: str, running: bool
) -> subprocess.CompletedProcess:
    ps_output = "printf 'cid-litellm-db\\n'" if running else ":"
    shell = f"""
source {shlex.quote(str(BACKUP_POSTGRES_LIB_FILE))}
infra_compose() {{ [[ "$1" == "ps" ]] && {ps_output}; }}
backup_postgres() {{ printf 'DUMP %s\\n' "$*"; }}
backup_litellm_db {shlex.quote(str(out))}
"""
    return subprocess.run(
        ["bash", "-c", shell],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "BACKUP_STACK": "bluegreen",
            "LLM_GATEWAY": gateway,
            "SNAPSHOTS_ROOT": str(tmp_path / "snapshots"),
        },
        check=False,
        text=True,
        capture_output=True,
    )


def _write_snapshot(snapshot: Path, files: dict[str, bytes], *, manifest: dict[str, str] | None) -> None:
    """A snapshot dir as backup.sh leaves it; ``manifest`` maps names to the sha256 it records."""
    snapshot.mkdir(parents=True)
    for name, content in files.items():
        (snapshot / name).write_bytes(content)
    if manifest is not None:
        entries = [{"name": name, "size": 0, "sha256": sha} for name, sha in manifest.items()]
        (snapshot / "manifest.json").write_text(json.dumps({"files": entries}), encoding="utf-8")


def test_backup_carries_the_last_verified_litellm_dump_forward_while_litellm_db_is_off(tmp_path: Path) -> None:
    # Rotation would otherwise drop the last dump RETENTION_DAYS after the switch.
    snapshots = tmp_path / "snapshots"
    dump = gzip.compress(b"-- the dump taken before the switch\n")
    sha = hashlib.sha256(dump).hexdigest()
    _write_snapshot(snapshots / "2026-09-01_030000Z", {"litellm-db.sql.gz": dump}, manifest={"litellm-db.sql.gz": sha})
    # An earlier carry of it: the newest good source, which names the original.
    _write_snapshot(
        snapshots / "2026-10-01_030000Z",
        {"litellm-db.sql.gz": dump, "litellm-db.carried-from": b"2026-09-01_030000Z\n"},
        manifest={"litellm-db.sql.gz": sha},
    )
    # Newer, but not to be trusted: a checksum the manifest does not vouch for,
    # a failed run (no manifest) that left a dump cut off midway, and a directory
    # that is not a snapshot at all.
    _write_snapshot(
        snapshots / "2026-10-05_030000Z", {"litellm-db.sql.gz": dump}, manifest={"litellm-db.sql.gz": "0" * 64}
    )
    _write_snapshot(snapshots / "2026-10-10_030000Z", {"litellm-db.sql.gz": dump[: len(dump) // 2]}, manifest=None)
    # A manifest that vouches for a dump which was already cut off: gzip -t.
    cut = dump[: len(dump) // 2]
    _write_snapshot(
        snapshots / "2026-10-11_030000Z",
        {"litellm-db.sql.gz": cut},
        manifest={"litellm-db.sql.gz": hashlib.sha256(cut).hexdigest()},
    )
    _write_snapshot(
        snapshots / "keep-litellm",
        {"litellm-db.sql.gz": gzip.compress(b"-- kept by hand\n")},
        manifest={"litellm-db.sql.gz": hashlib.sha256(gzip.compress(b"-- kept by hand\n")).hexdigest()},
    )
    dest = snapshots / "2026-10-13_030000Z"
    dest.mkdir()
    out = dest / "litellm-db.sql.gz"

    result = _run_litellm_db_backup(tmp_path, out, gateway="bifrost", running=False)

    assert result.returncode == 0, result.stderr
    assert "DUMP" not in result.stdout
    assert out.read_bytes() == dump
    # Linked rather than copied: snapshot files are never rewritten.
    assert os.path.samefile(out, snapshots / "2026-10-01_030000Z" / "litellm-db.sql.gz")
    assert (dest / "litellm-db.carried-from").read_text(encoding="utf-8") == "2026-09-01_030000Z\n"
    assert "carried the dump taken in 2026-09-01_030000Z forward (from 2026-10-01_030000Z)" in result.stdout


def test_backup_carries_nothing_it_cannot_verify(tmp_path: Path) -> None:
    snapshots = tmp_path / "snapshots"
    _write_snapshot(snapshots / "2026-10-10_030000Z", {"litellm-db.sql.gz": b"not a gzip"}, manifest=None)
    dest = snapshots / "2026-10-13_030000Z"
    dest.mkdir()
    out = dest / "litellm-db.sql.gz"

    result = _run_litellm_db_backup(tmp_path, out, gateway="bifrost", running=False)

    assert result.returncode == 0, result.stderr
    assert not out.exists()
    assert "no verified earlier dump to carry forward, skipped" in result.stdout


def test_backup_says_so_when_it_cannot_verify_earlier_dumps(tmp_path: Path) -> None:
    # Without jq or python3 nothing ever verifies; that must not read as "no dump".
    snapshots = tmp_path / "snapshots"
    dump = gzip.compress(b"-- dump\n")
    _write_snapshot(
        snapshots / "2026-10-01_030000Z",
        {"litellm-db.sql.gz": dump},
        manifest={"litellm-db.sql.gz": hashlib.sha256(dump).hexdigest()},
    )
    dest = snapshots / "2026-10-13_030000Z"
    dest.mkdir()
    shell = f"""
source {shlex.quote(str(BACKUP_POSTGRES_LIB_FILE))}
infra_compose() {{ :; }}
command() {{
  if [[ "$1" == "-v" && ( "$2" == "jq" || "$2" == "python3" ) ]]; then return 1; fi
  builtin command "$@"
}}
backup_litellm_db {shlex.quote(str(dest / "litellm-db.sql.gz"))}
"""
    result = subprocess.run(
        ["bash", "-c", shell],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "BACKUP_STACK": "bluegreen",
            "LLM_GATEWAY": "bifrost",
            "SNAPSHOTS_ROOT": str(snapshots),
        },
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert not (dest / "litellm-db.sql.gz").exists()
    assert "cannot be verified without jq or python3" in result.stderr


def test_litellm_db_alert_is_quiet_while_api_llm_runs_on_bifrost() -> None:
    rules = _load_yaml(PROMETHEUS_RULES_DIR / "unibridge-alerts.yml")
    [alert] = [
        rule
        for group in rules["groups"]
        for rule in group["rules"]
        if rule.get("alert") == "LiteLLMDbDown"
    ]
    expr = " ".join(alert["expr"].split())
    assert expr.startswith('probe_success{job="infra-db-tcp",db="litellm-db"} == 0 ')
    # The active instance's switch, not any instance's: a standby can lag behind.
    assert expr.endswith(
        'unless on() (max(unibridge_llm_gateway{gateway="bifrost"} and on(job, instance) '
        "unibridge_active_instance == 1) == 1)"
    )
