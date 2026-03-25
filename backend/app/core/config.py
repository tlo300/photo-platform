from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database — app connects via asyncpg; Alembic uses psycopg (psycopg3) as migrator
    database_url: str = "postgresql+asyncpg://app_user:changeme@db:5432/photo"
    database_migrator_url: str = "postgresql+psycopg://migrator:changeme@db:5432/photo"

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # CORS — comma-separated origins, e.g. "http://localhost:3000,http://localhost"
    cors_allowed_origins: str = "http://localhost:3000,http://localhost"

    # Object storage (MinIO locally; any S3-compatible endpoint in production)
    storage_endpoint: str = "minio:9000"
    storage_access_key: str = "changeme"
    storage_secret_key: str = "changeme"
    storage_bucket: str = "photos"
    storage_use_ssl: bool = False

    # JWT
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # Application
    allow_open_registration: bool = False
    max_upload_size_bytes: int = 5_368_709_120  # 5 GiB
    cookie_secure: bool = True  # set False in dev when running over plain HTTP

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _strip_origins(cls, v: str) -> str:
        return v.strip()

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]


settings = Settings()
