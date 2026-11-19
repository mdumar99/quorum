"""docs/data-access.md covers the pool settings, the outage behaviour and the numbers (issue #50).

It reads the doc as text: no database and no app code run. Every name the doc must mention comes
from the code, and the default it states for each setting must equal the default in Settings, so the
doc cannot drift from the code. The settings table has the columns Setting, Default, Meaning.
"""

import re

import pytest
from db_helpers import REPO_ROOT

from app.config import Settings
from app.db.pool import POOL_STARTED_EVENT
from app.db.returns import ACTIVE_ORDER_LINE_INDEX, KEY_INDEX

DOC = REPO_ROOT / "docs" / "data-access.md"
POOL_SETTINGS = sorted(name for name in Settings.model_fields if name.startswith("db_pool_"))


@pytest.fixture(scope="module")
def text() -> str:
    assert DOC.is_file(), "docs/data-access.md does not exist yet"
    return DOC.read_text(encoding="utf-8")


@pytest.mark.parametrize("field", POOL_SETTINGS)
def test_every_pool_setting_is_named(text, field):
    assert f"`{field.upper()}`" in text


@pytest.mark.parametrize("field", POOL_SETTINGS)
def test_the_default_the_doc_states_is_the_default_in_settings(text, field):
    rows = {}
    for line in text.splitlines():
        match = re.fullmatch(r"\| `(DB_POOL_[A-Z_]+)` \| ([^|]+?) \|.*", line)
        if match:
            rows[match.group(1)] = match.group(2)
    assert field.upper() in rows, f"{field.upper()} has no row in the settings table"
    assert float(rows[field.upper()]) == float(Settings.model_fields[field].default)


def test_the_log_event_is_named(text):
    assert f"`{POOL_STARTED_EVENT}`" in text


def test_the_doc_says_that_compose_passes_none_of_the_settings(text):
    lines = [line for line in text.splitlines() if "docker-compose.yml" in line]
    assert any("none of" in line.lower() for line in lines)


def test_every_path_the_doc_names_exists(text):
    found = re.findall(r"`((?:apps|docs)/[A-Za-z0-9_./-]+\.(?:py|md|toml))`", text)
    paths = sorted(set(found))
    assert paths, "the doc names no path (a test over an empty list proves nothing)"
    for path in paths:
        assert (REPO_ROOT / path).is_file(), path


# ---- the Compose network section: what an existing stack must do (issue #52) -------------------

COMPOSE_HEADING = "## The Compose network"
OLD_WORDING = ("untested", "may be needed", "not tested")


def compose_section(text: str) -> list[str]:
    """The lines under "## The Compose network", up to the next "## " heading.

    It fails loudly when the heading is missing or the section is empty: a check over an empty
    section would pass without proving anything.
    """
    lines = text.splitlines()
    assert COMPOSE_HEADING in lines, f"the doc has no {COMPOSE_HEADING!r} heading"
    section: list[str] = []
    for line in lines[lines.index(COMPOSE_HEADING) + 1 :]:
        if line.startswith("## "):
            break
        section.append(line)
    assert any(line.strip() for line in section), f"the {COMPOSE_HEADING!r} section has no lines"
    return section


def test_the_doc_says_an_existing_stack_needs_docker_compose_down_once(text):
    """The facts are pinned, not the exact sentence, so the sentence can be reworded."""
    lines = [line for line in compose_section(text) if "existing stack" in line]
    assert len(lines) == 1, (
        f"expected exactly one line about an existing stack in the Compose network section, "
        f"found {len(lines)}"
    )
    line = lines[0]
    assert "`docker compose down`" in line, (
        "the existing-stack line does not name `docker compose down`"
    )
    assert re.search(r"network .+ not found", line), (
        "the existing-stack line does not give the 'network ... not found' error"
    )
    assert re.search(r"never with `-v`", line), "the existing-stack line does not warn against `-v`"
    assert "volume is kept" in line, "the existing-stack line does not say the volume is kept"


def test_the_doc_does_not_call_the_existing_stack_step_untested(text):
    section = "\n".join(compose_section(text)).lower()
    found = [phrase for phrase in OLD_WORDING if phrase in section]
    assert not found, f"the Compose network section still says: {found}"


# ---- The returns repository section: the contract of the write side (issue #54) ----------------

REPOSITORY_HEADING = "## The returns repository"
KNOWN_LIMITS_HEADING = "### Known limits of the repository"
RETURNS_HEADING = "### What each function returns"
TRANSACTION_HEADING = "### The transaction rule"
LOST_HEADING = "### Lost"


def repository_section(text: str) -> list[str]:
    """The lines under "## The returns repository", up to the next "## " heading.

    Loud like `compose_section`: a missing heading or an empty section fails with a message, because
    a check over an empty section would pass without proving anything.
    """
    lines = text.splitlines()
    assert REPOSITORY_HEADING in lines, f"the doc has no {REPOSITORY_HEADING!r} heading"
    section: list[str] = []
    for line in lines[lines.index(REPOSITORY_HEADING) + 1 :]:
        if line.startswith("## "):
            break
        section.append(line)
    assert any(line.strip() for line in section), f"the {REPOSITORY_HEADING!r} section has no lines"
    return section


def subsection(section: list[str], heading: str) -> list[str]:
    """The lines under one "### " heading of the repository section, loud when it is missing."""
    assert heading in section, f"the repository section has no {heading!r} heading"
    lines: list[str] = []
    for line in section[section.index(heading) + 1 :]:
        if line.startswith("### "):
            break
        lines.append(line)
    assert any(line.strip() for line in lines), f"the {heading!r} subsection has no lines"
    return lines


def lines_with(section: list[str], *needles: str) -> list[str]:
    """The lines of the section that contain every needle."""
    return [line for line in section if all(needle in line for needle in needles)]


