"""Private, versioned bus sidebands; public projections must never consume them."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from .audience_manifest import FrozenAudience
    from .declarations import Message
    from .wake import NoWakeDecision, WakeDecision

PRIVATE_WIRE_FIELD: Final = "_agent_comms_private_v1"
_PRIVATE_WIRE_PREFIX: Final = "_agent_comms_private"
INITIAL_CODEC: Final = "audience-v1"
_DECISION_DOMAIN: Final = b"agent-comms:cohort-decisions:v1\0"
_HEX32: Final = re.compile(r"[0-9a-f]{32}\Z")
_HEX64: Final = re.compile(r"[0-9a-f]{64}\Z")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def public_envelope_digest(record: Mapping[str, object]) -> str:
    """Bind every public wire field without including the private sideband."""
    timestamp = record.get("ts")
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(timestamp)
    ):
        raise ValueError("A keyed wire timestamp must be finite.")
    if has_private_wire_fields(record):
        raise ValueError("Digest input must be a public envelope.")
    return hashlib.sha256(_canonical(record)).hexdigest()


def unique_wire_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject ambiguous JSON objects, including nested receipt keys."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate bus object key.")
        result[key] = value
    return result


def has_private_wire_fields(record: Mapping[str, object]) -> bool:
    """Identify reserved private keys without inspecting their values."""
    return any(isinstance(key, str) and key.startswith(_PRIVATE_WIRE_PREFIX) for key in record)


def reject_private_wire_fields(record: Mapping[str, object]) -> None:
    """Fail closed before an untyped raw wire record enters a public export."""
    if has_private_wire_fields(record):
        raise ValueError("Bus-private wire fields cannot be exported.")


def stable_thread_lookup(created_at: float) -> str:
    """Legacy-compatible immutable creation identity; duplicate timestamps fail at capture.

    The registry preserves Thread.created_at across renames and restarts. Its
    canonical name, aliases, mutable tags, PID, and session attachment are NOT
    lookup identities. A legacy unknown creation time (zero) cannot be trusted.
    """
    if isinstance(created_at, bool) or not isinstance(created_at, (float, int)):
        raise ValueError("Thread creation identity is invalid.")
    if not math.isfinite(created_at) or created_at <= 0:
        raise ValueError("Thread creation identity is unavailable.")
    identity = f"agent-comms:thread-creation:v1\0{float(created_at)!r}".encode()
    return hashlib.sha256(identity).hexdigest()[:32]


def _decision_wire(decision: WakeDecision | NoWakeDecision) -> dict[str, str]:
    from .wake import NoWakeDecision, WakeDecision

    if type(decision) is NoWakeDecision:
        return {"recipient_lookup": decision.recipient, "no_wake": decision.reason.value}
    if type(decision) is WakeDecision:
        return {
            "recipient_lookup": decision.recipient,
            "audience": decision.audience.value,
            "wake_mode": decision.wake_mode.value,
        }
    raise TypeError("Only typed, pure full-cohort decisions may be committed.")


def decisions_wire(decisions: tuple[WakeDecision | NoWakeDecision, ...]) -> list[dict[str, str]]:
    return [_decision_wire(decision) for decision in decisions]


def decisions_digest(decisions: list[dict[str, str]]) -> str:
    from .audience_manifest import MAX_AUDIENCE_BYTES

    encoded = _canonical(decisions)
    if len(encoded) > MAX_AUDIENCE_BYTES:
        raise ValueError("Private decision set exceeds its byte bound.")
    return hashlib.sha256(_DECISION_DOMAIN + encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class CommittedInitial:
    """Bus-reader result, never a caller-supplied SQLite authority."""

    wire_root_id: str
    message: Message
    audience: FrozenAudience
    decisions: tuple[WakeDecision | NoWakeDecision, ...]
    decisions_digest: str
    control: str
    resolver_version: str
    policy_version: str
    manifest_codec: str = INITIAL_CODEC


def initial_sideband(
    wire_root_id: str,
    message: Message,
    audience: FrozenAudience,
    decisions: tuple[WakeDecision | NoWakeDecision, ...],
    *,
    control: str,
) -> dict[str, object]:
    """Serialize once at the committed-bus boundary; no independent sidecar."""
    from .coordination import POLICY_VERSION, RESOLVER_VERSION

    if len(decisions) != len(audience.recipients):
        raise ValueError("Every frozen recipient requires a selected or no-wake decision.")
    rows = decisions_wire(decisions)
    return {
        "wire_root_id": wire_root_id,
        "manifest_codec": INITIAL_CODEC,
        "resolver_version": RESOLVER_VERSION,
        "policy_version": POLICY_VERSION,
        "control": control,
        "audience": {**audience._digest_record(), "digest": audience.digest},
        "decisions": rows,
        "decisions_digest": decisions_digest(rows),
    }


def validate_initial_record(record: Mapping[str, object], wire_root_id: str) -> CommittedInitial:
    """Strictly reconstruct and independently recompute all N from raw committed bytes.

    This alone is not authority: only MessageBus.read_initial_cohort calls it
    after checking the owner-only marker and scanning the original bus inode.
    """
    from .audience_manifest import FrozenRecipient, freeze_audience
    from .coordination import POLICY_VERSION, RESOLVER_VERSION
    from .declarations import Message
    from .wake import ControlClassification, resolve_wake_cohort

    if set(key for key in record if key.startswith(_PRIVATE_WIRE_PREFIX)) != {PRIVATE_WIRE_FIELD}:
        raise ValueError("Unknown private bus namespace.")
    private = record[PRIVATE_WIRE_FIELD]
    if (
        not isinstance(private, dict)
        or set(private) != {"version", "initial"}
        or type(private["version"]) is not int
        or private["version"] != 1
    ):
        raise ValueError("Unsupported initial bus sideband version.")
    raw = private["initial"]
    if not isinstance(raw, dict) or set(raw) != {
        "wire_root_id",
        "manifest_codec",
        "resolver_version",
        "policy_version",
        "control",
        "audience",
        "decisions",
        "decisions_digest",
    }:
        raise ValueError("Malformed private initial bus sideband.")
    if (
        raw["wire_root_id"] != wire_root_id
        or raw["manifest_codec"] != INITIAL_CODEC
        or raw["resolver_version"] != RESOLVER_VERSION
        or raw["policy_version"] != POLICY_VERSION
        or type(raw["control"]) is not str
    ):
        raise ValueError("Unsupported or wrong-root initial sideband.")
    control = ControlClassification(raw["control"])
    if control is not ControlClassification.ORDINARY:
        raise ValueError("Unsupported initial control issuer in codec v1.")
    audience_raw = raw["audience"]
    if not isinstance(audience_raw, dict) or set(audience_raw) != {
        "version",
        "wire_seq",
        "message_id",
        "exact_target",
        "sender_lookup",
        "sender_name",
        "source_revision",
        "recipients",
        "wire_envelope_digest",
        "digest",
    }:
        raise ValueError("Malformed initial audience.")
    member_rows = audience_raw["recipients"]
    if not isinstance(member_rows, list) or len(member_rows) > 4096:
        raise ValueError("Initial audience has invalid member list.")
    if any(
        not isinstance(item, dict)
        or set(item) != {"recipient_lookup", "canonical_thread"}
        or type(item["recipient_lookup"]) is not str
        or type(item["canonical_thread"]) is not str
        for item in member_rows
    ):
        raise ValueError("Malformed initial audience member.")
    if (
        type(audience_raw["source_revision"]) is not str
        or _HEX64.fullmatch(audience_raw["source_revision"]) is None
        or type(audience_raw["sender_lookup"]) is not str
        or _HEX32.fullmatch(audience_raw["sender_lookup"]) is None
        or any(_HEX32.fullmatch(member["recipient_lookup"]) is None for member in member_rows)
    ):
        raise ValueError("Initial creator lookup or source revision is invalid.")
    recipients = tuple(FrozenRecipient(**member) for member in member_rows)
    public = {key: value for key, value in record.items() if key != PRIVATE_WIRE_FIELD}
    message = Message.from_wire(public)
    if (
        type(public.get("seq")) is not int
        or any(
            type(public.get(field)) is not str
            for field in ("id", "from", "to", "text", "type", "sender_role")
        )
        or _canonical(public) != _canonical(message.to_wire())
        or public_envelope_digest(public) != public_envelope_digest(message.to_wire())
    ):
        raise ValueError("Initial public envelope is not canonical.")
    candidate = freeze_audience(
        message,
        recipients,
        audience_raw["source_revision"],
        sender_lookup=audience_raw["sender_lookup"],
        sender_name=audience_raw["sender_name"],
    )
    if (
        audience_raw != {**candidate._digest_record(), "digest": candidate.digest}
        or type(audience_raw["version"]) is not int
        or type(audience_raw["wire_seq"]) is not int
    ):
        raise ValueError("Initial audience does not match the committed public envelope.")
    decisions = resolve_wake_cohort(message, frozen_audience=candidate, control=control)
    rows = decisions_wire(decisions)
    if (
        type(raw["decisions"]) is not list
        or raw["decisions"] != rows
        or type(raw["decisions_digest"]) is not str
        or raw["decisions_digest"] != decisions_digest(rows)
    ):
        raise ValueError("Initial decisions do not match the full committed N.")
    return CommittedInitial(
        wire_root_id,
        message,
        candidate,
        decisions,
        raw["decisions_digest"],
        control.value,
        RESOLVER_VERSION,
        POLICY_VERSION,
    )
