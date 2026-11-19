"""Shared helpers for the data-access tests (issue #50).

The facts about the `returns` table are read from db/schema.sql, never retyped.
"""

import re
from datetime import date, datetime
from pathlib import Path
from uuid import UUID

from pydantic import JsonValue

from app.config import Settings

# tests/db/db_helpers.py -> db -> tests -> api -> apps -> repo root
REPO_ROOT = Path(__file__).resolve().parents[4]
API_ROOT = REPO_ROOT / "apps" / "api"
SCHEMA = REPO_ROOT / "db" / "schema.sql"

# An IP literal on a port nothing listens on: no DNS lookup, and the connection is refused at once.
CLOSED_PORT_URL = "postgresql://u:p@127.0.0.1:1/d"
# A syntactically fine URL whose port is not a number: it parses, and only fails when it connects.
WRONG_PORT_URL = "postgresql://u:p@127.0.0.1:notaport/d"
# A made-up password marker. It is never printed: every check on it is a boolean.
MARKER = "Zq7pPw41Mx90"
# Malformed DATABASE_URL values (id -> url). Each is not a connection string, and psycopg's error
# message QUOTES it, marker included (verified read-only: the precondition test checks it again for
# every form), which is why the app must not pass that message on. Forms that psycopg rejects
# without quoting the marker are left out on purpose: a protection test over them would pass even
# with the protection removed. The marker sits in the password position, except in "plain-text".
BAD_URLS = {
    "bracketed-host": f"postgresql://u:{MARKER}@[h/d",
    "bad-percent-encoding": f"postgresql://u:{MARKER}%zz@h/d",
    "unclosed-ipv6": f"postgresql://u:{MARKER}@[::1/d",
    "plain-text": f"{MARKER} is not a url",
}
URL_WITH_MARKER = f"postgresql://u:{MARKER}@127.0.0.1:1/d"
# A timing assertion on a CI runner needs margin. What these assertions prove is that something did
# NOT wait (the boot) or did not hang (the shutdown), not how fast it is.
TIMING_MARGIN_S = 3.0

SQL_TYPES: dict[str, type] = {
    "uuid": UUID,
    "text": str,
    "bigint": int,
    "integer": int,
    "date": date,
    "timestamp with time zone": datetime,
}


def returns_columns() -> list[tuple[str, type, bool]]:
    """(name, Python type, nullable) of public.returns, in table order, from db/schema.sql."""
    assert SCHEMA.is_file(), "db/schema.sql does not exist"
    match = re.search(r"CREATE TABLE public\.returns \((.*?)\n\);", SCHEMA.read_text(), re.S)
    assert match is not None, "CREATE TABLE public.returns not found in db/schema.sql"
    columns = []
    for line in match.group(1).splitlines():
        m = re.fullmatch(r"    ([a-z_]+) (.+?),?", line)
        if m is None:  # a CONSTRAINT line
            continue
        rest = m.group(2)
        sql_type = re.split(r" DEFAULT | NOT NULL", rest)[0]
        assert sql_type in SQL_TYPES, f"a returns column has a type this test does not map: {line}"
        columns.append((m.group(1), SQL_TYPES[sql_type], " NOT NULL" not in rest))
    return columns


# The Python types of the `decisions` columns (issue #56). A separate map from SQL_TYPES, so the
# check on `returns` is not loosened: `jsonb` is the JSON as stored, `text[]` a list of strings.
DECISION_SQL_TYPES: dict[str, object] = {
    "uuid": UUID,
    "text": str,
    "integer": int,
    "boolean": bool,
    "jsonb": JsonValue,
    "text[]": list[str],
}


def decisions_columns() -> list[tuple[str, object, bool]]:
    """(name, Python type, nullable) of public.decisions, in table order, from db/schema.sql."""
    assert SCHEMA.is_file(), "db/schema.sql does not exist"
    match = re.search(r"CREATE TABLE public\.decisions \((.*?)\n\);", SCHEMA.read_text(), re.S)
    assert match is not None, "CREATE TABLE public.decisions not found in db/schema.sql"
    columns = []
    for line in match.group(1).splitlines():
        if not line.strip():  # the capture starts right after "(", so the first line is empty
            continue
        m = re.fullmatch(r"    ([a-z_0-9]+) (.+?),?", line)  # digits too: raw_output_sha256
        if m is None:
            # Not skipped silently: a column this pattern missed would drop out of every check.
            assert line.startswith("    CONSTRAINT "), f"unrecognised line in decisions: {line}"
            continue
        rest = m.group(2)
        sql_type = re.split(r" DEFAULT | NOT NULL", rest)[0]
        assert sql_type in DECISION_SQL_TYPES, (
            f"a decisions column has a type this test does not map: {line}"
        )
        columns.append((m.group(1), DECISION_SQL_TYPES[sql_type], " NOT NULL" not in rest))
    return columns


def make_settings(**kwargs) -> Settings:
    """Settings for a closed port, without a root .env and without the process environment."""
    base = {"database_url": CLOSED_PORT_URL, "redis_url": "redis://unused", "llm_provider": "fake"}
    return Settings(_env_file=None, **{**base, **kwargs})
