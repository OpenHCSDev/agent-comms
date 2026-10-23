"""Canonical original-bus batch capture and one-use native reservation."""


from bus_record_fixture import prepare_native_comms
from conftest import publish_initial

import os

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.coordination_cohort import accept_delivery_cohort, pending_sealed_assignments
from agent_comms.coordinator import Coordination
from agent_comms.selected_participant import SelectedParticipant
from agent_comms.thread_model import DeclaredModel


@pytest.mark.asyncio
async def test_a_mixed_route_wave_turns_one_route_at_a_time(tmp_path, *, cursor_delivery):
    """One selected turn answers originals of one reply route; the rest stay

    pending for their own turns, oldest first. Found when the batch answer
    became one plain text: a wave mixing a direct request and a channel
    request must never share that answer across audiences.
    """
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = prepare_native_comms(wire(root))
    for name in ("sender", "receiver"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset({'team'}), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()), model=DeclaredModel('openai-codex/gpt-6-sol')))
    root_id = comms.registry.prepare_native_root().namespace
    lookup = comms.registry.require("receiver").incarnation.lookup
    with Coordination(str(root / "coordination.sqlite3")) as store:
        store.participants.register(lookup, committed=True)

        def send(target, body):
            message = publish_initial(comms, "sender", target, body)
            accept_delivery_cohort(comms.bus, root_id, message.seq, store)
            return message

        direct = send("receiver", "A private question")          # reply route: sender
        channel = send("#team", "A team question")               # reply route: #team
        pending_wires = (direct.seq, channel.seq)
        pending = pending_sealed_assignments(store, lookup)
        assert tuple(row.wire_seq for row in pending) == pending_wires
        async with SelectedParticipant.select(
            comms, store, root_id, "receiver", 0, cursor_delivery=cursor_delivery
        ) as selected:
            assert selected.batch.target == "sender"
            assert tuple(row.wire_seq for row in selected.batch.assignments) == (direct.seq,)
        # The channel original was never claimed by this turn: selection alone
        # settles nothing, so it stays pending for its own turn.
        assert tuple(row.wire_seq for row in
                     pending_sealed_assignments(store, lookup)) == pending_wires
