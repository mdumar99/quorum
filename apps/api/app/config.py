from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py -> app/ -> api/ -> apps/ -> repo root. The .env lives at the repo root.
# In Docker (P0-2) this file won't exist; that's fine: missing env files are skipped
# and real environment variables are used instead.
ROOT_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_ENV_FILE,
        # The root .env also holds POSTGRES_USER etc. for Compose.
        # Without this, pydantic-settings rejects those as unexpected fields.
        extra="ignore",
    )

    # Matched case-insensitively: DATABASE_URL -> database_url
    database_url: str
    redis_url: str


@lru_cache
def get_settings() -> Settings:
    """Built once, then cached. Tests can override this as a FastAPI dependency."""
    return Settings()
