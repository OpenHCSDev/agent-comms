"""Conservative, read-only routing for legacy incoming Pi user entries.

The caller must supply committed bus messages that were authorized for this
recipient and registry incarnation facts from the *same* wire. This function
never reads a bus, marks a message delivered, or writes transcript metadata.
An exact rendered prompt is evidence for a local display fallback, not a
substitute for durable turn-start provenance or a provider receipt.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from math import isfinite
from pathlib import Path
from types import MappingProxyType

from .declarations import Message, ScheduledTurn, TurnRouting

_MAX_TEXT_BYTES = 256 * 1024
_MAX_CANDIDATES = 256
_MAX_INDEX_ROWS = 8192
_MAX_HEADER_CHARS = 256


@dataclass(frozen=True, slots=True)
class CommittedIncomingCandidate:
    """One caller-verified bus row and its current sender/recipient identities.

    Constructing this record does not establish commitment or delivery. The
    transcript reader must obtain the row from its own verified wire and use
    the same registry snapshot for the incarnation times; deleted senders and
    ambiguous aliases should not be supplied as candidates.
    """

    message: Message
    wire_root: Path
    delivered_to: str
    recipient_created_at: float
    sender_created_at: float


def _exact_user_text(entry: Mapping[str, object]) -> tuple[str, float] | None:
    if entry.get("type") != "message" or type(entry.get("id")) is not str:
        return None
    message = entry.get("message")
    if not isinstance(message, Mapping) or message.get("role") != "user":
        return None
    content = message.get("content")
    if (
        type(content) is not list
        or len(content) != 1
        or type(content[0]) is not dict
        or content[0].get("type") != "text"
        or type(content[0].get("text")) is not str
    ):
        return None
    text = content[0]["text"]
    # Most Pi user entries are ordinary prompts. This is only a cheap reject;
    # the complete ScheduledTurn rendering and committed bus row still decide.
    if not text.startswith("[agent-comms from "):
        return None
    if len(text.encode("utf-8")) > _MAX_TEXT_BYTES:
        return None
    timestamp = entry.get("timestamp")
    if type(timestamp) is not str:
        return None
    try:
        instant = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            return None
        entry_time = instant.timestamp()
    except (ValueError, OverflowError):
        return None
    return (text, entry_time) if isfinite(entry_time) else None


def _canonical_root_id(path: Path) -> str | None:
    """Compare caller-resolved root identities without per-row filesystem I/O.

    The caller must have resolved and verified each root before constructing
    this page's candidates. A lexical alias, symlink spelling or unknown root
    fails closed instead of trying to resolve it during transcript projection.
    """
    try:
        root = Path(path)
    except (TypeError, ValueError):
        return None
    if not root.is_absolute() or ".." in root.parts:
        return None
    return str(root)


@dataclass(frozen=True, slots=True)
class LegacyIncomingIndex:
    """A read-only per-page candidate lookup; no bus read occurs per Pi row."""

    by_header: Mapping[str, Mapping[bytes, tuple[CommittedIncomingCandidate, ...]]]

    def candidates_for(self, entry: Mapping[str, object]) -> tuple[CommittedIncomingCandidate, ...]:
        parsed = _exact_user_text(entry)
        if parsed is None:
            return ()
        text, _ = parsed
        line_end = text.find("\n", 0, _MAX_HEADER_CHARS + 1)
        if line_end < 0:
            return ()
        return self.by_header.get(text[:line_end], {}).get(sha256(text.encode()).digest(), ())


def index_legacy_incoming_candidates(
    candidates: Iterable[CommittedIncomingCandidate],
) -> LegacyIncomingIndex | None:
    """Index a bounded caller-authorized committed-bus snapshot once per page.

    Header is ONLY an optimization; the SHA-256 bucket retains every matching
    bus row (including duplicates) and ``verify_legacy_incoming_route`` still
    compares the entire rendered prompt to each candidate. Return None if the
    source is too large to index safely. This function performs no disk I/O.
    """
    grouped: dict[str, dict[bytes, list[CommittedIncomingCandidate]]] = {}
    for index, candidate in enumerate(candidates):
        if index >= _MAX_INDEX_ROWS or type(candidate) is not CommittedIncomingCandidate:
            return None
        message = candidate.message
        if type(message) is not Message:
            return None
        header = f"[agent-comms from {message.sender} to {message.target}]"
        if len(header) > _MAX_HEADER_CHARS:
            continue
        prompt = ScheduledTurn.incoming(message).prompt
        if len(prompt.encode()) > _MAX_TEXT_BYTES:
            continue
        by_digest = grouped.setdefault(header, {})
        bucket = by_digest.setdefault(sha256(prompt.encode()).digest(), [])
        # Two exact candidates already make attribution ambiguous. Keep a
        # bounded third entry to force the verifier's ambiguity path, rather
        # than allocating for unlimited duplicate messages.
        if len(bucket) < 3:
            bucket.append(candidate)
    return LegacyIncomingIndex(MappingProxyType({
        header: MappingProxyType({digest: tuple(rows) for digest, rows in digests.items()})
        for header, digests in grouped.items()
    }))


def verify_legacy_incoming_route(
    entry: Mapping[str, object],
    *,
    owner_name: str,
    owner_created_at: float,
    session_wire_root: Path,
    candidates: Iterable[CommittedIncomingCandidate],
) -> TurnRouting | None:
    """Match exactly one already-committed, same-root ScheduledTurn prompt.

    Legacy agent-origin messages steered into an active Pi turn have no saved
    ``record_turn_routing`` sidecar. Only a whole Pi user entry equal to the
    canonical ScheduledTurn rendering may be shown as an incoming route.
    Coordination/goal preludes, quoted fragments, copied text with unrelated
    content, old incarnations, and duplicate identical bus messages all fail
    closed. No substring/header regex is used.

    ``session_wire_root`` is the caller's verified wire identity for this
    transcript, not a value inferred from the Pi user text. The caller must
    not use this fallback when the transcript's source root is unknown.
    """
    parsed = _exact_user_text(entry)
    if parsed is None or type(owner_name) is not str or not owner_name:
        return None
    if type(owner_created_at) not in (int, float) or not isfinite(owner_created_at):
        return None
    source_root = _canonical_root_id(session_wire_root)
    if source_root is None:
        return None

    text, entry_time = parsed
    matches: list[Message] = []
    for index, candidate in enumerate(candidates):
        if index >= _MAX_CANDIDATES:
            return None
        if type(candidate) is not CommittedIncomingCandidate:
            return None
        message = candidate.message
        if type(message) is not Message or type(message.seq) is not int or message.seq <= 0:
            continue
        if (
            candidate.delivered_to != owner_name
            or message.sender == owner_name
            or (
                message.target not in {owner_name, "broadcast"}
                and not message.target.startswith("#")
            )
            or type(candidate.recipient_created_at) not in (int, float)
            or type(candidate.sender_created_at) not in (int, float)
            or candidate.recipient_created_at != owner_created_at
            or not isfinite(candidate.sender_created_at)
            or type(message.timestamp) not in (int, float)
            or not isfinite(message.timestamp)
            or message.timestamp < max(owner_created_at, candidate.sender_created_at)
            or message.timestamp > entry_time
            or not message.starts_turn_for(owner_name)
        ):
            continue
        if _canonical_root_id(candidate.wire_root) != source_root:
            continue
        if ScheduledTurn.incoming(message).prompt == text:
            matches.append(message)
            if len(matches) > 1:
                return None
    return TurnRouting((matches[0],), None) if len(matches) == 1 else None
