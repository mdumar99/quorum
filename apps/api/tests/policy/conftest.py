"""Fixtures for the policy tests of #46."""

import pytest


@pytest.fixture(scope="session")
def policy():
    """The current policy, loaded once (it is frozen, so tests can share it)."""
    from app.policy import load_current_policy

    return load_current_policy()
