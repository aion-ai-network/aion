from functools import lru_cache
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AION_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    db_host: str = "127.0.0.1"
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = "aion"
    db_user: str = "aion_runtime"
    db_password: SecretStr = SecretStr("local_runtime_password")
    db_migration_user: str = "aion_migrator"
    db_migration_password: SecretStr | None = None
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    result_backend: SecretStr = SecretStr("redis://127.0.0.1:6379/1")
    dependency_timeout_seconds: float = Field(default=3, gt=0, le=30)
    scheduler_interval_seconds: float = Field(default=10, ge=1, le=300)
    heartbeat_max_age_seconds: float = Field(default=45, ge=5, le=3600)
    public_origin: str = "http://127.0.0.1:5173"
    session_ttl_seconds: int = Field(default=604800, ge=300, le=2592000)
    auth_attempt_limit: int = Field(default=10, ge=1, le=100)
    auth_window_seconds: int = Field(default=900, ge=1, le=3600)
    rate_limit_prefix: str = "aion"
    storage_backend: Literal["local", "s3"] = "local"
    storage_path: str = ".data/private"
    s3_endpoint: str | None = None
    s3_region: str = "us-east-1"
    s3_bucket: str = "aion-private"
    s3_access_key: SecretStr = SecretStr("")
    s3_secret_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_enabled: bool = False
    llm_max_output_tokens: int = Field(default=2000, ge=100, le=8000)
    llm_daily_budget_usd: float = Field(default=1, ge=0, le=10000)
    llm_input_usd_per_million: float = Field(default=10, ge=0, le=10000)
    llm_output_usd_per_million: float = Field(default=40, ge=0, le=10000)
    stt_enabled: bool = False
    stt_global_daily_seconds: int = Field(default=3600, ge=0, le=864000)
    stt_base_url: str = "https://api.openai.com/v1"
    stt_model: str = ""
    stt_api_key: SecretStr = SecretStr("")
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_bot_username: str = ""
    telegram_webhook_secret: SecretStr = SecretStr("")
    vapid_private_key: SecretStr = SecretStr("")
    vapid_public_key: str = ""
    vapid_subject: str = ""

    @field_validator("public_origin")
    @classmethod
    def valid_origin(cls, value: str) -> str:
        value = value.rstrip("/")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("AION_PUBLIC_ORIGIN must be an HTTP(S) origin without a path")
        return value

    @model_validator(mode="after")
    def production_https(self) -> Self:
        if self.environment == "production" and not self.public_origin.startswith("https://"):
            raise ValueError("Production requires an HTTPS public origin")
        return self

    @property
    def database_url(self) -> URL:
        return self._database_url(self.db_user, self.db_password)

    @property
    def migration_database_url(self) -> URL:
        if self.db_migration_password is None:
            raise ValueError("AION_DB_MIGRATION_PASSWORD is required for migrations")
        return self._database_url(self.db_migration_user, self.db_migration_password)

    def _database_url(self, user: str, password: SecretStr) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=user,
            password=password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
