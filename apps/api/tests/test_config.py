import pytest
from pydantic import ValidationError

from app.config import Settings


def test_invalid_log_level_is_rejected():
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql://unused", redis_url="redis://unused", log_level="LOUD")
