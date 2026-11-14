"""Connections as the real database roles, shared by the integration tests in this folder."""

import psycopg
import pytest

from app.config import get_settings


@pytest.fixture
def owner_conn(owner_database_url):
    conn = psycopg.connect(owner_database_url)
    try:
        yield conn
    finally:
        conn.rollback()  # every test that writes does so in a transaction it never commits
        conn.close()


@pytest.fixture
def app_conn():
    """The API's own connection: the URL comes from Settings, as in production."""
    conn = psycopg.connect(get_settings().database_url)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
