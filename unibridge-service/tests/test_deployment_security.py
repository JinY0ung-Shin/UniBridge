from __future__ import annotations

import json
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
UI_ENTRYPOINT_FILE = REPO_ROOT / "unibridge-ui" / "entrypoint.sh"
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
    "litellm": {"memory": "1g", "cpus": "1.00"},
    "unibridge-ui": {"memory": "128m", "cpus": "0.25"},
    "blackbox-exporter": {"memory": "128m", "cpus": "0.25"},
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

    assert "/healthz" in str(ui_healthcheck)
    assert "/-/ready" in str(prometheus_healthcheck)
    assert "/-/healthy" in str(blackbox_healthcheck)


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
    assert (
        "UNIBRIDGE_SERVICE_UPSTREAM=${UNIBRIDGE_SERVICE_UPSTREAM:-unibridge-service-${APP_COLOR}}"
        in ui_env
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


def test_deploy_script_guards_shared_sqlite_and_serializes() -> None:
    script = DEPLOY_SCRIPT_FILE.read_text(encoding="utf-8")

    # Refuses SQLite for blue/green unless explicitly overridden.
    assert "require_shared_db_safe" in script
    assert "ALLOW_SQLITE_BLUEGREEN" in script
    # Serializes mutating runs with a lock.
    assert "flock" in script
    assert "acquire_lock" in script
    # Forces re-provisioning if APISIX lost its core routes (etcd reset).
    assert "apisix_has_core_routes" in script
    # APISIX promotion PUTs retry instead of leaving colors half-switched.
    assert "apisix_put" in script


def test_ui_entrypoint_fails_loudly_on_bad_template() -> None:
    entrypoint = UI_ENTRYPOINT_FILE.read_text(encoding="utf-8")

    assert "set -eu" in entrypoint
    # Guards against leftover placeholders and invalid config before exec'ing nginx.
    assert "__UNIBRIDGE_SERVICE_UPSTREAM__" in entrypoint
    assert "nginx -t" in entrypoint
