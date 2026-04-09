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

    # Keycloak OIDC (leave empty to use dev HS256 mode)
    KEYCLOAK_ISSUER_URL: str = ""
    KEYCLOAK_JWKS_URL: str = ""
    KEYCLOAK_JWT_AUDIENCE: str = ""

    model_config = {"env_file": ".env"}


settings = Settings()
