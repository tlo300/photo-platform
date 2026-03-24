from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://app_user:changeme@db:5432/photo"

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # CORS — comma-separated origins, e.g. "http://localhost:3000,http://localhost"
    cors_allowed_origins: str = "http://localhost:3000,http://localhost"

    # Application
    allow_open_registration: bool = False
    max_upload_size_bytes: int = 5_368_709_120  # 5 GiB

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _strip_origins(cls, v: str) -> str:
        return v.strip()

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]


settings = Settings()
