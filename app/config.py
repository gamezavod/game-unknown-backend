from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    jwt_secret: SecretStr = Field(min_length=32)
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    jwt_issuer: str = "gamezavod"
    jwt_audience: str = "gamezavod-api"
    access_ttl_seconds: int = Field(default=900, gt=0)
    refresh_ttl_seconds: int = Field(default=2592000, gt=0)
    login_max_attempts: int = Field(default=5, gt=0)
    login_window_seconds: int = Field(default=300, gt=0)

    @model_validator(mode="after")
    def validate_database(self) -> "Settings":
        if not self.database_url.startswith("postgresql+psycopg://"):
            raise ValueError("DATABASE_URL must use postgresql+psycopg")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
