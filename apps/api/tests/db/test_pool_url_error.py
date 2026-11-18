"""A malformed DATABASE_URL stops the boot WITHOUT printing the string (issue #50).

psycopg's error for a malformed connection string quotes the string, so a password inside it would
reach the boot traceback. The app turns that error into `DatabaseUrlError`, whose message holds no
part of the string. The marker is made up and never printed: every check is a boolean, so a failing
assertion cannot show it.
"""

import traceback

import psycopg
import pytest
from db_helpers import BAD_URLS, MARKER, make_settings
from psycopg.conninfo import conninfo_to_dict

from app.db.pool import DatabaseUrlError, create_pool

pytestmark = pytest.mark.usefixtures("no_pool_env")
MESSAGE = "DATABASE_URL is not a valid connection string"
# Every malformed form in db_helpers.BAD_URLS; the ids are the form names, never the URLs.
every_bad_url = pytest.mark.parametrize("url", list(BAD_URLS.values()), ids=list(BAD_URLS))


def raised_by_create_pool(url: str) -> BaseException:
    with pytest.raises(Exception) as excinfo:
        create_pool(make_settings(database_url=url))
    return excinfo.value


def texts_of(exc: BaseException) -> dict[str, str]:
    return {
        "str": str(exc),
        "repr": repr(exc),
        "traceback": "".join(traceback.format_exception(exc)),
    }


@every_bad_url
def test_the_driver_does_quote_the_string_so_the_protection_is_needed(url):
    """A precondition: if psycopg stops quoting a form, the tests below prove nothing for it."""
    with pytest.raises(Exception) as excinfo:
        conninfo_to_dict(url)
    assert type(excinfo.value) is psycopg.ProgrammingError
    quoted = MARKER in str(excinfo.value)
    assert quoted, "psycopg no longer quotes the string: the protection tests prove nothing"


@every_bad_url
def test_create_pool_raises_exactly_database_url_error(url):
    assert type(raised_by_create_pool(url)) is DatabaseUrlError


@every_bad_url
def test_the_message_is_fixed_and_holds_no_part_of_the_string(url):
    assert str(raised_by_create_pool(url)) == MESSAGE


@every_bad_url
@pytest.mark.parametrize("which", ["str", "repr", "traceback"])
def test_the_marker_is_in_no_text_of_the_error(url, which):
    text = texts_of(raised_by_create_pool(url))[which]
    whole = MARKER in text
    prefix = MARKER[:8] in text
    assert not whole, f"the password marker is in the {which} of the error"
    assert not prefix, f"the first 8 characters of the marker are in the {which} of the error"


@every_bad_url
def test_the_error_carries_no_context_and_no_cause(url):
    """Raised outside the handler: the original error is not even attached to the new one."""
    exc = raised_by_create_pool(url)
    assert exc.__context__ is None
    assert exc.__cause__ is None
