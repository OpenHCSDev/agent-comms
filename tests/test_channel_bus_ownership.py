"""Channel membership and notifications have one canonical bus authority."""

from dataclasses import replace

from agent_comms.coordination_cohort import accept_initial_cohort
from agent_comms.coordination_store import MutationStore
from agent_comms.wake_policy import BoundedTriageWake
from test_coordinated_runtime import _root, tmp_path  # noqa: F401


def test_membership_changes_keep_canonical_notification_and_history(tmp_path):  # noqa: F811
    root, root_id, comms, original, _people = _root(tmp_path)
    # The initial agent-authored ordinary message addresses both channel peers.
    receipts = comms.views.message_notifications((original.message,))
    assert {
        row.recipient for row in receipts[(original.message.seq, original.message.message_id)]
    } == {"alpha", "beta"}
    comms.channels.update_tags("alpha", remove=frozenset({"team"}))
    comms.threads.register(
        replace(comms.registry.require("beta"), tags=frozenset({"team", "extra"}))
    )
    message = comms.messaging.send_user_message("#team", "current membership", worktree=str(root))
    initial = comms.bus.log.read_initial_cohort(root_id, message.seq)
    assert [row.canonical_thread for row in initial.audience.recipients] == ["beta"]
    assert all(isinstance(decision.wake_mode, BoundedTriageWake) for decision in initial.decisions)
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        accepted = accept_initial_cohort(comms.bus, root_id, message.seq, store).value
    assert [assignment.recipient for assignment in accepted.assignments] == ["beta"]
    current = comms.views.message_notifications((message,))[(message.seq, message.message_id)]
    assert [(row.recipient, row.state) for row in current] == [("beta", "Pending")]
    assert comms.registry.require("beta").active_turn is None
    assert original.message in comms.views.channel_history("#team")
    assert message in comms.views.channel_history("#team")
    assert not (root / "acp_passive_channel_awareness.json").exists()


def test_no_wake_observer_keeps_bounded_pointers_after_ui_ack(tmp_path):  # noqa: F811
    import json

    from agent_comms.wake import NoWakeDecision

    root, root_id, comms, initial, _people = _root(tmp_path, mentioned=True)
    observer = comms.registry.require("alpha")
    assert any(isinstance(decision, NoWakeDecision) for decision in initial.decisions)
    for number in range(6):
        message = comms.messaging.send_message("sender", "#team", f"@beta review {number}")
    assert comms.registry.require("alpha").active_turn is None
    comms.messaging.acknowledge("alpha", "#team")
    ledger = comms.bus.reads.path.read_bytes()
    source = comms.bus.log.path.read_bytes()
    frame = comms.bus.awareness_prompt(observer)
    pointers = json.loads(frame.splitlines()[-1])["sources"]
    assert len(pointers) == 4
    assert pointers[-1]["seq"] == message.seq
    assert pointers[-1]["message_id"] == message.message_id
    for pointer in pointers:
        row = json.loads(source[pointer["offset"] : pointer["offset"] + pointer["length"]])
        assert row["seq"] == pointer["seq"] and row["to"] == "#team"
    assert "may omit older" in frame
    assert comms.bus.reads.path.read_bytes() == ledger
    assert comms.bus.log.path.read_bytes() == source
    assert not (root / "acp_passive_channel_awareness.json").exists()
    assert comms.bus.awareness_prompt(replace(observer, created_at=observer.created_at + 1)) == ""
    with comms.bus.log.locked():
        marker = comms.bus.log._private_marker_unlocked()
        marker.admission_after_seq = marker.last_seq
        comms.bus.log.write_metadata_unlocked(marker)
    assert comms.bus.awareness_prompt(observer) == ""


def test_awareness_refuses_changed_checkpoint_without_rebuild(tmp_path):  # noqa: F811
    root, _root_id, comms, _initial, _people = _root(tmp_path, mentioned=True)
    observer = comms.registry.require("alpha")
    path = root / "private_bus_checkpoint.sqlite3"
    path.touch()  # Same content, but the durable index seal must refuse its new revision.
    before = path.stat()
    frame = comms.bus.awareness_prompt(observer)
    assert "awareness unavailable" in frame
    assert path.stat() == before


async def test_natural_owned_turn_prepares_observer_pointer_without_native_start(tmp_path):  # noqa: F811
    import os
    from pathlib import Path

    import pytest

    from agent_comms.acp import CommsAgent
    from agent_comms.owned_turn import OwnedTurn

    package = os.environ.get("PI_COMPACTION_TEST_PACKAGE")
    if not package:
        pytest.skip("Prepared native package required; no model request")
    root, root_id, comms, initial, _people = _root(tmp_path, mentioned=True)
    agent = CommsAgent(
        comms,
        agent_bin="pi",
        auto_wake=False,
        adaptive_compaction_enabled=False,
        private_nk_wire_root_id=root_id,
        private_nk_native_package=Path(package),
    )
    turn = OwnedTurn(agent.turns, "alpha", "alpha", "Independently authorized task")
    try:
        assert turn.admit()
        turn.begin()
        turn.prepare_prompt()
        turn.open_stream()
        await turn.prepare_native()
        assert "Independently authorized task" in turn.task
        assert initial.message.message_id in turn.task
        assert "Canonical bus awareness" in turn.task
        assert "No response obligation" in turn.task
        assert agent.turns.persistent_backends == {}
        assert not (root / "acp_passive_channel_awareness.json").exists()
    finally:
        comms.agents.finish_turn(turn.turn_lease)
        await agent.shutdown()
