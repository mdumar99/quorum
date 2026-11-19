"""Controls on `is_plain_select`, the predicate behind "every statement the read runs is a SELECT"
(issue #56, criterion 4).

It needs no database and is not marked `integration`, like the controls of the guard. The recorder
test of the read will assert `is_plain_select(statement)` for every statement it recorded; a
predicate that is always True, or that misses a form of write, would make that test pass for the
wrong reason, and the red run cannot show it (the stubbed function records nothing). So the
predicate is controlled here, on its own, with the forms a write or a lock can take:
- plain SELECTs are accepted, in any case, with leading whitespace or comments, with a trailing
  semicolon, with a keyword only inside a string literal, a quoted identifier or a comment, and
  the real statement of the read;
- INSERT, UPDATE, DELETE and SAVEPOINT are refused, also with a leading comment;
- the four locking clauses are refused in any case and across line breaks;
- two statements joined by a semicolon, a data-modifying CTE and SELECT ... INTO are refused;
- text that cannot be read to the end, an escape string, and anything that is not a SELECT are
  refused.
The limits are pinned too: a function call with a side effect passes, because the predicate reads
text and not effects (see its docstring).
"""

import pytest
from repo_data import is_plain_select

from app.db.returns import DECISION_BY_RETURN_SQL, RETURN_BY_ID_SQL

ACCEPTED = [
    pytest.param("SELECT a, b FROM public.decisions WHERE return_id = %s", id="plain"),
    pytest.param("select a from t where id = %s", id="lower-case"),
    pytest.param("SeLeCt a FROM t", id="mixed-case"),
    pytest.param("  \n\t SELECT a FROM t", id="leading-whitespace"),
    pytest.param("-- reads one row\nSELECT a FROM t", id="leading-line-comment"),
    pytest.param("/* reads one row */ SELECT a FROM t", id="leading-block-comment"),
    pytest.param("/* a /* nested */ comment */ SELECT a FROM t", id="leading-nested-comment"),
    pytest.param("SELECT a FROM t;", id="one-trailing-semicolon"),
    pytest.param("SELECT a FROM t ;  \n", id="trailing-semicolon-and-space"),
    pytest.param("SELECT a\nFROM t\nWHERE b = %s", id="line-breaks"),
    pytest.param("SELECT 'INSERT INTO t; DELETE FROM t' FROM t", id="keywords-in-a-string"),
    pytest.param("SELECT 'it''s FOR UPDATE' FROM t", id="escaped-quote-in-a-string"),
    pytest.param('SELECT "update", "into" FROM t', id="keywords-as-quoted-identifiers"),
    pytest.param("SELECT $$ x; DELETE FROM t $$ FROM t", id="keywords-in-a-dollar-quote"),
    pytest.param("SELECT $tag$ FOR UPDATE; $tag$ FROM t", id="keywords-in-a-tagged-dollar-quote"),
    pytest.param("SELECT a FROM t /* ; DELETE FROM t */", id="keywords-in-a-trailing-comment"),
    pytest.param("SELECT a FROM t -- ; DELETE FROM t", id="keywords-after-a-line-comment"),
    pytest.param(
        "SELECT updated_at, set_by, into_x FROM t", id="words-that-only-contain-a-keyword"
    ),
    pytest.param("SELECT a$x$ FROM t", id="an-identifier-that-holds-dollar-signs"),
    pytest.param(
        "SELECT 1 $$ x; DELETE $$", id="a-dollar-quote-after-whitespace-hides-its-content"
    ),
    pytest.param(DECISION_BY_RETURN_SQL, id="the-statement-of-the-read"),
    pytest.param(RETURN_BY_ID_SQL, id="the-statement-of-get-return"),
]


@pytest.mark.parametrize("text", ACCEPTED)
def test_a_plain_select_is_accepted(text):
    assert is_plain_select(text) is True


WRITES = [
    pytest.param("INSERT INTO t (a) VALUES (1)", id="insert"),
    pytest.param("insert into t (a) values (1)", id="insert-lower-case"),
    pytest.param("UPDATE t SET a = 1", id="update"),
    pytest.param("update public.returns set updated_at = updated_at", id="update-lower-case"),
    pytest.param("DELETE FROM t", id="delete"),
    pytest.param("SAVEPOINT quorum_sp", id="savepoint"),
    pytest.param("RELEASE SAVEPOINT quorum_sp", id="release-savepoint"),
    pytest.param("ROLLBACK TO SAVEPOINT quorum_sp", id="rollback-to-savepoint"),
    pytest.param("COMMIT", id="commit"),
    pytest.param("/* x */ INSERT INTO t (a) VALUES (1)", id="insert-after-a-comment"),
    pytest.param("-- x\nDELETE FROM t", id="delete-after-a-line-comment"),
    pytest.param("TRUNCATE t", id="truncate"),
    pytest.param("COPY t FROM STDIN", id="copy"),
]


@pytest.mark.parametrize("text", WRITES)
def test_a_write_is_refused(text):
    assert is_plain_select(text) is False


