"""Spec-compliant vCon construction (syntax 0.4.0, draft-ietf-vcon-vcon-core-02).

Adapters should call the helpers in this module rather than constructing vCon
fields directly. We route everything through the `vcon` library (>= 0.9.4)
which is spec-correct out of the box for `add_party`, `add_dialog`,
`add_attachment` (with `encoding="json"` + `party`/`dialog`), `add_analysis`
(enforces required `vendor`), and `add_tag` (writes `party`/`dialog` since 0.9.3).

Why a thin wrapper at all (instead of using `Vcon` directly):
- `Vcon.build_new()` does NOT set `vcon` syntax param — we set "0.4.0".
- `build_new()` leaves empty `group: []` and `redacted: {}` — we drop them.
- `subject` has no setter on the class — we write via vcon_dict["subject"].
- Convenience: `sha512_b64url()` for external-media content_hash formatting.
"""

from __future__ import annotations

import hashlib
from base64 import urlsafe_b64encode
from typing import Any

from vcon import Vcon

VCON_SYNTAX = "0.4.0"


def new_vcon(
    *,
    subject: str | None = None,
    extensions: list[str] | None = None,
) -> Vcon:
    """Create a new vCon with the spec-correct syntax param and cleanup."""
    v = Vcon.build_new()
    v.vcon_dict["vcon"] = VCON_SYNTAX

    if subject is not None:
        v.vcon_dict["subject"] = subject

    # build_new() leaves these as empty placeholders; spec reserves them
    # and they should only appear when used.
    v.vcon_dict.pop("group", None)
    v.vcon_dict.pop("redacted", None)

    if extensions:
        v.vcon_dict["extensions"] = list(extensions)

    return v


def sha512_b64url(data: bytes) -> str:
    """Return content_hash formatted as `sha512-<base64url-of-digest>` per spec."""
    digest = hashlib.sha512(data).digest()
    return "sha512-" + urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def external_media_url(
    *,
    url: str,
    content: bytes,
    mediatype: str,
) -> dict[str, Any]:
    """Build the dialog dict body for external-media mode."""
    return {
        "type": "recording",
        "url": url,
        "mediatype": mediatype,
        "content_hash": sha512_b64url(content),
    }
