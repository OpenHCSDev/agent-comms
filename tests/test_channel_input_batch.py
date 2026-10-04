"""Channel batch identity is proved by actual durable admitted inputs."""

from contextlib import AsyncExitStack
from dataclasses import replace

import pytest

from agent_comms.channel_input_batch import InputBatch
from agent_comms.input_attempt import NotSentInput
from agent_comms.input_disposition import InputDispositions
from agent_comms.input_origin import InputProvenance
from agent_comms.messages import Message, MessageType
from agent_comms.owned_turn import OwnedTurn
from agent_comms.routing import ScheduledTurn
from agent_comms.threads import Thread
from agent_comms.turn_context import UserInputSegment
from native_backend_fixture import native_backend_fixture


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
    ):
        assert not InputBatch.capture(messages, input_keys, text, owner, ledger).admits_multiple

    with pytest.raises(ValueError, match="receipt is unavailable"):
        InputBatch.capture(origins, (keys[0], "missing"), prompt, owner, ledger)
    admitted = InputBatch.capture(origins, keys, prompt, owner, ledger)
    assert admitted.originals == ledger.read().originals(keys)
    assert admitted.keys == keys and admitted.prompt == prompt


@pytest.mark.asyncio
async def test_captured_originals_reach_context_reservation_and_unbound_retirement(tmp_path):
    """Actual saved native owner; no prompt is dispatched or replayed."""
    async with native_backend_fixture(tmp_path) as native:
        await native.author_history()
        saved = native.session.read_bytes()
        async with native.open_owner() as (agent, session_id):
            comms = agent._comms

            def require_context(turn):
                segment, = (
                    item for item in turn.context.segments if isinstance(item, UserInputSegment)
                )
                provenances = tuple(
                    item for item in segment.provenance if isinstance(item, InputProvenance)
                )
                assert provenances == tuple(
                    row.context_provenance() for row in turn.original.batch.originals
                )
                assert turn.original.keys == turn.original_keys
                assert agent.inputs.original_sources[session_id] is turn.original
                return turn.original_keys

            async with native.original_input(agent, session_id, "Original direct input") as turn:
                direct_keys = require_context(turn)
                assert len(direct_keys) == 1 and not turn.original.batch.admits_multiple

            comms.threads.claim_thread(
                "peer", tags=frozenset(), worktree=str(native.project)
            )
            origins = tuple(
                comms.messaging.send_message("peer", "#comms", text)
                for text in ("Original first channel input", "Original second channel input")
            )
            prompt = "\n\n".join(ScheduledTurn.incoming(item).prompt for item in origins)
            turn = OwnedTurn(agent.turns, session_id, agent.sessions.require(session_id),
                             prompt, origins=origins)
            async with AsyncExitStack() as resources, AsyncExitStack() as permits:
                assert await turn.acquire(resources, permits)
                channel_keys = require_context(turn)
                assert len(channel_keys) == 2 and turn.original.batch.admits_multiple
                assert turn.original.batch.prompt == prompt

            turn = OwnedTurn(agent.turns, session_id, agent.sessions.require(session_id),
                             "Original scheduled input")
            async with AsyncExitStack() as resources, AsyncExitStack() as permits:
                assert await turn.acquire(resources, permits)
                scheduled_keys = turn.original.keys
                assert len(scheduled_keys) == 1 and not turn.original.batch.admits_multiple
                segment, = (item for item in turn.context.segments
                            if isinstance(item, UserInputSegment))
                assert not any(isinstance(item, InputProvenance) for item in segment.provenance)
                assert turn.original.batch.prompt == turn.context.render().text

            originals = agent.inputs.dispositions.read().originals(
                (*direct_keys, *channel_keys, *scheduled_keys)
            )
            assert all(isinstance(row, NotSentInput) and not row.has_native_binding
                       for row in originals)
            assert comms.registry.require(agent.sessions.require(session_id)).active_turn is None
            assert not agent.inputs.original_sources and not agent.inputs.backend_inboxes
        assert native.session.read_bytes() == saved
        assert native.provider.posts == 0 and native.starts == []
    assert all(not child.alive() and not child.platform.group_members(child.identity)
               for child in native.children)
