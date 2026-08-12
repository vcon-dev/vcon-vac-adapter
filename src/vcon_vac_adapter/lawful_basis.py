"""Build a lawful_basis attachment body per draft-howe-vcon-lawful-basis.

The lawful_basis attachment is the documented exception that uses `type:
"lawful_basis"` rather than the conventional `purpose`. Add `"lawful_basis"` to
the top-level vCon `extensions[]` whenever this attachment is present.

For synthetic / test data this module emits a `legitimate_interests` basis with
explicit purpose grants for agent-session recording, analysis, and (optionally)
redistribution.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

DEFAULT_PURPOSES = (
    "agent_session_recording",
    "agent_session_analysis",
    "agent_session_redistribution",
)


def synthetic_lawful_basis(
    *,
    data_subjects: list[int],
    purposes: tuple[str, ...] = DEFAULT_PURPOSES,
    issued_at: datetime | None = None,
) -> dict[str, Any]:
    """Return a lawful_basis attachment body for synthetic agent-session data.

    `data_subjects` is a list of party indices. `purposes` is the set of
    purpose_grants. `issued_at` defaults to now (UTC).
    """
    issued = (issued_at or datetime.now(timezone.utc)).isoformat()
    return {
        "lawful_basis": "legitimate_interests",
        "data_subjects": data_subjects,
        "issued_at": issued,
        "expiration": None,
        "purpose_grants": [
            {
                "purpose": p,
                "scope": "session",
            }
            for p in purposes
        ],
        "proof_mechanisms": [
            {
                "type": "external_system",
                "details": {
                    "kind": "synthetic_test_data",
                    "note": "Synthetic agent-session fixture; not real user data.",
                },
            }
        ],
    }
