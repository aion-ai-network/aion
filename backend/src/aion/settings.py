from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
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
