from __future__ import annotations

import json
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE_FILE = REPO_ROOT / ".env.example"
REALM_EXPORT_FILE = REPO_ROOT / "keycloak" / "realm-export.json"
APISIX_CONFIG_FILE = REPO_ROOT / "apisix" / "config.yaml"
KEYCLOAK_ENTRYPOINT_FILE = REPO_ROOT / "keycloak" / "docker-entrypoint.sh"
SERVICE_DOCKERFILE = REPO_ROOT / "unibridge-service" / "Dockerfile"
UI_DOCKERFILE = REPO_ROOT / "unibridge-ui" / "Dockerfile"
CI_WORKFLOW_FILE = REPO_ROOT / ".github" / "workflows" / "ci.yml"
RESTORE_SQLITE_FILE = REPO_ROOT / "backup" / "lib" / "sqlite.sh"
RESTORE_ETCD_FILE = REPO_ROOT / "backup" / "lib" / "etcd.sh"
PROMETHEUS_CONFIG_FILE = REPO_ROOT / "prometheus" / "prometheus.yml"
PROMETHEUS_RULES_DIR = REPO_ROOT / "prometheus" / "rules"
NGINX_CONFIG_FILE = REPO_ROOT / "unibridge-ui" / "nginx.conf"

COMPOSE_SERVICE_LIMITS = {
    "etcd": {"memory": "256m", "cpus": "0.50"},
    "apisix": {"memory": "512m", "cpus": "1.00"},
    "keycloak-db": {"memory": "512m", "cpus": "0.50"},
    "keycloak": {"memory": "1g", "cpus": "1.00"},
    "unibridge-service": {"memory": "512m", "cpus": "1.00"},
    "prometheus": {"memory": "512m", "cpus": "0.50"},
    "litellm-db": {"memory": "512m", "cpus": "0.50"},
    "litellm": {"memory": "512m", "cpus": "1.00"},
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


def test_apisix_enables_edge_protection_plugins() -> None:
    config = _load_yaml(APISIX_CONFIG_FILE)
    plugins = set(config["plugins"])

    assert {"key-auth", "consumer-restriction", "proxy-rewrite", "prometheus"} <= plugins
    assert {"limit-req", "limit-conn", "cors"} <= plugins


def test_keycloak_realm_enforces_login_bruteforce_and_password_policy() -> None:
    realm_export = json.loads(REALM_EXPORT_FILE.read_text(encoding="utf-8"))

    assert realm_export["bruteForceProtected"] is True
    assert realm_export["failureFactor"] <= 6
    assert "length(12)" in realm_export["passwordPolicy"]
    assert "digits" in realm_export["passwordPolicy"]
    assert "specialChars" in realm_export["passwordPolicy"]


def test_keycloak_entrypoint_uses_strict_hostname_in_production() -> None:
    entrypoint = KEYCLOAK_ENTRYPOINT_FILE.read_text(encoding="utf-8")

    assert "--hostname-strict=false" not in entrypoint
    assert "--hostname-strict=true" in entrypoint
    assert "--hostname=${KEYCLOAK_HOSTNAME}" in entrypoint


def test_service_dockerfile_runs_as_non_root_user() -> None:
    dockerfile = SERVICE_DOCKERFILE.read_text(encoding="utf-8")

    assert "USER 1000:1000" in dockerfile
    assert "chown -R 1000:1000 /app" in dockerfile


def test_ui_dockerfile_does_not_disable_strict_ssl_unconditionally() -> None:
    dockerfile = UI_DOCKERFILE.read_text(encoding="utf-8")

    assert "npm config set strict-ssl false &&" not in dockerfile
    assert "NPM_STRICT_SSL" in dockerfile
    assert 'strict-ssl "$NPM_STRICT_SSL"' in dockerfile


def test_ci_pins_actions_and_runs_coverage_and_image_builds() -> None:
    workflow = CI_WORKFLOW_FILE.read_text(encoding="utf-8")
    uses_lines = [
        line.strip().lstrip("- ")
        for line in workflow.splitlines()
        if line.strip().lstrip("- ").startswith("uses:")
    ]

    assert uses_lines
    assert all("@" in line and len(line.rsplit("@", 1)[1]) == 40 for line in uses_lines)
    assert "--cov=app" in workflow
    assert "--cov-fail-under=70" in workflow
    assert "docker/build-push-action" in workflow


def test_unibridge_meta_restore_restarts_apisix_to_clear_consumer_cache() -> None:
    restore_sqlite = RESTORE_SQLITE_FILE.read_text(encoding="utf-8")

    assert "compose restart apisix" in restore_sqlite


def test_etcd_restore_restarts_service_to_replay_consumer_restrictions() -> None:
    restore_etcd = RESTORE_ETCD_FILE.read_text(encoding="utf-8")

    assert "compose restart unibridge-service" in restore_etcd


def test_readme_documents_online_etcd_password_rotation() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "etcdctl auth user passwd root" in readme
    assert "without deleting the etcd volume" in readme


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
