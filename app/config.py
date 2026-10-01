"""Application settings, loaded from the environment (and `.env` in development)."""

from functools import lru_cache
from pathlib import Path
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
    # Budget controls (see llm/client.py): every call is logged, identical requests are cached, and
    # an optional daily USD cap stops further calls.
    llm_cost_log: Path = Path("costs.jsonl")
    llm_cache_enabled: bool = True
    llm_cache_dir: Path = Path(".cache/llm")
    llm_daily_budget_usd: float | None = None

    # JSONL of golden cases the "replay" backend answers from (CI / offline demos only).
    llm_replay_file: Path = Path("evals/query_golden.jsonl")

    # Ingest worker: runs inside the API process, or standalone via `python -m pipeline.worker`.
    worker_enabled: bool = True
    worker_poll_seconds: float = 1.0

    cors_origins: list[str] = ["http://localhost:5173"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
