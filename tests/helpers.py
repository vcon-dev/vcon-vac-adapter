"""Test-only helpers. NOT importable from `src/` — see CON-1088: the adapter
must never emit synthetic lawful-basis data on a real conversion path, only
`vcon_builder.add_lawful_basis()` driven by an explicit `LawfulBasisConfig`
may write a `lawful_basis` attachment. `grep -rn synthetic_lawful_basis` must
show hits only under `tests/`.

Formerly `src/vcon_vac_adapter/lawful_basis.py` (moved here CON-1088); no code
under `src/` used it for anything other than a dead fixture-data path, so it
was safe to relocate outright rather than keep a thin re-export.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
    """Return a legacy-shape (`type: "lawful_basis"`) attachment body, kept
    only for tests that still want to exercise a hand-built lawful_basis
    dict shape. Real vCon construction goes through
    `vcon_builder.LawfulBasisConfig` + `vcon_builder.add_lawful_basis()`,
    which writes `purpose: "lawful_basis"` (not `type`) per the current
    lawful_basis extension shape.
    """
    issued = (issued_at or datetime.now(UTC)).isoformat()
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
