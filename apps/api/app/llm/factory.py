"""Build the one LLM client the app uses (issue #48, decision 1)."""

from typing import TYPE_CHECKING

from app.llm.client import LLMClient
from app.llm.errors import LLMConfigError
from app.llm.fake import FakeLLMClient

if TYPE_CHECKING:  # type-only: app.config imports from app.llm, so a real import would loop
    from app.config import Settings


def build_llm_client(settings: "Settings") -> LLMClient:
    """The fake from the settings; `groq` stops the boot until P1-14 builds its adapter.

    `fake_llm_mode` is read only for the fake. The key is never read here: it is not needed by the
    fake, and `groq` fails before any use of it.
    """
    if settings.llm_provider == "fake":
        return FakeLLMClient(
            mode=settings.fake_llm_mode, timeout_seconds=settings.llm_timeout_seconds
        )
    if settings.llm_provider == "groq":
        raise LLMConfigError(
            "LLM_PROVIDER=groq is not available yet: the Groq adapter does not exist until P1-14. "
            "Set LLM_PROVIDER=fake."
        )
    raise LLMConfigError(f"unknown LLM provider: {settings.llm_provider!r}")
