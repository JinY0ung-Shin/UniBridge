from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    META_DB_URL: str = "sqlite+aiosqlite:///data/meta.db"
    ENCRYPTION_KEY: str = "your-32-byte-fernet-key-here-replace!"
    JWT_SECRET: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    DEFAULT_QUERY_TIMEOUT: int = 30
    DEFAULT_ROW_LIMIT: int = 10000
    ENABLE_DEV_TOKEN_ENDPOINT: bool = False
    APISIX_ADMIN_URL: str = "http://apisix:9180"
    APISIX_ADMIN_KEY: str = ""
    PROMETHEUS_URL: str = "http://prometheus:9090"

    # CORS — comma-separated allowed origins (e.g. "http://localhost:3001,https://app.example.com")
    CORS_ALLOWED_ORIGINS: str = ""

    # SSL verification for outgoing requests (Keycloak, etc.)
    SSL_VERIFY: bool = True
    SSL_CA_CERT_PATH: str = ""  # optional CA bundle path

    # MSSQL TrustServerCertificate (set to "no" in production)
    MSSQL_TRUST_SERVER_CERT: str = "no"

    # Keycloak OIDC (leave empty to use dev HS256 mode)
    KEYCLOAK_ISSUER_URL: str = ""
    KEYCLOAK_JWKS_URL: str = ""
    KEYCLOAK_JWT_AUDIENCE: str = ""

    # Keycloak Service Account (for user management)
    KEYCLOAK_URL: str = ""
    KEYCLOAK_REALM: str = "apihub"
    KEYCLOAK_SERVICE_CLIENT_ID: str = "apihub-service"
    KEYCLOAK_SERVICE_CLIENT_SECRET: str = ""

    model_config = {"env_file": ".env"}


settings = Settings()
