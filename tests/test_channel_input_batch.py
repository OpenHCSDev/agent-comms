"""Channel batch identity is proved by actual durable admitted inputs."""

from dataclasses import replace

from agent_comms.channel_input_batch import InputBatch
from agent_comms.input_disposition import InputDispositions
from agent_comms.messages import Message, MessageType
from agent_comms.threads import Thread


def test_channel_batch_exact_identity_and_no_false_multi_input_proof(tmp_path):
    owner = Thread("owner", frozenset(), str(tmp_path))
    ledger = InputDispositions(tmp_path / InputDispositions.filename)
    origins = tuple(
        Message("peer", "#comms", f"text {seq}", MessageType.INFO, seq=seq) for seq in (1, 2)
    )
    keys = tuple(ledger.bus_key(message, owner) for message in origins)
    for key, message in zip(keys, origins, strict=True):
        ledger.record(
            key,
            seq=message.seq,
            owner=owner.name,
            admission=1,
            target=message.target,
            text=message.body,
        )
    prompt = "text 1\n\ntext 2"
    assert InputBatch.capture(origins, keys, prompt, owner, ledger).admits_multiple
    for messages, input_keys, text in (
        (origins[:1], keys[:1], "text 1"),
        ((origins[0], origins[0]), keys, prompt),
        ((replace(origins[0], seq=0), origins[1]), keys, prompt),
        ((replace(origins[0], target="owner"), origins[1]), keys, prompt),
        (origins, tuple(reversed(keys)), prompt),
        (origins, keys, "corrected prompt"),
        (origins, (keys[0], "missing"), prompt),
    ):
        assert not InputBatch.capture(messages, input_keys, text, owner, ledger).admits_multiple
