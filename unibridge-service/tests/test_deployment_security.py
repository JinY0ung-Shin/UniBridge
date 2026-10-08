from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

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
DEPLOY_SCRIPT_FILE = REPO_ROOT / "scripts" / "deploy-bluegreen.sh"
BIFROST_DIR = REPO_ROOT / "bifrost"
BIFROST_TEST_SCRIPT_FILE = REPO_ROOT / "scripts" / "bifrost-test.sh"
UI_ENTRYPOINT_FILE = REPO_ROOT / "unibridge-ui" / "entrypoint.sh"
UI_DOCKERIGNORE_FILE = REPO_ROOT / "unibridge-ui" / ".dockerignore"
BACKUP_SCRIPT_FILE = REPO_ROOT / "backup" / "backup.sh"
RESTORE_SCRIPT_FILE = REPO_ROOT / "backup" / "restore.sh"
BACKUP_META_LIB_FILE = REPO_ROOT / "backup" / "lib" / "meta.sh"

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
    "bifrost": {"memory": "1g", "cpus": "1.00"},
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
    for route_id, route_var, uri, upstream_id in (
        ("prometheus-api", "prometheus_route", "/api/prometheus/*", "prometheus"),
        ("llm-metrics", "llm_metrics_route", "/api/llm/metrics", "litellm"),
        ("llm-models", "models_route", "/api/llm/v1/models", "llm-converter"),
    ):
        assert f'apisix_get "routes/{route_id}"' in script
        assert (
            f'json_contains_pair "${route_var}" "uri" "{uri}" || return 1' in script
        )
        assert (
            f'json_contains_pair "${route_var}" "upstream_id" "{upstream_id}" '
            "|| return 1" in script
        )
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
        # Bifrost runs next to LiteLLM, so nothing may hide behind a profile: a
        # profiled service is skipped by a plain `up` and by `config --services`,
        # which the blue/green infra health gate iterates.
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


def test_bifrost_test_routes_expose_only_inference_paths() -> None:
    import ast

    from app.routers.gateway import (
        _SYSTEM_ROUTE_URIS,
        _TIMEOUT_OVERRIDE_LABEL,
        _shadowed_system_uri,
    )
    from app.services.consumer_restrictions import DENY_ALL_CONSUMER, IMPLIED_ROUTES

    script = BIFROST_TEST_SCRIPT_FILE.read_text(encoding="utf-8")
    program = script.split("<<'PY'\n", 1)[1].rsplit("\nPY\n", 1)[0]
    assigned = {
        node.targets[0].id: node.value
        for node in ast.parse(program).body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    }

    routes = ast.literal_eval(assigned["ROUTES"])
    assert {route[0] for route in routes} == BIFROST_TEST_ROUTE_IDS
    exposed = [uri for route in routes for uri in route[1]]
    # Exact paths only — no wildcard, so neither Bifrost's UI fallback nor its
    # management API or MCP endpoints are reachable through the gateway.
    assert sorted(exposed) == sorted(BIFROST_TEST_EXPOSED_PATHS)
    for uri in exposed:
        assert "*" not in uri, uri
        assert _shadowed_system_uri(uri, list(_SYSTEM_ROUTE_URIS)) is None, uri
    assert '"regex_uri": ["^/api/llm-bi(.*)", "$1"]' in program
    # Credentials / key selectors a client could aim at Bifrost; key-auth reads
    # the caller's apikey before proxy-rewrite strips these.
    assert ast.literal_eval(assigned["REMOVED_HEADERS"]) == [
        "Authorization",
        "x-api-key",
        "api-key",
        "x-goog-api-key",
        "x-bf-api-key",
        "x-bf-api-key-id",
    ]
    assert '"x-bf-vk": GATEWAY_VK' in program
    assert '"x-bf-lh-consumer": "$consumer_name"' in program
    # Admin calls never go through an HTTP(S)_PROXY from .env.
    assert "urllib.request.ProxyHandler({})" in program
    assert "urllib.request.urlopen(" not in program

    # Values the script mirrors from the app, so they cannot drift apart.
    assert ast.literal_eval(assigned["TIMEOUT_LABEL"]) == _TIMEOUT_OVERRIDE_LABEL
    assert ast.literal_eval(assigned["DENY_ALL"]) == DENY_ALL_CONSUMER
    # Test access stays an explicit grant for regular keys: no existing grant
    # implies these routes. (Master keys, `*`, are whitelisted on every key-auth
    # route by the consumer-restriction reconciler, these included.)
    assert not BIFROST_TEST_ROUTE_IDS & set().union(*IMPLIED_ROUTES.values())
    assert not BIFROST_TEST_ROUTE_IDS & set(IMPLIED_ROUTES)
