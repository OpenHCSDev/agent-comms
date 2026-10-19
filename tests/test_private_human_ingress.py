"""Cooperative local USER ingress on a marked private root; no provider or live wake."""

from __future__ import annotations

from agent_comms.channel_targets import CommandTarget
from conftest import publish_initial
import asyncio
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.bus_publication import AgentOrigin, HumanOrigin
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms, wire
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordination_results import Applied
from agent_comms.coordinator import Coordination
from agent_comms.errors import (
    HumanInitialUnknownError,
    RelationViolationError,
    UnregisteredThreadError,
)
from agent_comms.messages import Message, InfoKind
from agent_comms.thread_identity import UserThreadRole
from agent_comms.threads import Thread

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="private bus requires POSIX owner/directory durability"
)


def _root(tmp_path: Path) -> tuple[Comms, Coordination, str, dict[str, str]]:
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root)
    lookups: dict[str, str] = {}
    for name, created_at in (("alice", 17001.0), ("bob", 17002.0)):
        comms.registry.declare(comms.registry.new_thread(name, frozenset({'team'}), str(root), created_at=created_at))
        lookups[name] = RecordedThreadIncarnation("fixture", created_at).lookup
    store = Coordination(str(root / "coordination.sqlite"), clock_ms=lambda: 9999)
    install_private_cohort_schema(store)
    for name in lookups:
        store.participants.register(lookups[name], name, name, committed=True)
    return comms, store, comms.messaging.initialize_private_initial_protocol(), lookups


def test_private_human_channel_and_dm_seal_original_full_audience(tmp_path: Path) -> None:
    comms, store, root_id, lookups = _root(tmp_path)
    channel = comms.messaging.send_user_message("#team", "hello @alice", worktree=str(tmp_path))
    assert channel.sender_role == UserThreadRole
    assert [row.body for row in list(comms.views.channel_history_page("#team").messages)] == ["hello @alice"]
    initial = comms.bus.log.read_delivery_cohort(root_id, channel.seq)
    assert initial.message == channel
    assert {member.recipient_lookup for member in initial.audience.recipients} == set(
        lookups.values()
    )
    acknowledgement = accept_delivery_cohort(comms.bus, root_id, channel.seq, store)
    assert isinstance(acknowledgement, Applied)
    assert acknowledgement.value.member_count == 2
    assert acknowledgement.value.assignment_count == 1  # Bob is a frozen unmentioned observer.
    dm = comms.messaging.send_user_message("bob", "direct", worktree=str(tmp_path))
    assert dm.seq > channel.seq and dm.sender == channel.sender
    assert list(comms.views.history_page(CommandTarget.named("bob"), viewer=channel.sender).messages)[-1] == dm
    dm_initial = comms.bus.log.read_delivery_cohort(root_id, dm.seq)
    assert [
        (member.canonical_thread, member.recipient_lookup)
        for member in dm_initial.audience.recipients
    ] == [("bob", lookups["bob"])]
    accepted = accept_delivery_cohort(comms.bus, root_id, dm.seq, store)
    assert isinstance(accepted, Applied)
    assert accepted.value.member_count == accepted.value.assignment_count == 1


def test_explicit_root_override_preserves_private_user_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms, _, root_id, _ = _root(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "other-home"))
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(comms.root))
    selected = wire()
    assert selected.root == comms.root
    sent = selected.messaging.send_user_message("alice", "explicit", worktree=str(tmp_path))
    assert selected.bus.log.read_delivery_cohort(root_id, sent.seq).message == sent


@pytest.mark.parametrize(
    ("origin_of", "speaker", "refused", "lost"),
    [
        (lambda user: AgentOrigin(), "alice", "executable", OSError),
        (
            lambda user: HumanOrigin(user.name, user.created_at, user.worktree),
            None,
            "USER origin",
            HumanInitialUnknownError,
        ),
    ],
    ids=["agent", "human"],
)
def test_initial_origin_owns_sender_check_and_append_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin_of, speaker, refused, lost
) -> None:
    comms, _, _, _ = _root(tmp_path)
    user = comms.messaging.user_identity(str(tmp_path))
    origin = origin_of(user)
    accepted = speaker or user.name
    other = user.name if speaker else "alice"
    with pytest.raises(RelationViolationError, match=refused):
        comms.bus.publisher.publish_ordinary(Message(other, "bob", "refused", InfoKind), origin=origin)
    assert comms.bus.log.latest_sequence() == 0
    original = comms.bus.log.append_private_unlocked

    def fail_after_append(metadata, row):
        original(metadata, row)
        raise OSError("simulated lost acknowledgement after durable append")

    monkeypatch.setattr(comms.bus.log, "append_private_unlocked", fail_after_append)
    with pytest.raises(lost):
        comms.bus.publisher.publish_ordinary(Message(accepted, "bob", "uncertain", InfoKind), origin=origin)
    assert len(list(comms.bus.full_history_page().messages)) == 1


