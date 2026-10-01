"""Application settings, loaded from the environment (and `.env` in development)."""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://clearpipe:clearpipe@localhost:5432/clearpipe"
    jwt_secret: SecretStr
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 60

    anthropic_api_key: SecretStr | None = None
    llm_backend: Literal["anthropic", "replay"] = "anthropic"

    cors_origins: list[str] = ["http://localhost:5173"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
