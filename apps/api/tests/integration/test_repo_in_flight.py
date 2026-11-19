"""Criterion 8: until P1-13, a `processing` row of ANY age is in flight (issue #54, decision 3).

No age is read anywhere: the lookup does not decide staleness, and another key on the same order
line takes the R4 path, never a takeover. A crashed attempt therefore blocks its order line until
P1-13 adds the staleness check; this is the recorded cost of the interim rule.
"""

import pytest
from repo_data import (
    HASH_A,
    HASH_B,
    PV,
    audit_rows,
    backdate_created,
    backdate_updated,
    fields_for,
    key_for,
    new_return,
    owner_row,
    run,
    with_conn,
)

from app.db.models import InFlight, R4Denied
from app.db.returns import insert_return, lookup_key

pytestmark = pytest.mark.integration

DAY = 86_400.0


@pytest.mark.parametrize(
    ("age_days", "ttl_days"), [(3, 30), (60, 90), (400, 3650)], ids=["3d", "60d", "400d"]
)
def test_a_processing_row_that_is_days_old_is_still_in_flight(
    marker, owner_conn, app_url, age_days, ttl_days
):
    return_id = new_return(owner_conn, marker, status="processing")
    backdate_created(owner_conn, return_id, age_days * DAY)
    backdate_updated(owner_conn, return_id, age_days * DAY)  # nothing has touched it for days
    result = run(
        with_conn(
            app_url,
            lambda c: lookup_key(c, key_for(marker), HASH_A, ttl_days * DAY),
            commit=False,
        )
    )
    assert isinstance(result, InFlight)
    assert result.row.id == return_id


def test_another_key_on_the_order_line_of_an_old_processing_row_is_r4_not_a_takeover(
    marker, owner_conn, app_url
):
    blocker = new_return(owner_conn, marker, name="a", status="processing")
    backdate_created(owner_conn, blocker, 3 * DAY)
    backdate_updated(owner_conn, blocker, 3 * DAY)
    before = owner_row(owner_conn, blocker)

    async def action(conn):
        return await insert_return(
            conn,
            fields_for(marker),
            idempotency_key=key_for(marker, "b"),
            payload_hash=HASH_B,
            key_generation=1,
            policy_version=PV,
        )

    result = run(with_conn(app_url, action))
    assert isinstance(result, R4Denied)
    assert owner_row(owner_conn, blocker) == before  # still processing, attempt 1, untouched
    assert audit_rows(owner_conn, blocker) == []  # no takeover, so no `abandoned` row either
