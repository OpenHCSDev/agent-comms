"""Declaration extension and public-format tests for S4 ownership boundaries."""



from agent_comms.channels import AnyOfMatch, ViewPredicate, ActivityView
from agent_comms.field_codec import FieldCodec
from agent_comms.messages import Message, InfoKind


def test_wire_formats_derive_field_names_optional_values_and_sorted_tags():
    view = ActivityView('work', ViewPredicate(AnyOfMatch, frozenset({'z', 'a'})), created_at=7.0)
    expected = {
        "name": "work",
        "kind": "activity",
        "predicate": {"match": "any_of", "tags": ["a", "z"]},
        "created_at": 7.0,
        "original_targets": [],
    }
    assert FieldCodec.encode(view) == expected
    assert FieldCodec.decode(SavedView, expected) == view
    message = Message("sender", "peer", "body", InfoKind, timestamp=7.0, seq=1)
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


