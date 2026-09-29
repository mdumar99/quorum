from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py -> app/ -> api/ -> apps/ -> repo root. The .env lives at the repo root.
# In Docker (P0-2) this file won't exist; that's fine: missing env files are skipped
# and real environment variables are used instead.
# Locally, config.py sits at <repo>/apps/api/app/config.py, so the repo root is
# three levels up. In Docker the file is at /app/app/config.py and has no such
# ancestor. There's no .env file in the image anyway (real env vars come from
# Compose), so we simply skip it.
_PARENTS = Path(__file__).resolve().parents
ROOT_ENV_FILE = _PARENTS[3] / ".env" if len(_PARENTS) > 3 else None

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


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
    # Safe to default: every environment can run at INFO. The URLs above have no safe default.
    log_level: LogLevel = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Built once, then cached. Tests can override this as a FastAPI dependency."""
    return Settings()
