from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.llm.fake import FakeMode

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
        # Keep the input out of the TEXT of a validation error (str and repr, what a traceback or
        # a log line shows): pydantic echoes the whole input dict for a missing field, and that
        # dict holds the API key and the database URLs. This does not cover `.errors()` and
        # `.json()`, which still return the input unless the caller passes include_input=False;
        # the app never calls them on a Settings error.
        hide_input_in_errors=True,
    )

    # Matched case-insensitively: DATABASE_URL -> database_url
    database_url: str
    redis_url: str
    # Safe to default: every environment can run at INFO. The URLs above have no safe default.
    log_level: LogLevel = "INFO"

    # The LLM client (ADR-0003, Decision 6; issue #48).
    # Required, no default: a deployment that forgets it fails at startup instead of silently
    # making fake decisions, which would be the opposite of fail-closed.
    llm_provider: Literal["fake", "groq"]
    # One provider-neutral key. Required unless the provider is fake; an empty or whitespace-only
    # value counts as missing (see the validator below). Never logged: SecretStr hides it.
    llm_api_key: SecretStr | None = None
    # The client's own deadline, in seconds (ADR-0004, Decision 1). Positive and finite.
    llm_timeout_seconds: float = Field(default=20.0, gt=0, allow_inf_nan=False)
    # Read only by the fake adapter; it has no effect for any other provider.
    fake_llm_mode: FakeMode = "ok"

    # The database connection pool (ADR-0002, Decision 2; issue #50).
    # Final defaults: the values measured with the fixed-address Compose network
    # (docs/data-access.md has the numbers).
    db_pool_min_size: int = Field(default=4, ge=1)
    db_pool_max_size: int = Field(default=4, ge=1)
    # How long a checkout waits for a connection (ADR-0004, Decision 1: 5 s, below the LLM call
    # timeout). Positive and finite.
    db_pool_checkout_timeout_seconds: float = Field(default=5.0, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _key_is_required_unless_the_provider_is_fake(self) -> "Settings":
        if self.llm_api_key is not None and not self.llm_api_key.get_secret_value().strip():
            self.llm_api_key = None  # empty or whitespace-only: the same as not set
        if self.llm_provider != "fake" and self.llm_api_key is None:
            raise ValueError(
                "llm_api_key (LLM_API_KEY) is required, and must not be empty, "
                "when llm_provider (LLM_PROVIDER) is not fake"
            )
        return self

    @model_validator(mode="after")
    def _the_min_pool_size_is_not_above_the_max(self) -> "Settings":
        if self.db_pool_min_size > self.db_pool_max_size:
            raise ValueError(
                f"db_pool_min_size ({self.db_pool_min_size}) must not be above "
                f"db_pool_max_size ({self.db_pool_max_size})"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Built once, then cached. Tests can override this as a FastAPI dependency."""
    return Settings()
