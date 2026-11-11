"""Channel membership and notifications have one canonical bus authority."""

from contextlib import AsyncExitStack
from dataclasses import replace

from agent_comms.coordination.coordinator import Coordination
from agent_comms.execution.turn_input_source import NoTurnAcceptance
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.notification_evidence import AllOutcomes
from agent_comms.messaging.notification_progress import SentProgress
from agent_comms.projects.checkouts import Checkout
from agent_comms.waking.wake_policy import BoundedTriageWake
from bus_history import read_history
from conftest import accept_initial
from test_coordinated_runtime import _root, tmp_path  # noqa: F401


def test_membership_changes_keep_canonical_notification_and_history(tmp_path):  # noqa: F811
    root, root_id, comms, original, _people = _root(tmp_path)
    # The initial agent-authored ordinary message addresses both channel peers.
    receipts = comms.views.message_notifications_for_references(AllOutcomes(), (original.message.reference,))
    assert {
        row.recipient for row in receipts.outcomes[(original.message.seq, original.message.message_id)]
    } == {"alpha", "beta"}
    comms.channels.update_tags("alpha", remove=frozenset({"team"}))
    comms.registry.declare(
        replace(comms.registry.require("beta"), tags=frozenset({"team", "extra"}))
    )
    message = comms.messaging.send_user_message(CommandTarget.named('#team'), "current membership", worktree=Checkout(root))
    initial = comms.bus.bus.delivery(message.seq)
    assert [row.canonical_thread for row in initial.recipients] == ["beta"]
    assert all(isinstance(decision.wake_mode, BoundedTriageWake) for decision in initial.decisions)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        accepted = accept_initial(comms.bus, root_id, message.seq, store)
    assert [assignment.recipient for assignment in accepted] == ["beta"]
    current = comms.views.message_notifications_for_references(AllOutcomes(), (message.reference,)).outcomes[(message.seq, message.message_id)]
    assert [(row.recipient, type(row.state)) for row in current] == [("beta", SentProgress)]
    assert not comms.registry.require("beta").executing
    assert original.message in read_history(comms, "#team")
    assert message in read_history(comms, "#team")


def test_channel_observer_keeps_bounded_pointers_after_ui_ack(tmp_path):  # noqa: F811
    import json

    from agent_comms.messaging.message_reference import MessageReference
    from agent_comms.nominal.field_codec import FieldCodec

    root, root_id, comms, initial, _people = _root(tmp_path, mentioned=True)
    observer = comms.registry.require("alpha")
    assert any(isinstance(decision.wake_policy(), BoundedTriageWake)
               for decision in initial.decisions)
    sent = tuple(comms.messaging.send("sender", "#team", f"@beta review {number}").message
                 for number in range(6))
    assert not comms.registry.require("alpha").executing
    shown = comms.bus.inbox("alpha", CommandTarget.named("#team")).page.messages
    comms.bus.mark_read_through("alpha", shown[-1].seq, target=CommandTarget.named("#team"))
    ledger = comms.bus.reads.path.read_bytes()
    revision = comms.bus.bus.revision()
    frame = "".join(segment.text() for segment in comms.bus.awareness_segments(observer, ()))
    pointers = tuple(FieldCodec.decode(MessageReference, pointer)
                     for pointer in json.loads(frame.splitlines()[-1])["sources"])
    assert set(pointers) == {initial.message.reference, *(message.reference for message in sent)}
    pointed = comms.bus.bus.messages_for_references(pointers)
    assert all(record.target == "#team" for record in pointed)
    assert comms.bus.reads.path.read_bytes() == ledger
    assert comms.bus.bus.revision() == revision
    assert comms.bus.awareness_segments(replace(observer, created_at=observer.created_at + 1), ()) == ()


async def test_natural_owned_turn_prepares_observer_pointer_without_native_start(
    tmp_path, native_runtime_package,
):  # noqa: F811
    from agent_comms.execution.acp import CommsAgent
    from agent_comms.execution.owned_turn import OwnedTurn
    from agent_comms.nominal.field_codec import AcpSessionId
    
    root, root_id, comms, initial, _people = _root(tmp_path, mentioned=True)
    agent = CommsAgent(comms, auto_wake=False)
    turn = OwnedTurn(
        agent.turns, "alpha", "alpha", "Independently authorized task",
        acceptance=NoTurnAcceptance(),
    )
    try:
        # Binding acquires real session authority; context preparation sends no model input.
        await agent.sessions.bind_owned(comms.registry.require("alpha"), AcpSessionId("alpha"))
        async with AsyncExitStack() as resources:
            async with AsyncExitStack() as permits:
                assert await turn.acquire(resources, permits)
                await turn.prepare_native()
                assert turn.task == "Independently authorized task"
                rendered = turn.context.render().text
                assert initial.message.message_id in rendered
                assert set(agent.turns.persistent_backends) == {"alpha"}
                assert not agent.turns.persistent_backends["alpha"].available
    finally:
        await agent.shutdown()