LOCKING = [
    pytest.param("SELECT a FROM t FOR UPDATE", id="for-update"),
    pytest.param("select a from t for update", id="for-update-lower-case"),
    pytest.param("SELECT a FROM t FoR UpDaTe", id="for-update-mixed-case"),
    pytest.param("SELECT a FROM t FOR NO KEY UPDATE", id="for-no-key-update"),
    pytest.param("SELECT a FROM t for no key update", id="for-no-key-update-lower-case"),
    pytest.param("SELECT a FROM t FOR SHARE", id="for-share"),
    pytest.param("select a from t for share", id="for-share-lower-case"),
    pytest.param("SELECT a FROM t FOR KEY SHARE", id="for-key-share"),
    pytest.param("select a from t for key share", id="for-key-share-lower-case"),
    pytest.param("SELECT a FROM t\nFOR\n    UPDATE", id="for-update-across-line-breaks"),
    pytest.param("SELECT a FROM t\nFOR NO\n KEY\tUPDATE", id="for-no-key-update-line-breaks"),
    pytest.param("SELECT a FROM t\nFOR\nKEY\nSHARE", id="for-key-share-across-line-breaks"),
    pytest.param("SELECT a FROM t\nFOR\n  SHARE", id="for-share-across-line-breaks"),
    pytest.param("SELECT a FROM t FOR UPDATE OF t NOWAIT", id="for-update-of-nowait"),
    pytest.param("SELECT a FROM t FOR SHARE SKIP LOCKED", id="for-share-skip-locked"),
    pytest.param("SELECT a FROM t WHERE b = %s FOR UPDATE;", id="for-update-with-a-semicolon"),
    pytest.param("SELECT a FROM t /* lock */ FOR /* it */ UPDATE", id="for-update-with-comments"),
]


@pytest.mark.parametrize("text", LOCKING)
def test_a_locking_clause_is_refused(text):
    assert is_plain_select(text) is False


OTHER_REFUSED = [
    pytest.param("SELECT 1; SELECT 2", id="two-selects"),
    pytest.param("SELECT 1; DELETE FROM t", id="select-then-delete"),
    pytest.param("SELECT 1;\nINSERT INTO t (a) VALUES (1);", id="select-then-insert"),
    pytest.param(";SELECT 1", id="leading-semicolon"),
    pytest.param("SELECT 1;;", id="two-trailing-semicolons"),
    pytest.param("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", id="cte-with-a-delete"),
    pytest.param(
        "WITH i AS (INSERT INTO t (a) VALUES (1) RETURNING a) SELECT a FROM i",
        id="cte-with-an-insert",
    ),
    pytest.param(
        "with u as (update t set a = 1 returning a) select a from u", id="cte-with-an-update"
    ),
    pytest.param("WITH x AS (SELECT 1) SELECT * FROM x", id="a-harmless-cte-is-refused-too"),
    pytest.param("SELECT a INTO new_t FROM t", id="select-into"),
    pytest.param("select a into temporary t2 from t", id="select-into-temporary"),
    pytest.param("SELECT a\nINTO\n  new_t FROM t", id="select-into-line-breaks"),
    pytest.param("", id="empty"),
    pytest.param("   \n ", id="blank"),
    pytest.param("-- only a comment", id="only-a-comment"),
    pytest.param("EXPLAIN ANALYZE SELECT 1", id="explain-analyze"),
    pytest.param("VALUES (1)", id="values"),
    pytest.param("TABLE t", id="table"),
    pytest.param("(SELECT 1)", id="parenthesised-select"),
    pytest.param(
        "SELECT a$x$ FROM t; DELETE FROM t; SELECT b$x$ FROM t", id="glued-tags-hide-a-delete"
    ),
    pytest.param(
        "SELECT a$$ FROM t; DELETE FROM t; SELECT b$$ FROM t",
        id="glued-double-dollars-hide-a-delete",
    ),
    pytest.param(
        "SELECT a$x$, c, d FROM t WHERE e = %s; DELETE FROM t; SELECT f, g, b$x$ FROM t ORDER BY f",
        id="a-delete-hidden-between-glued-tags-in-a-longer-select",
    ),
    pytest.param(
        "SELECT a_$x$ FROM t; DELETE FROM t; SELECT b_$x$ FROM t", id="glued-after-an-underscore"
    ),
    pytest.param(
        "SELECT a1$x$ FROM t; DELETE FROM t; SELECT b1$x$ FROM t", id="glued-after-a-digit"
    ),
    pytest.param(
        "SELECT é$x$ FROM t; DELETE FROM t; SELECT ü$x$ FROM t", id="glued-after-a-non-ascii-letter"
    ),
    pytest.param("SELECT 'a FROM t", id="unterminated-string"),
    pytest.param("SELECT 1 /* never closed", id="unterminated-comment"),
    pytest.param("SELECT $$ never closed FROM t", id="unterminated-dollar-quote"),
    pytest.param('SELECT "never closed FROM t', id="unterminated-quoted-identifier"),
    pytest.param("SELECT E'\\'; DELETE FROM t; --' FROM t", id="escape-string-is-not-read"),
    pytest.param("SELECT e'x' FROM t", id="escape-string-lower-case"),
]


@pytest.mark.parametrize("text", OTHER_REFUSED)
def test_anything_else_is_refused(text):
    assert is_plain_select(text) is False


SIDE_EFFECTS_IT_CANNOT_SEE = [
    pytest.param("SELECT nextval('some_sequence')", id="nextval"),
    pytest.param("SELECT set_config('lock_timeout', '1', false)", id="set-config"),
    pytest.param("SELECT pg_advisory_lock(1)", id="advisory-lock"),
    pytest.param("SELECT a, writes_a_row(b) FROM t", id="a-user-function-that-writes"),
]


@pytest.mark.parametrize("text", SIDE_EFFECTS_IT_CANNOT_SEE)
def test_the_limit_a_function_with_a_side_effect_passes(text):
    """A documented limit, pinned so it cannot be mistaken for a guarantee: the predicate reads
    text. The recorder test never rests on it alone (exact SQL, row counts, xmin, the grants)."""
    assert is_plain_select(text) is True
