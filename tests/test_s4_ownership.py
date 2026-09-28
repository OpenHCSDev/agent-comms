"""Declaration extension and public-format tests for S4 ownership boundaries."""

import ast
from pathlib import Path


from agent_comms.channel_targets import BuiltinChannel
from agent_comms.channels import AnyOfMatch, SavedView, ViewKind, ViewPredicate
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.messages import Message, MessageType
from agent_comms.threads import Thread


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
        comms.threads.register(Thread(name, frozenset(), str(tmp_path)))
    assert "everyone" in comms.channels.catalog.read().targets_for(frozenset())
    assert "everyone" in comms.channels.catalog.read().history_targets("everyone")
    comms.messaging.initialize_private_initial_protocol()
    message = comms.messaging.send_user_message("everyone", "hello", worktree=str(tmp_path))
    assert message.response_policy.starts_turn
    assert comms.views.channel_history("everyone")[0].target == BuiltinChannel.ALL.value
    assert comms.bus.pending_count("bob") == 1
    assert not BuiltinChannel.exact_stored_target("everyone")
    assert not BuiltinChannel.exact_stored_target(BuiltinChannel.ANY.value)
    assert BuiltinChannel.exact_stored_target(BuiltinChannel.ALL.value)


def test_wire_formats_derive_field_names_optional_values_and_sorted_tags():
    view = SavedView(
        "work",
        ViewKind.ACTIVITY,
        ViewPredicate(AnyOfMatch, frozenset({"z", "a"})),
        created_at=7.0,
    )
    expected = {
        "name": "work",
        "kind": "activity",
        "predicate": {"match": "any_of", "tags": ["a", "z"]},
        "created_at": 7.0,
        "original_targets": [],
    }
    assert FieldCodec.encode(view) == expected
    assert FieldCodec.decode(SavedView, expected) == view
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


def test_authorities_do_not_import_presentation_or_recover_policy_cases():
    root = Path(__file__).resolve().parents[1] / "src" / "agent_comms"
    for name in (
        "messages.py",
        "message_bus.py",
        "read_basis.py",
        "read_ledger.py",
        "response_policy.py",
    ):
        tree = ast.parse((root / name).read_text())
        assert not any(
            isinstance(node, ast.ImportFrom) and node.module == "presentation"
            for node in ast.walk(tree)
        )
    for name in ("messages.py", "message_bus.py", "input_disposition.py"):
        tree = ast.parse((root / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                assert not any(
                    isinstance(child, ast.Attribute)
                    and isinstance(child.value, ast.Name)
                    and child.value.id == "ResponsePolicy"
                    for child in ast.walk(node)
                )
    assert not (root / "operations.py").exists()
    operations = (root / "history_views.py").read_text()
    assert "_marker_key" not in operations and "_view_marker_key" not in operations
    assert (
        "owner_epochs"
        not in operations[
            operations.index("    def dm_display_page") : operations.index(
                "    def channel_history_page"
            )
        ]
    )
