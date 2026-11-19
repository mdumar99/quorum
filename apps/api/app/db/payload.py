"""The payload hash of a return (ADR-0004, Decision 4; issue #54, decision 2).

It decides whether a request that reuses an idempotency key is the SAME request (a replay) or
another one (422). It is the SHA-256 of the canonical JSON of the seven validated fields: sorted
keys, the separators "," and ":", ASCII only, the date in ISO form and the amount an integer. The
idempotency key is never part of it. No Unicode normalisation and no trimming: the stored text is
verbatim, so two payloads that are stored differently hash differently.

Changing this form changes what `returns.payload_hash` holds, so old keys would stop matching, until
they expire. ADR-0004 says keys expire but leaves the TTL open ("schema-ticket decisions"); the
intended default of 24 hours was set in the data-model issue (#44), and the setting itself is added
by the API ticket (there is none in app/config.py yet). A golden value in the tests pins the form.
"""

import hashlib
import json

from app.db.models import ReturnFields


def payload_hash(fields: ReturnFields) -> str:
    """SHA-256 hex of the canonical JSON of the seven validated fields, never of the key."""
    canonical = json.dumps(
        fields.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