def test_duplicate_human_id_denied_before_second_append(tmp_path: Path) -> None:
    comms, _, _, _ = _root(tmp_path)
    user = comms.messaging.user_identity(str(tmp_path))
    message = Message(user.name, "alice", "one input", InfoKind, timestamp=123456.0)
    origin = HumanOrigin(user.name, user.created_at, user.worktree)
    first = comms.bus.publisher.publish_ordinary(message, origin=origin)
    with pytest.raises(RelationViolationError, match="already exists"):
        comms.bus.publisher.publish_ordinary(message, origin=origin)
    assert [row.message_id for row in list(comms.bus.full_history_page().messages)] == [first.message_id]


def test_post_append_error_is_unknown_and_never_automatically_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms, _, root_id, _ = _root(tmp_path)
    original = comms.bus.log.append_private_unlocked
    calls = 0

    def fail_after_append(metadata, row):
        nonlocal calls
        calls += 1
        original(metadata, row)
        raise OSError("simulated lost acknowledgement after durable append")

    monkeypatch.setattr(comms.bus.log, 'append_private_unlocked', fail_after_append)
    with pytest.raises(HumanInitialUnknownError, match="do not retry") as raised:
        comms.messaging.send_user_message("alice", "uncertain", worktree=str(tmp_path))
    assert calls == 1
    error = raised.value
    assert error.wire_root_id == root_id
    initial = comms.bus.log.read_delivery_cohort(root_id, error.wire_seq)
    assert initial.message.message_id == error.message_id
    assert len(list(comms.bus.full_history_page().messages)) == 1
    monkeypatch.setattr(comms.bus.log, 'append_private_unlocked', original)
    user = comms.registry.require(initial.message.sender)
    same_input = Message(
        user.name,
        "alice",
        "uncertain",
        InfoKind,
        timestamp=initial.message.timestamp,
    )
    with pytest.raises(RelationViolationError, match="already exists"):
        comms.bus.publisher.publish_ordinary(
            same_input,
            origin=HumanOrigin(user.name, user.created_at, user.worktree),
        )
    assert len(list(comms.bus.full_history_page().messages)) == 1


def test_reservation_only_unknown_blocks_same_id_and_later_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms, _, root_id, _ = _root(tmp_path)
    original_open = os.open
    captured = {}
    original_append = comms.bus.log.append_private_unlocked

    def fail_bus_open(path, *args, **kwargs):
        if Path(path) == comms.bus.log.path and args[0] & os.O_WRONLY:
            raise OSError("bus open failed after durable marker reservation")
        return original_open(path, *args, **kwargs)

    def capture_then_append(metadata, row):
        captured.update(row)
        original_append(metadata, row)

    monkeypatch.setattr(os, "open", fail_bus_open)
    monkeypatch.setattr(comms.bus.log, 'append_private_unlocked', capture_then_append)
    with pytest.raises(HumanInitialUnknownError, match="UNKNOWN") as raised:
        comms.messaging.send_user_message("alice", "first input", worktree=str(tmp_path))
    assert raised.value.wire_root_id == root_id
    assert captured["id"] == raised.value.message_id
    assert list(comms.bus.full_history_page().messages) == []
    assert comms.bus.log.private_marker_unlocked().last_seq == 1
    monkeypatch.setattr(os, "open", original_open)
    second = Comms(comms.root)
    user = second.registry.require(captured["from"])
    same = Message(user.name, "alice", "first input", InfoKind, timestamp=captured["ts"])
    with pytest.raises(RelationViolationError, match="reservation has UNKNOWN"):
        second.bus.publisher.publish_ordinary(
            same,
            origin=HumanOrigin(user.name, user.created_at, user.worktree),
        )
    assert list(second.bus.full_history_page().messages) == []
    with pytest.raises(RelationViolationError, match="reservation has UNKNOWN"):
        second.messaging.send_user_message("alice", "different new human input", worktree=str(tmp_path))
    assert list(second.bus.full_history_page().messages) == []
    later_agent = publish_initial(second, "alice", "bob", "unrelated agent input")
    assert later_agent.seq == 2
    with pytest.raises(RelationViolationError, match="sequence gap has UNKNOWN"):
        second.messaging.send_user_message("alice", "new human input", worktree=str(tmp_path))
    assert len(list(second.bus.full_history_page().messages)) == 1


