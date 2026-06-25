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
    APISIX_PROVISION_ON_START: bool = True
    # Gateway read/send timeout (seconds) for the query route. APISIX defaults to
    # 60s, which cuts long queries before the app's own timeout fires; keep this
    # above the app's max req.timeout (300s) so the app wins the race and returns
    # a clean 408. Connect stays short (APISIX_QUERY_ROUTE_CONNECT_TIMEOUT).
    APISIX_QUERY_ROUTE_TIMEOUT: int = 310
    APISIX_QUERY_ROUTE_CONNECT_TIMEOUT: int = 10
    # Default read/send timeout (seconds) applied to user-registered gateway
    # routes that don't set their own. APISIX's built-in default is 60s; this is
    # the seed for the runtime-configurable gateway_route_timeout setting.
    APISIX_GATEWAY_ROUTE_TIMEOUT: int = 60
    APISIX_GATEWAY_ROUTE_CONNECT_TIMEOUT: int = 10
    APISIX_UNIBRIDGE_SERVICE_NODE: str = "unibridge-service:8000"
    APISIX_LLM_CONVERTER_NODE: str = "llm-converter:4001"
    APISIX_BIFROST_NODE: str = "bifrost:8080"
    PROMETHEUS_URL: str = "http://prometheus:9090"
    # Server (host) monitoring: Prometheus scrape job for node_exporter agents,
    # and the file-based service-discovery targets file the service writes from
    # the MonitoredHost registry (must be on a volume shared with Prometheus).
    NODE_EXPORTER_JOB: str = "nodes"
    PROMETHEUS_FILE_SD_PATH: str = "/etc/prometheus/file_sd/nodes.json"
    # Optional global comma-separated mountpoint whitelist for disk-capacity
    # alerts (e.g. "/,/data,/backup"). Hosts with MonitoredHost.disk_mountpoints
    # override this. Empty at both levels → every real (non-pseudo) filesystem is
    # considered, taking the most-full one per host.
    NODE_EXPORTER_DISK_MOUNTPOINTS: str = ""
    BIFROST_VIRTUAL_KEY: str = ""
    # Deprecated compatibility field for old .env files/tests. New deployments
    # should use BIFROST_VIRTUAL_KEY when enabling Bifrost governance.
    LITELLM_MASTER_KEY: str = ""

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

    GRAPHDB_DEFAULT_PORT: int = 7200
    GRAPHDB_MAX_RESPONSE_BYTES: int = 10 * 1024 * 1024  # 10 MiB

    # NAS / local-filesystem read-only provider
    NAS_ALLOWED_ROOTS: str = (
        "/mnt"  # comma-separated absolute prefixes a base_path MUST sit under
    )
    NAS_MAX_DOWNLOAD_BYTES: int = (
        500 * 1024 * 1024
    )  # 500 MiB hard ceiling for proxy streaming
    NAS_MAX_LIST_ENTRIES: int = 5000  # hard cap on entries scanned per listing
    NAS_LIST_DEFAULT_LIMIT: int = 500
    NAS_STREAM_CHUNK_BYTES: int = 1024 * 1024
    NAS_MAX_PATH_BYTES: int = 4096
    NAS_FS_OP_TIMEOUT_SECONDS: float = (
        10.0  # per-op timeout so a hung NFS/FIFO syscall cannot wedge the service
    )

    # S3 / S3-compatible object storage blocking-call isolation
    S3_MAX_WORKERS: int = 8
    S3_OP_TIMEOUT_SECONDS: float = 30.0
    S3_CONNECT_TIMEOUT_SECONDS: float = 5.0
    S3_READ_TIMEOUT_SECONDS: float = 30.0

    APP_VERSION: str = "unknown"

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
            'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
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
            'Generate one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
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
            "APISIX_ADMIN_KEY is not set. Set it to your APISIX admin API key in .env."
        )
