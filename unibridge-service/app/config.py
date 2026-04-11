from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Base networking — all service URLs are derived from these
    HOST_IP: str = "localhost"
    KEYCLOAK_PORT: int = 8443
    UNIBRIDGE_UI_PORT: int = 3000

    META_DB_URL: str = "sqlite+aiosqlite:///data/meta.db"
    ENCRYPTION_KEY: str = ""
    JWT_SECRET: str = ""
    JWT_ALGORITHM: str = "HS256"
    DEFAULT_QUERY_TIMEOUT: int = 30
    DEFAULT_ROW_LIMIT: int = 10000
    RATE_LIMIT_PER_MINUTE: int = 60
    MAX_CONCURRENT_QUERIES: int = 5
    ENABLE_DEV_TOKEN_ENDPOINT: bool = False
    APISIX_ADMIN_URL: str = "http://apisix:9180"
    APISIX_ADMIN_KEY: str = ""
    PROMETHEUS_URL: str = "http://prometheus:9090"

    # CORS — comma-separated allowed origins (e.g. "http://localhost:3001,https://app.example.com")
    # Auto-derived from HOST_IP:UNIBRIDGE_UI_PORT if empty
    CORS_ALLOWED_ORIGINS: str = ""

    # SSL verification for outgoing requests (Keycloak, etc.)
    SSL_VERIFY: bool = True
    SSL_CA_CERT_PATH: str = ""  # optional CA bundle path

    # MSSQL TrustServerCertificate (set to "no" in production)
    MSSQL_TRUST_SERVER_CERT: str = "no"

    # Keycloak OIDC (leave empty to auto-derive from HOST_IP + KEYCLOAK_PORT + KEYCLOAK_REALM)
    KEYCLOAK_ISSUER_URL: str = ""
    KEYCLOAK_JWKS_URL: str = ""
    KEYCLOAK_JWT_AUDIENCE: str = ""

    # Keycloak Service Account (for user management)
    KEYCLOAK_URL: str = ""
    KEYCLOAK_REALM: str = "apihub"
    KEYCLOAK_SERVICE_CLIENT_ID: str = "apihub-service"
    KEYCLOAK_SERVICE_CLIENT_SECRET: str = ""

    model_config = {"env_file": ".env"}

    @model_validator(mode="after")
    def _derive_urls(self) -> "Settings":
        """Fill in URL fields that weren't explicitly set.

        Keycloak URLs are only derived when KEYCLOAK_JWT_AUDIENCE is set,
        so that dev HS256 mode (all Keycloak fields empty) still works.
        """
        if self.KEYCLOAK_JWT_AUDIENCE:
            if not self.KEYCLOAK_URL:
                self.KEYCLOAK_URL = "https://keycloak:8443"
            if not self.KEYCLOAK_ISSUER_URL:
                self.KEYCLOAK_ISSUER_URL = (
                    f"https://{self.HOST_IP}:{self.KEYCLOAK_PORT}"
                    f"/realms/{self.KEYCLOAK_REALM}"
                )
            if not self.KEYCLOAK_JWKS_URL:
                self.KEYCLOAK_JWKS_URL = (
                    f"https://keycloak:8443"
                    f"/realms/{self.KEYCLOAK_REALM}/protocol/openid-connect/certs"
                )
        if not self.CORS_ALLOWED_ORIGINS:
            self.CORS_ALLOWED_ORIGINS = (
                f"https://{self.HOST_IP}:{self.UNIBRIDGE_UI_PORT}"
            )
        return self


settings = Settings()


def validate_settings() -> None:
    """Validate that critical secrets are set. Called at startup."""
    _INSECURE_DEFAULTS = {
        "change-me-in-production",
        "your-32-byte-fernet-key-here-replace!",
        "change-me-to-a-32-byte-key-here!",
        "change-me-to-a-secure-secret",
        "",
    }
    if settings.ENCRYPTION_KEY in _INSECURE_DEFAULTS:
        raise RuntimeError(
            "ENCRYPTION_KEY is not set or uses an insecure default. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    # Validate ENCRYPTION_KEY produces a usable Fernet key
    try:
        from cryptography.fernet import Fernet
        import base64
        import hashlib
        key = settings.ENCRYPTION_KEY
        if len(key) != 44:
            digest = hashlib.sha256(key.encode()).digest()
            key = base64.urlsafe_b64encode(digest).decode()
        Fernet(key.encode())
    except Exception:
        raise RuntimeError(
            "ENCRYPTION_KEY is invalid. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    # JWT_SECRET is only required when Keycloak is not configured (dev HS256 mode)
    if not settings.KEYCLOAK_ISSUER_URL and settings.JWT_SECRET in _INSECURE_DEFAULTS:
        raise RuntimeError(
            "JWT_SECRET is not set or uses an insecure default. "
            "Set a strong secret or configure KEYCLOAK_ISSUER_URL for RS256 mode."
        )
    # APISIX_ADMIN_KEY is required for gateway management
    if not settings.APISIX_ADMIN_KEY:
        raise RuntimeError(
            "APISIX_ADMIN_KEY is not set. "
            "Set it to your APISIX admin API key in .env."
        )
