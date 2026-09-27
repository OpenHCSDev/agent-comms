"""Declaration extension and public-format tests for S4 ownership boundaries."""

import ast
import json
from pathlib import Path

import pytest

from agent_comms import (
    Message,
    MessageType,
    SavedView,
    Thread,
    ViewKind,
    ViewMatch,
    ViewPredicate,
    wire,
)
from agent_comms.declarations import BuiltinChannel, ThreadRole


def test_alias_declaration_drives_lookup_delivery_history_and_audience(monkeypatch, tmp_path):
    original = BuiltinChannel.aliases.fget
    monkeypatch.setattr(
        BuiltinChannel,
        "aliases",
        property(
            lambda member: (
                (*original(member), "everyone")
                if member is BuiltinChannel.ALL
                else original(member)
            )
        ),
    )
    assert BuiltinChannel.lookup("everyone") is BuiltinChannel.ALL
    comms = wire(tmp_path)
    for name in ("alice", "bob"):
        comms.register(Thread(name, frozenset(), str(tmp_path)))
    assert "everyone" in comms.channel_catalog.targets_for(frozenset())
    assert "everyone" in comms.channel_catalog.history_targets("everyone")
    message = Message("alice", "everyone", "hello", MessageType.INFO, sender_role=ThreadRole.USER)
    assert message.response_policy.starts_turn
    comms.bus.send(message)
    assert comms.channel_history("everyone")[0].target == BuiltinChannel.ALL.value
    assert comms.pending_count("bob") == 1
    assert not BuiltinChannel.exact_stored_target("everyone")
    assert not BuiltinChannel.exact_stored_target(BuiltinChannel.ANY.value)
    assert BuiltinChannel.exact_stored_target(BuiltinChannel.ALL.value)


def test_wire_formats_derive_field_names_optional_values_and_sorted_tags():
    view = SavedView(
        "work",
        ViewKind.ACTIVITY,
        ViewPredicate(ViewMatch.ANY_OF, frozenset({"z", "a"})),
        created_at=7.0,
    )
    expected = {
        "name": "work",
        "kind": "activity",
        "predicate": {"match": "any_of", "tags": ["a", "z"]},
        "created_at": 7.0,
    }
    assert view.to_wire() == expected
    assert SavedView.from_wire("work", {k: v for k, v in expected.items() if k != "name"}) == view
    message = Message("sender", "peer", "body", MessageType.INFO, timestamp=7.0, seq=1)
    assert message.to_wire() == {
        "from": "sender",
        "to": "peer",
        "text": "body",
        "type": "info",
        "ts": 7.0,
        "seq": 1,
        "sender_role": "agent",
        "id": message.message_id,
    }
    assert list(message.to_wire()) == [
        "seq",
        "id",
        "from",
        "to",
        "ts",
        "type",
        "text",
        "sender_role",
    ]
    assert Message.from_wire(message.to_wire()) == message


@pytest.mark.parametrize("key", ["user", '["user","#team"]', '["view2","user","#team","exact",[]]'])
def test_legacy_markers_without_shown_membership_reset_with_visible_notice(tmp_path, key):
    comms = wire(tmp_path)
    comms.register(Thread("alice", frozenset({"team"}), str(tmp_path)))
    viewer = comms.user_identity(str(tmp_path)).name
    comms.send("alice", "#team", "not proved shown by a legacy maximum")
    comms.reads.path.unlink()  # Model upgrade from a pre-ledger root.
    legacy = comms.reads.path.with_name(comms.reads.legacy_filename)
    legacy.write_text(json.dumps({key: 1}))
    original = legacy.read_bytes()
    reopened = wire(tmp_path)
    snapshot = reopened.viewer_snapshot(str(tmp_path))
    assert snapshot.read_marker_notice and snapshot.channel_unread["#team"] == 1
    assert not reopened.reads.seen_sequences(viewer, reopened.registry.snapshot())
    assert legacy.read_bytes() == original  # Executor compatibility bytes preserved.


def test_authorities_do_not_import_presentation_or_recover_policy_cases():
    root = Path(__file__).resolve().parents[1] / "src" / "agent_comms"
    for name in ("declarations.py", "read_basis.py", "read_ledger.py", "response_policy.py"):
        tree = ast.parse((root / name).read_text())
        assert not any(
            isinstance(node, ast.ImportFrom) and node.module == "presentation"
            for node in ast.walk(tree)
        )
    for name in ("declarations.py", "input_disposition.py"):
        tree = ast.parse((root / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                assert not any(
                    isinstance(child, ast.Attribute)
                    and isinstance(child.value, ast.Name)
                    and child.value.id == "ResponsePolicy"
                    for child in ast.walk(node)
                )
    operations = (root / "operations.py").read_text()
    assert "_marker_key" not in operations and "_view_marker_key" not in operations
    assert (
        "owner_epochs"
        not in operations[
            operations.index("    def dm_display_page") : operations.index(
                "    def channel_history_page"
            )
        ]
    )
