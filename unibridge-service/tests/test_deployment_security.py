from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
REALM_EXPORT_FILE = REPO_ROOT / "keycloak" / "realm-export.json"

FORBIDDEN_COMPOSE_PATTERNS = [
    "KC_BOOTSTRAP_ADMIN_PASSWORD=${KC_ADMIN_PASSWORD:-admin}",
    "POSTGRES_PASSWORD=${KC_DB_PASSWORD:-keycloak}",
    "KC_DB_PASSWORD=${KC_DB_PASSWORD:-keycloak}",
    "POSTGRES_PASSWORD=${LITELLM_DB_PASSWORD:-litellm}",
    "DATABASE_URL=postgresql://litellm:${LITELLM_DB_PASSWORD:-litellm}@litellm-db:5432/litellm",
]

REQUIRED_REALM_USERNAMES = {"service-account-apihub-service"}
FORBIDDEN_REALM_USERNAMES = {"apihub-admin", "apihub-dev", "apihub-viewer"}


def test_docker_compose_does_not_contain_insecure_password_fallbacks() -> None:
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
    present_patterns = [
        pattern for pattern in FORBIDDEN_COMPOSE_PATTERNS if pattern in compose_text
    ]

    assert present_patterns == [], (
        "docker-compose.yml still contains insecure password fallback patterns: "
        f"{present_patterns}"
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
