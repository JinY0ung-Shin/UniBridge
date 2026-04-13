from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE_FILE = REPO_ROOT / ".env.example"
REALM_EXPORT_FILE = REPO_ROOT / "keycloak" / "realm-export.json"

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
