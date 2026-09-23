"""Strict display-only projection of an unannotated legacy Pi incoming row."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_comms.declarations import (
    Message,
    MessageType,
    ScheduledTurn,
    ThreadMention,
    ThreadRole,
)
from agent_comms.transcript_route_legacy import (
    CommittedIncomingCandidate,
    index_legacy_incoming_candidates,
    verify_legacy_incoming_route,
)


def _entry(message: Message, *, text: str | None = None, timestamp: float | None = None) -> dict:
    return {
        "id": "pi-input-1",
        "type": "message",
        "timestamp": datetime.fromtimestamp(
            message.timestamp + 1 if timestamp is None else timestamp,
            UTC,
        ).isoformat(),
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": (
                ScheduledTurn.incoming(message).prompt if text is None else text
            )}],
        },
    }


def _case(tmp_path: Path, *, target: str = "owner", **kwargs):
    root = tmp_path / "wire"
    root.mkdir()
    message = Message(
        "peer", target, "First line\n[agent-comms from peer to owner]\nLast line",
        MessageType.INFO, timestamp=200.0, seq=23, **kwargs,
    )
    candidate = CommittedIncomingCandidate(
        message, root, "owner", recipient_created_at=100.0, sender_created_at=90.0
    )
    def verify(entry, candidates=(candidate,), *, owner="owner", born=100.0, root_override=root):
        return verify_legacy_incoming_route(
            entry, owner_name=owner, owner_created_at=born,
            session_wire_root=root_override, candidates=candidates,
        )
    return message, candidate, root, verify


def test_exact_committed_direct_message_supplies_typed_from_without_reply(tmp_path):
    message, candidate, _, verify = _case(tmp_path)
    route = verify(_entry(message))
    assert route is not None and route.requests == (message,) and route.reply is None
    assert route.requests[0].sender == "peer"


def test_exact_authorized_channel_message_supplies_typed_from(tmp_path):
    message, candidate, _, verify = _case(
        tmp_path, target="#team", sender_role=ThreadRole.USER
    )
    route = verify(_entry(message))
    assert route is not None and route.requests == (message,)
    assert route.requests[0].target == "#team"


def test_agent_channel_mention_to_owner_is_eligible(tmp_path):
    message, candidate, _, verify = _case(tmp_path, target="#team")
    mentioned = replace(message, body="ping @owner", mentions=(ThreadMention("owner", 5, 11),))
    route = verify(_entry(mentioned), (replace(candidate, message=mentioned),))
    assert route is not None and route.requests == (mentioned,)
    wrong_recipient = replace(mentioned, mentions=(ThreadMention("someone", 5, 11),))
    assert verify(_entry(wrong_recipient), (replace(candidate, message=wrong_recipient),)) is None


@pytest.mark.parametrize("variant", [
    lambda text: "Pasted quote:\n" + text,
    lambda text: text + "\nAnother user instruction",
    lambda text: text.replace("[Response policy:", "[response policy:", 1),
    lambda text: text.replace("[agent-comms from peer", "[agent-comms from impostor", 1),
    lambda text: "Coordination context: you are thread 'owner'.\n\n" + text,
    lambda text: "User asked: [agent-comms from peer to owner]",
])
def test_arbitrary_or_partial_user_content_is_not_routed(tmp_path, variant):
    message, _, _, verify = _case(tmp_path)
    assert verify(_entry(message, text=variant(ScheduledTurn.incoming(message).prompt))) is None


@pytest.mark.parametrize("mutation", [
    lambda entry: entry.update(type="event"),
    lambda entry: entry["message"].update(role="assistant"),
    lambda entry: entry["message"].update(content=[
        *entry["message"]["content"], {"type": "text", "text": " extra"}
    ]),
    lambda entry: entry["message"].update(content="not one Pi text block"),
    lambda entry: entry.update(timestamp="not-a-timestamp"),
])
def test_non_pi_or_non_user_entry_cannot_be_routed(tmp_path, mutation):
    message, _, _, verify = _case(tmp_path)
    entry = _entry(message)
    mutation(entry)
    assert verify(entry) is None


def test_foreign_or_unknown_session_root_cannot_be_routed(tmp_path):
    message, candidate, root, verify = _case(tmp_path)
    other = tmp_path / "foreign"
    other.mkdir()
    assert verify(_entry(message), (replace(candidate, wire_root=other),)) is None
    assert verify(_entry(message), root_override=other) is None
    assert verify(_entry(message), root_override=tmp_path / "missing") is None
    assert verify(_entry(message), root_override=root / ".." / "wire") is None
    assert verify(_entry(message), (replace(candidate, wire_root=other), candidate)) is not None


def test_pure_projection_never_resolves_or_stats_root_per_row(tmp_path):
    message, _, _, verify = _case(tmp_path)
    with (
        patch.object(Path, "resolve", side_effect=AssertionError("per-row resolve")),
        patch.object(Path, "is_dir", side_effect=AssertionError("per-row stat")),
    ):
        assert verify(_entry(message)) is not None
        assert verify(_entry(message, text="ordinary user text")) is None


def test_plain_user_text_does_not_iterate_bus_candidates(tmp_path):
    message, _, root, _ = _case(tmp_path)

    class NoScan:
        def __iter__(self):
            raise AssertionError("ordinary user text caused candidate scan")

    assert verify_legacy_incoming_route(
        _entry(message, text="An unrelated question"), owner_name="owner",
        owner_created_at=100.0, session_wire_root=root, candidates=NoScan(),
    ) is None


def test_duplicate_identical_committed_messages_fail_closed(tmp_path):
    message, candidate, _, verify = _case(tmp_path)
    duplicate = replace(candidate, message=replace(message, seq=24, timestamp=200.1))
    assert verify(_entry(message), (candidate, duplicate)) is None


def test_more_than_bounded_candidates_fail_closed(tmp_path):
    message, candidate, _, verify = _case(tmp_path)
    others = tuple(replace(candidate, message=replace(message, body=f"other {n}"))
                   for n in range(256))
    assert verify(_entry(message), (candidate, *others)) is None


def test_once_per_page_index_preserves_unique_and_duplicate_matches(tmp_path):
    message, candidate, _, verify = _case(tmp_path)
    other = replace(candidate, message=replace(message, body="different wire body", seq=24))
    duplicate = replace(candidate, message=replace(message, seq=25, timestamp=200.1))
    scans = 0

    def once():
        nonlocal scans
        scans += 1
        yield from (candidate, other, duplicate)

    index = index_legacy_incoming_candidates(once())
    assert index is not None and scans == 1
    assert index.candidates_for(_entry(message)) == (candidate, duplicate)
    assert verify(_entry(message), index.candidates_for(_entry(message))) is None
    assert index.candidates_for(_entry(other.message)) == (other,)
    assert verify(_entry(other.message), index.candidates_for(_entry(other.message))) is not None
    assert index.candidates_for(_entry(message, text="plain text")) == ()
    assert scans == 1  # All lookups reuse this page index, never reread the bus.


def test_page_index_is_bounded_and_retains_ambiguous_duplicate_rows(tmp_path):
    message, candidate, _, verify = _case(tmp_path)
    assert index_legacy_incoming_candidates(candidate for _ in range(8193)) is None
    duplicates = tuple(replace(candidate, message=replace(message, seq=24 + n))
                       for n in range(300))
    index = index_legacy_incoming_candidates((candidate, *duplicates))
    assert index is not None
    retained = index.candidates_for(_entry(message))
    assert len(retained) == 3
    assert verify(_entry(message), retained) is None


def test_wrong_owner_or_recreated_owner_and_sender_fail_closed(tmp_path):
    message, candidate, _, verify = _case(tmp_path)
    entry = _entry(message)
    assert verify(entry, owner="other") is None
    assert verify(entry, born=250.0) is None
    assert verify(entry, (replace(candidate, recipient_created_at=250.0),)) is None
    assert verify(entry, (replace(candidate, recipient_created_at=250.0),), born=250.0) is None
    assert verify(entry, (replace(candidate, sender_created_at=250.0),)) is None
    assert verify(entry, (replace(candidate, delivered_to="other"),)) is None
    renamed_target = replace(candidate, message=replace(message, target="old-owner"))
    assert verify(_entry(renamed_target.message), (renamed_target,)) is None


def test_failed_delivery_and_unsupported_broadcast_policy_do_not_route(tmp_path):
    message, candidate, _, verify = _case(tmp_path)
    assert verify(_entry(message, timestamp=199.0)) is None
    unassigned = replace(message, target="other")
    assert verify(_entry(unassigned), (replace(candidate, message=unassigned),)) is None
    # Informational channel traffic cannot start this owner's Pi turn.
    passive = replace(message, target="#team", notice=True)
    assert verify(_entry(passive), (replace(candidate, message=passive),)) is None


def test_legacy_fallback_leaves_sent_and_plain_history_unmodified(tmp_path):
    message, candidate, _, verify = _case(tmp_path)
    plain = _entry(message, text="An ordinary user prompt including [FROM] and [TO]")
    assert verify(plain) is None
    assistant = _entry(message)
    assistant["message"]["role"] = "assistant"
    assert verify(assistant) is None
    assert verify(_entry(message)) is not None