# (function, the result classes its bullet must name)
RETURNS = [
    ("lookup_key", ["NoKey", "ExpiredKey", "PayloadMismatch", "InFlight", "Retryable", "Replay"]),
    ("insert_return", ["Inserted", "R4Denied", "KeyConflict"]),
    ("finalize_decided", ["Applied", "Lost"]),
    ("finalize_failed", ["Applied", "Lost"]),
    ("retry_failed", ["Retried", "R4Denied", "Lost"]),
    ("get_decision", ["DecisionRow", "None"]),
]


@pytest.mark.parametrize(("name", "classes"), RETURNS, ids=[name for name, _ in RETURNS])
def test_each_function_has_its_own_bullet_that_names_what_it_returns(text, name, classes):
    bullets = [line for line in repository_section(text) if line.startswith(f"- `{name}(")]
    assert len(bullets) == 1, f"expected one bullet for {name}, found {len(bullets)}"
    missing = [cls for cls in classes if f"`{cls}`" not in bullets[0]]
    assert not missing, f"the {name} bullet does not name {missing}"


def test_the_two_unique_index_names_are_the_ones_in_the_code(text):
    section = "\n".join(repository_section(text))
    for name in (ACTIVE_ORDER_LINE_INDEX, KEY_INDEX):
        assert f"`{name}`" in section, f"the repository section does not name {name}"


def test_the_doc_says_the_repository_assumes_read_committed(text):
    found = lines_with(repository_section(text), "assumes", "READ COMMITTED")
    assert found, "no line says that the repository assumes READ COMMITTED"


def test_the_doc_says_the_repository_never_calls_conn_transaction(text):
    found = lines_with(
        repository_section(text), "never calls `conn.transaction()`", "outer transaction", "commits"
    )
    assert found, "no line says why `conn.transaction()` is never used (outer transaction, commits)"


def test_the_doc_says_a_retry_denied_by_r4_advances_attempt_to_n_plus_1(text):
    found = lines_with(repository_section(text), "denied by R4", "`attempt`", "n + 1", "ADR-0004")
    assert found, "no line says that a retry denied by R4 advances `attempt` to n + 1 (ADR-0004)"


def test_the_doc_says_autocommit_is_refused(text):
    found = lines_with(
        repository_section(text), "Autocommit is refused", "`RuntimeError`", "before any SQL"
    )
    assert found, "no line says that autocommit is refused with a RuntimeError before any SQL"


def test_the_doc_lists_the_audit_event_types(text):
    found = lines_with(repository_section(text), "One audit row per attempt")
    assert len(found) == 1, f"expected one audit-rows bullet, found {len(found)}"
    names = ["rules_decided", "llm_decided", "escalated", "r4_denied", "superseded"]
    missing = [name for name in [*names, "failed_<error_type>"] if f"`{name}`" not in found[0]]
    assert not missing, f"the audit-rows bullet does not name {missing}"


def test_the_doc_says_how_to_run_the_integration_tests_safely(text):
    found = lines_with(repository_section(text), "`QUORUM_TEST_DB=throwaway`", "`CI=true`")
    assert found, "no line names QUORUM_TEST_DB=throwaway and CI=true"


def test_the_known_limits_name_the_stuck_processing_and_the_unreachable_key_check(text):
    limits = subsection(repository_section(text), KNOWN_LIMITS_HEADING)
    assert lines_with(limits, "P1-13", "order line"), "no limit names the stuck processing row"
    assert lines_with(limits, "key re-check", "not reachable"), "no limit names the key re-check"


def test_the_repository_section_has_no_private_references(text):
    section = "\n".join(repository_section(text))
    found = re.findall(r"\bD-\d{3}\b|quorum-notes|scratchpad|/tmp/|~/", section)
    assert not found, f"the public section names private things: {found}"


# ---- get_decision: the stored-decision read (issue #56) ------------------------------------------


def test_the_get_decision_bullet_says_what_comes_back_as_stored_and_when_it_is_none(text):
    functions = subsection(repository_section(text), RETURNS_HEADING)
    bullets = [line for line in functions if line.startswith("- `get_decision(")]
    assert len(bullets) == 1, f"expected one get_decision bullet, found {len(bullets)}"
    needles = [
        "every column",
        "unknown id",
        "`processing`",
        "`failed`",
        "unparsed",
        "`rule_results`",
        "the JSON that was stored",
    ]
    missing = [needle for needle in needles if needle not in bullets[0]]
    assert not missing, f"the get_decision bullet does not say {missing}"


def test_the_doc_says_a_read_writes_nothing_and_is_not_refused_on_an_autocommit_connection(text):
    rule = subsection(repository_section(text), TRANSACTION_HEADING)
    found = lines_with(
        rule,
        "`get_decision` runs one `SELECT`",
        "no savepoint",
        "does not refuse an autocommit connection",
    )
    assert found, "no line says that get_decision runs one SELECT and does not refuse autocommit"


def test_the_doc_says_the_decision_is_read_after_the_return_and_gated_on_its_status(text):
    rule = subsection(repository_section(text), TRANSACTION_HEADING)
    found = lines_with(
        rule,
        "Read the return first, then the decision",
        "show the decision only when the status says `decided` or `pending_review`",
        "committed in one transaction",
        "show no decision",
    )
    assert found, "no line says to read the return first and to show the decision only when decided"


def test_the_lost_section_says_the_caller_reads_the_return_again_and_answers_from_its_state(text):
    lost = subsection(repository_section(text), LOST_HEADING)
    found = lines_with(
        lost,
        "After a lost finalize or a lost retry",
        "reads the return again",
        "answers from its state",
    )
    assert found, "the Lost section does not say that the caller reads the return again"
