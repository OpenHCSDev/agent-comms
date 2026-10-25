"""Channel batch identity is proved by actual durable authorized inputs."""

from agent_comms.repo import Checkout
from dataclasses import replace

import pytest

from agent_comms.channel_input_batch import InputBatch
from agent_comms.field_codec import InputId
from agent_comms.input_key import AcpInputKey
from agent_comms.input_disposition import InputDispositions
from agent_comms.messages import Message, InfoKind
from stored_inputs import stored_inputs
from agent_comms.threads import Thread


def test_channel_batch_exact_identity_and_no_false_multi_input_proof(tmp_path, *, thread_registration):
    owner = Thread('owner', frozenset(), Checkout.from_wire(str(tmp_path)))
    ledger = InputDispositions(tmp_path)
    origins = tuple(
        Message("peer", "#comms", f"text {seq}", InfoKind, seq=seq) for seq in (1, 2)
    )
    keys = tuple(ledger.bus_key(message, owner) for message in origins)
    for key, message in zip(keys, origins, strict=True):
        ledger.record(
            key,
            seq=message.seq,
            owner=owner.name,
            claim_scope=1,
            target=message.target,
            text=message.body,
        )
    prompt = "text 1\n\ntext 2"
    document = stored_inputs(ledger.root)
    assert InputBatch.capture(origins, keys, prompt, owner, document).allows_multiple
    for messages, input_keys, text in (
        (origins[:1], keys[:1], "text 1"),
        ((origins[0], origins[0]), keys, prompt),
        ((replace(origins[0], seq=0), origins[1]), keys, prompt),
        ((replace(origins[0], target="owner"), origins[1]), keys, prompt),
        (origins, tuple(reversed(keys)), prompt),
        (origins, keys, "corrected prompt"),
    ):
        assert not InputBatch.capture(messages, input_keys, text, owner, document).allows_multiple

    with pytest.raises(ValueError):
        InputBatch.capture(origins, (keys[0], AcpInputKey(InputId.mint())), prompt, owner, document)
    authorized = InputBatch.capture(origins, keys, prompt, owner, document)
    assert authorized.originals == document.originals(keys)
    assert authorized.keys == keys and authorized.prompt == prompt


