"""Message exposes the same text-size contract as transcript events."""

from dataclasses import replace

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.messages import InfoKind, Message


@pytest.mark.parametrize("body", ["hello", "a\nb", "λ🙂", "e\u0301", "x" * 4096])
def test_text_size_is_derived_from_the_message_body(body):
    message = Message("sender", "target", body, InfoKind)
    assert message.text_size == len(body)
    changed = replace(message, body=body + "!")
    assert changed.text_size == len(body) + 1
    assert "text_size" not in FieldCodec.encode(message)