def test_cancellation_after_append_entry_is_typed_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms, _, root_id, _ = _root(tmp_path)
    original = comms.bus.log.append_private_unlocked

    def cancel_after_append(metadata, row):
        original(metadata, row)
        raise asyncio.CancelledError("cancelled after durable append")

    monkeypatch.setattr(comms.bus.log, 'append_private_unlocked', cancel_after_append)
    with pytest.raises(HumanInitialUnknownError, match="outcome UNKNOWN") as raised:
        comms.messaging.send_user_message("alice", "cancelled after append", worktree=str(tmp_path))
    assert comms.bus.log.read_delivery_cohort(root_id, raised.value.wire_seq).message.message_id == (
        raised.value.message_id
    )
    assert len(list(comms.bus.full_history_page().messages)) == 1


def test_preappend_route_or_target_denial_has_no_bus_row(tmp_path: Path) -> None:
    comms, _, _, _ = _root(tmp_path)
    with pytest.raises(RelationViolationError, match="not a routable"):
        comms.messaging.send_user_message("#any", "wrong route", worktree=str(tmp_path))
    with pytest.raises(UnregisteredThreadError, match="not a registered thread"):
        comms.messaging.send_user_message("unregistered", "wrong peer", worktree=str(tmp_path))
    user = comms.messaging.user_identity(str(tmp_path))
    with pytest.raises(RelationViolationError, match="Human messages"):
        comms.messaging.send(user.name, "alice", "agent impersonation").message
    assert comms.bus.log.latest_sequence() == 0


def test_cooperating_process_objects_serialize_distinct_user_inputs(tmp_path: Path) -> None:
    comms, _, root_id, _ = _root(tmp_path)
    roots = [Comms(comms.root), Comms(comms.root)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(wire.messaging.send_user_message, "#team", f"input-{n}", worktree=str(tmp_path))
            for n, wire in enumerate(roots)
        ]
        messages = [future.result(timeout=5) for future in futures]
    assert sorted(message.seq for message in messages) == [1, 2]
    assert {
        comms.bus.log.read_delivery_cohort(root_id, message.seq).message.message_id
        for message in messages
    } == {message.message_id for message in messages}


def test_separate_processes_serialize_one_private_user_identity(tmp_path: Path) -> None:
    comms, _, root_id, _ = _root(tmp_path)
    program = (
        "import sys\nfrom agent_comms.comms import Comms\nm = Comms(sys.argv[1]).messaging.send_user_message('#team', sys.argv[2], worktree=sys.argv[3])\nprint(m.seq, m.message_id)"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", program, str(comms.root), f"child-{n}", str(tmp_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for n in range(2)
    ]
    receipts = [process.communicate(timeout=15) for process in processes]
    assert all(process.returncode == 0 for process in processes), receipts
    seqs = sorted(int(stdout.split()[0]) for stdout, _ in receipts)
    assert seqs == [1, 2]
    members = comms.registry.all_threads().values()
    humans = [human for thread in members for human in thread.role.human(thread)]
    assert len(humans) == 1
    assert [comms.bus.log.read_delivery_cohort(root_id, seq).message.seq for seq in seqs] == seqs


async def test_cancelled_ui_wait_does_not_replay_a_blocked_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    comms, _, root_id, _ = _root(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = comms.bus.log.append_private_unlocked
    calls = 0

    def blocked_append(metadata, row):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(5)
        original(metadata, row)

    monkeypatch.setattr(comms.bus.log, 'append_private_unlocked', blocked_append)
    pending = asyncio.create_task(
        asyncio.to_thread(comms.messaging.send_user_message, "alice", "cancelled", worktree=str(tmp_path))
    )
    assert await asyncio.to_thread(entered.wait, 5)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    release.set()
    # The worker may have committed despite a cancelled caller; inspect, never resubmit.
    await asyncio.to_thread(entered.wait, 5)
    for _ in range(100):
        if comms.bus.log.latest_sequence() == 1:
            break
        await asyncio.sleep(0.01)
    assert calls == 1
    assert comms.bus.log.read_delivery_cohort(root_id, 1).message.body == "cancelled"
