"""The guard of the committing integration tests (issue #54, criterion 9).

`check_test_database(env)` is a pure function of the environment mapping it is given. These are the
control tests of the guard itself; they need no database and are not marked `integration`.
- QUORUM_TEST_DB=throwaway is accepted; CI=true is accepted (exactly "true", as GitHub sets it).
- Neither set is refused; QUORUM_TEST_DB with any other value is refused; CI with any other value
  is refused ("false", "1", "True" and the empty string included).
- The refusal names the variable but never prints a value from the environment.
- The process environment is never read: a test sets the variable in os.environ and passes an empty
  mapping, and the guard still refuses.
- An explicit QUORUM_TEST_DB with any value other than "throwaway" is refused even when CI=true:
  the explicit value wins over CI (decided by the maintainer).
"""

import pytest
from repo_fixtures import DatabaseRefused, check_test_database

REFUSED = [
    pytest.param({}, id="neither-set"),
    pytest.param({"HOME": "/home/x", "DATABASE_URL": "postgresql://u:p@h:5433/d"}, id="unrelated"),
    pytest.param({"QUORUM_TEST_DB": "dev"}, id="other-value"),
    pytest.param({"QUORUM_TEST_DB": ""}, id="empty-value"),
    pytest.param({"QUORUM_TEST_DB": "Throwaway"}, id="wrong-case"),
    pytest.param({"QUORUM_TEST_DB": "throwaway "}, id="trailing-space"),
    pytest.param({"QUORUM_TEST_DB": "ci"}, id="ci-is-not-a-value"),
    pytest.param({"CI": "false"}, id="ci-false"),
    pytest.param({"CI": "1"}, id="ci-1"),
    pytest.param({"CI": "True"}, id="ci-True"),
    pytest.param({"CI": ""}, id="ci-empty"),
]

ACCEPTED = [
    pytest.param({"QUORUM_TEST_DB": "throwaway"}, id="throwaway"),
    pytest.param({"CI": "true"}, id="ci-true"),
    pytest.param({"QUORUM_TEST_DB": "throwaway", "CI": "true"}, id="both"),
    pytest.param({"QUORUM_TEST_DB": "throwaway", "CI": "false"}, id="throwaway-and-ci-false"),
]


@pytest.mark.parametrize("env", REFUSED)
def test_the_environment_is_refused(env):
    with pytest.raises(DatabaseRefused, match="QUORUM_TEST_DB"):
        check_test_database(env)


REFUSED_EVEN_WITH_CI_TRUE = [
    pytest.param({"QUORUM_TEST_DB": "dev", "CI": "true"}, id="other-value"),
    pytest.param({"QUORUM_TEST_DB": "", "CI": "true"}, id="empty-value"),
    pytest.param({"QUORUM_TEST_DB": "ci", "CI": "true"}, id="ci-is-not-a-value"),
    pytest.param({"QUORUM_TEST_DB": "Throwaway", "CI": "true"}, id="wrong-case"),
    pytest.param({"QUORUM_TEST_DB": "throwaway ", "CI": "true"}, id="trailing-space"),
]


@pytest.mark.parametrize("env", REFUSED_EVEN_WITH_CI_TRUE)
def test_an_explicit_wrong_value_is_refused_even_when_ci_is_true(env):
    with pytest.raises(DatabaseRefused, match="QUORUM_TEST_DB"):
        check_test_database(env)


@pytest.mark.parametrize("env", ACCEPTED)
def test_the_environment_is_accepted(env):
    check_test_database(env)  # must not raise


def test_the_refusal_names_the_variable_and_never_prints_a_value():
    secret = "postgresql://quorum_owner:s3cr3t-value@host:5433/quorum"
    with pytest.raises(DatabaseRefused) as caught:
        check_test_database({"QUORUM_TEST_DB": secret, "DATABASE_URL": secret})
    assert "QUORUM_TEST_DB" in str(caught.value)
    assert "s3cr3t-value" not in str(caught.value)
    assert "postgresql://" not in str(caught.value)


def test_the_process_environment_is_never_read(monkeypatch):
    monkeypatch.setenv("QUORUM_TEST_DB", "throwaway")
    monkeypatch.setenv("CI", "true")
    with pytest.raises(DatabaseRefused):
        check_test_database({})
