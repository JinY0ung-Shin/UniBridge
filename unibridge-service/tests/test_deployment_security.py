from __future__ import annotations

import json
import os
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
  elif [[ "$1" == "ps" && "$2" == "-q" ]]; then
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
    case "${{@: -1}}" in
      id-apisix) printf '%s\n' 'running|none' ;;
      id-postgres) printf '%s\n' 'running|healthy' ;;
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
