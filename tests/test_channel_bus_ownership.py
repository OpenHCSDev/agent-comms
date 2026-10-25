"""Channel membership and notifications have one canonical bus authority."""

from contextlib import AsyncExitStack
from dataclasses import replace

from agent_comms.channel_targets import CommandTarget
from conftest import accept_initial
from agent_comms.coordinator import Coordination
from agent_comms.native_arguments import GivenBinary
from agent_comms.notification_progress import SentProgress
from agent_comms.turn_input_source import NoTurnAcceptance
from agent_comms.wake_policy import BoundedTriageWake
from bus_history import read_history
from test_coordinated_runtime import _root, tmp_path  # noqa: F401


def test_membership_changes_keep_canonical_notification_and_history(tmp_path):  # noqa: F811
    root, root_id, comms, original, _people = _root(tmp_path)
    # The initial agent-authored ordinary message addresses both channel peers.
    receipts = comms.views.message_notifications_for_references((original.message.reference,))
    assert {
        row.recipient for row in receipts[(original.message.seq, original.message.message_id)]
    } == {"alpha", "beta"}
    comms.channels.update_tags("alpha", remove=frozenset({"team"}))
    comms.registry.declare(
        replace(comms.registry.require("beta"), tags=frozenset({"team", "extra"}))
    )
    message = comms.messaging.send_user_message(CommandTarget.named('#team'), "current membership", worktree=str(root))
    initial = comms.bus.bus.delivery(message.seq)
    assert [row.canonical_thread for row in initial.recipients] == ["beta"]
    assert all(isinstance(decision.wake_mode, BoundedTriageWake) for decision in initial.decisions)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        accepted = accept_initial(comms.bus, root_id, message.seq, store)
    assert [assignment.recipient for assignment in accepted] == ["beta"]
    current = comms.views.message_notifications_for_references((message.reference,))[(message.seq, message.message_id)]
    assert [(row.recipient, type(row.state)) for row in current] == [("beta", SentProgress)]
    assert not comms.registry.require("beta").executing
    assert original.message in read_history(comms, "#team")
    assert message in read_history(comms, "#team")
    assert not (root / "acp_passive_channel_awareness.json").exists()


def test_no_wake_observer_keeps_bounded_pointers_after_ui_ack(tmp_path):  # noqa: F811
    import json

    from agent_comms.field_codec import FieldCodec
    from agent_comms.message_reference import MessageReference
    from agent_comms.wake_policy import NoWakeDecision

    root, root_id, comms, initial, _people = _root(tmp_path, mentioned=True)
    observer = comms.registry.require("alpha")
    assert any(isinstance(decision, NoWakeDecision) for decision in initial.decisions)
    sent = tuple(comms.messaging.send("sender", "#team", f"@beta review {number}").message
                 for number in range(6))
    assert not comms.registry.require("alpha").executing
    references = tuple(message.reference for message in comms.bus.inbox("alpha", CommandTarget.named("#team")).page.messages)
    comms.messaging.acknowledge("alpha", references)
    ledger = comms.bus.reads.path.read_bytes()
    revision = comms.bus.bus.revision()
    frame = "".join(segment.text() for segment in comms.bus.awareness_segments(observer, ()))
    pointers = tuple(FieldCodec.decode(MessageReference, pointer)
                     for pointer in json.loads(frame.splitlines()[-1])["sources"])
    assert set(pointers) == {initial.message.reference, *(message.reference for message in sent)}
    pointed = comms.bus.bus.messages_for_references(pointers)
    assert all(record.target == "#team" for record in pointed)
    assert "may omit older" in frame
    assert comms.bus.reads.path.read_bytes() == ledger
    assert comms.bus.bus.revision() == revision
    assert not (root / "acp_passive_channel_awareness.json").exists()
    assert comms.bus.awareness_segments(replace(observer, created_at=observer.created_at + 1), ()) == ()


async def test_natural_owned_turn_prepares_observer_pointer_without_native_start(tmp_path):  # noqa: F811
    from pathlib import Path


    from agent_comms.acp import CommsAgent
    from agent_comms.owned_turn import OwnedTurn

    root, root_id, comms, initial, _people = _root(tmp_path, mentioned=True)
    agent = CommsAgent(
        comms,
        agent_bin=GivenBinary("pi"),
        auto_wake=False,
    )
    turn = OwnedTurn(
        agent.turns, "alpha", "alpha", "Independently authorized task",
        acceptance=NoTurnAcceptance(),
    )
    try:
        async with AsyncExitStack() as resources:
            async with AsyncExitStack() as permits:
                assert await turn.acquire(resources, permits)
                await turn.prepare_native()
                assert turn.task == "Independently authorized task"
                rendered = turn.context.render().text
                assert initial.message.message_id in rendered
                assert "Canonical bus awareness" in rendered
                assert "No response obligation" in rendered
                assert set(agent.turns.persistent_backends) == {"alpha"}
                assert not agent.turns.persistent_backends["alpha"].available
                assert not (root / "acp_passive_channel_awareness.json").exists()
    finally:
        await agent.shutdown()
