"""Spec-compliant vCon construction (syntax 0.4.0, draft-ietf-vcon-vcon-core-04).

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

JSON bodies (draft-ietf-vcon-vcon-core-04 §2.3.2): with `encoding: "json"`,
`body` is the JSON value itself (object/array/number/bool/null) — NOT a
`json.dumps()` string. (Only -02 required a string body for JSON content;
-04's CDDL is `body: any`, and the vendored schema already allowed "any
type for encoding=json".) `add_lawful_basis()` below follows this. When
reading a JSON body back, use `json_body()` — it accepts both the -04 raw
value and a legacy `json.dumps`-string body, for compatibility with vCons
built under -02 rules.

`LawfulBasisConfig`, `add_lawful_basis()`, `finalize_vcon()`, and
`json_body()` below are copied from `vcon-dev/vcon-adapter-template`'s
`src/__ADAPTER_PACKAGE__/vcon_builder.py` (`vcon-dev/vcon-adapter-template`,
pull request #1), the reference implementation for the lawful_basis
extension shared across adapters built from that template. Do NOT use
vcon-lib's `Vcon.add_lawful_basis_attachment()` — as of vcon-lib 0.9.6 its
output is missing the required `start` field and the `mediatype` the
vendored schema requires on any attachment with a non-empty body (its
`body` itself, a dict, is fine under -04).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vcon import Vcon

if TYPE_CHECKING:
    from collections.abc import Mapping

log = logging.getLogger(__name__)

VCON_SYNTAX = "0.4.0"

# The six GDPR lawful bases, per draft-howe-vcon-lawful-basis.
VALID_LAWFUL_BASES = frozenset(
    {
        "consent",
        "contract",
        "legal_obligation",
        "vital_interests",
        "public_task",
        "legitimate_interests",
    }
)

# Set once `add_lawful_basis()` has logged its "no lawful basis configured"
# warning, so a long-running adapter emits it only once per process rather
# than once per event.
_warned_no_lawful_basis = False


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


def _walk_dicts(node: Any) -> Any:
    """Yield every dict found anywhere in a nested JSON-like structure
    (the dicts themselves, so callers can mutate them in place).
    """
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_dicts(item)


def finalize_vcon(vcon_dict: dict[str, Any]) -> dict[str, Any]:
    """Strip vcon-lib's empty `meta`/`metadata` placeholders from every
    object in `vcon_dict` (parties, dialog, and anywhere else the library
    defaults them to `{}` instead of omitting the key), in place, and
    return it.

    vcon-lib's `Dialog.__init__` always sets both `self.meta` and
    `self.metadata` to `{}` when neither is passed to `add_dialog()`, and
    its `to_dict()` (like `Party.to_dict()`) only omits attributes that are
    `None` — so every dialog added via `add_dialog()` serializes with two
    empty-object fields, which the core spec disallows ("no empty
    meta/metadata/group/redacted").

    Call this once on the finished vCon dict before serializing or
    delivering it. `WebhookDelivery.deliver()` and `ConserverDelivery.deliver()`
    both call it for you, so an adapter following the documented delivery
    path gets this for free; call it directly if you serialize a vCon
    outside those two paths (e.g. writing straight to local storage).
    """
    for node in _walk_dicts(vcon_dict):
        for key in ("meta", "metadata"):
            if node.get(key) == {}:
                del node[key]
    return vcon_dict


@dataclass(frozen=True)
class LawfulBasisConfig:
    """Configuration for the `lawful_basis` vCon extension.

    Populated either from environment variables (`from_env`), from an
    adapter's YAML `vcon.lawful_basis:` block (`from_yaml`), or a merge of
    both (`resolve`, where environment variables win over YAML per-field).

    Never default `lawful_basis` in code — an unset value means the adapter
    builds vCons with no lawful-basis attachment, and `add_lawful_basis()`
    logs a warning rather than inventing a basis.
    """

    lawful_basis: str | None
    purposes: tuple[str, ...] = ("recording",)
    jurisdiction: str | None = None
    expiration: str | None = None  # ISO 8601
    proof_mechanism: str | None = None  # mechanism_type, e.g. "external_system"
    proof_description: str | None = None

    def __post_init__(self) -> None:
        if self.lawful_basis is not None and self.lawful_basis not in VALID_LAWFUL_BASES:
            raise ValueError(
                f"invalid lawful_basis {self.lawful_basis!r}; must be one of "
                f"{sorted(VALID_LAWFUL_BASES)} or unset"
            )

    @staticmethod
    def _split_purposes(raw: str) -> tuple[str, ...]:
        return tuple(p.strip() for p in raw.split(",") if p.strip())

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LawfulBasisConfig:
        """Build from env vars: LAWFUL_BASIS, LAWFUL_BASIS_PURPOSE (comma-separated),
        LAWFUL_BASIS_JURISDICTION, LAWFUL_BASIS_EXPIRATION,
        LAWFUL_BASIS_PROOF_MECHANISM, LAWFUL_BASIS_PROOF_DESCRIPTION.
        """
        env = os.environ if env is None else env

        purposes_raw = env.get("LAWFUL_BASIS_PURPOSE")
        purposes = cls._split_purposes(purposes_raw) if purposes_raw else cls.purposes

        return cls(
            lawful_basis=env.get("LAWFUL_BASIS") or None,
            purposes=purposes,
            jurisdiction=env.get("LAWFUL_BASIS_JURISDICTION") or None,
            expiration=env.get("LAWFUL_BASIS_EXPIRATION") or None,
            proof_mechanism=env.get("LAWFUL_BASIS_PROOF_MECHANISM") or None,
            proof_description=env.get("LAWFUL_BASIS_PROOF_DESCRIPTION") or None,
        )

    @classmethod
    def from_yaml(cls, block: Mapping[str, Any] | None) -> LawfulBasisConfig:
        """Build from an adapter's YAML `vcon.lawful_basis:` block, e.g.:

        vcon:
          lawful_basis:
            lawful_basis: consent
            purposes: [recording, transcription]
            jurisdiction: US-MA
            expiration: "2026-01-02T12:00:00Z"
            proof_mechanism: external_system
            proof_description: "Verbal consent captured at start of recording"
        """
        block = block or {}
        purposes_raw = block.get("purposes")
        if isinstance(purposes_raw, str):
            purposes = cls._split_purposes(purposes_raw)
        elif purposes_raw:
            purposes = tuple(purposes_raw)
        else:
            purposes = cls.purposes

        return cls(
            lawful_basis=block.get("lawful_basis") or None,
            purposes=purposes,
            jurisdiction=block.get("jurisdiction") or None,
            expiration=block.get("expiration") or None,
            proof_mechanism=block.get("proof_mechanism") or None,
            proof_description=block.get("proof_description") or None,
        )

    @classmethod
    def resolve(
        cls,
        *,
        yaml_block: Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> LawfulBasisConfig:
        """Merge YAML `vcon.lawful_basis` with env vars. Env wins per-field: any
        `LAWFUL_BASIS*` var that is set overrides the corresponding YAML value;
        an unset var falls back to YAML, then to the field default.
        """
        yaml_cfg = cls.from_yaml(yaml_block)
        env = os.environ if env is None else env

        purposes_raw = env.get("LAWFUL_BASIS_PURPOSE")
        purposes = cls._split_purposes(purposes_raw) if purposes_raw else yaml_cfg.purposes

        return cls(
            lawful_basis=env.get("LAWFUL_BASIS") or yaml_cfg.lawful_basis,
            purposes=purposes,
            jurisdiction=env.get("LAWFUL_BASIS_JURISDICTION") or yaml_cfg.jurisdiction,
            expiration=env.get("LAWFUL_BASIS_EXPIRATION") or yaml_cfg.expiration,
            proof_mechanism=env.get("LAWFUL_BASIS_PROOF_MECHANISM") or yaml_cfg.proof_mechanism,
            proof_description=(
                env.get("LAWFUL_BASIS_PROOF_DESCRIPTION") or yaml_cfg.proof_description
            ),
        )


def add_lawful_basis(
    vcon: Vcon,
    cfg: LawfulBasisConfig,
    *,
    granted_at: str,
    party: int = 0,
    dialog: int = 0,
) -> bool:
    """Attach a `lawful_basis` record (draft-howe-vcon-lawful-basis) to `vcon`.

    Returns False and adds nothing if `cfg.lawful_basis` is unset (logging a
    warning once per process). Returns True and appends the attachment
    (plus the `lawful_basis` extension, deduped) otherwise.

    `granted_at` is the ISO 8601 timestamp the basis was established (used
    as the attachment's `start` and each purpose grant's `granted_at`).
    """
    global _warned_no_lawful_basis

    if not cfg.lawful_basis:
        if not _warned_no_lawful_basis:
            log.warning(
                "no lawful_basis configured; building vCons without a "
                "lawful-basis attachment (set LAWFUL_BASIS or the "
                "vcon.lawful_basis config block)"
            )
            _warned_no_lawful_basis = True
        return False

    body: dict[str, Any] = {"lawful_basis": cfg.lawful_basis}
    if cfg.expiration is not None:
        body["expiration"] = cfg.expiration
    body["purpose_grants"] = [
        {"purpose": purpose, "granted": True, "granted_at": granted_at} for purpose in cfg.purposes
    ]
    if cfg.jurisdiction is not None:
        body["jurisdiction"] = cfg.jurisdiction
    if cfg.proof_mechanism is not None:
        proof_mechanism: dict[str, Any] = {"mechanism_type": cfg.proof_mechanism}
        if cfg.proof_description is not None:
            proof_mechanism["description"] = cfg.proof_description
        body["proof_mechanisms"] = [proof_mechanism]

    vcon.vcon_dict["attachments"].append(
        {
            "purpose": "lawful_basis",
            "start": granted_at,
            "party": party,
            "dialog": dialog,
            "encoding": "json",
            # The vendored core schema (tests/schema/vcon_json_schema.json)
            # requires `mediatype` on any attachment with a non-empty `body`.
            "mediatype": "application/json",
            # draft-ietf-vcon-vcon-core-04 §2.3.2: with encoding: "json",
            # `body` is the JSON value itself, not a json.dumps() string.
            "body": body,
        }
    )

    extensions = vcon.vcon_dict.setdefault("extensions", [])
    if "lawful_basis" not in extensions:
        extensions.append("lawful_basis")

    return True


def json_body(attachment: Mapping[str, Any]) -> Any:
    """Return an `encoding: "json"` attachment's (or analysis's) `body` as a
    Python value.

    -04 §2.3.2 says `body` for `encoding: "json"` IS the JSON value, not a
    string — but a reader may still meet vCons built under -02 rules (or by
    an emitter that hasn't caught up), where `body` was `json.dumps()`'d.
    Accept both: if `body` is already a non-string JSON value, return it as
    is; if it's a `str`, `json.loads()` it first.

    Raises `ValueError` if `attachment["encoding"] != "json"`.
    """
    encoding = attachment.get("encoding")
    if encoding != "json":
        raise ValueError(f"json_body() requires encoding: 'json', got {encoding!r}")

    body = attachment.get("body")
    if isinstance(body, str):
        return json.loads(body)
    return body
