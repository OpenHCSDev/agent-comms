"""Declaration extension and public-format tests for S4 ownership boundaries."""

import ast
from pathlib import Path


from agent_comms.channels import AnyOfMatch, SavedView, ViewKind, ViewPredicate
from agent_comms.field_codec import FieldCodec
from agent_comms.messages import Message, MessageType


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
