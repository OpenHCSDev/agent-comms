"""Cooperative local USER ingress on a marked private root; no provider or live wake."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_comms.bus_publication import HumanOrigin, stable_thread_lookup
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms, wire
from agent_comms.coordination_cohort import accept_initial_cohort
from agent_comms.coordination_store import Applied, MutationStore
from agent_comms.errors import HumanInitialUnknownError, RelationViolationError
from agent_comms.messages import Message, MessageType
from agent_comms.thread_identity import ThreadRole
from agent_comms.threads import Thread

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="private bus requires POSIX owner/directory durability"
)


def _root(tmp_path: Path) -> tuple[Comms, MutationStore, str, dict[str, str]]:
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root)
    lookups: dict[str, str] = {}
    for name, created_at in (("alice", 17001.0), ("bob", 17002.0)):
        comms.registry.register(Thread(name, frozenset({"team"}), str(root), created_at=created_at))
        lookups[name] = stable_thread_lookup(created_at)
    store = MutationStore(str(root / "coordination.sqlite"), clock_ms=lambda: 9999)
    install_private_cohort_schema(store)
    for name in lookups:
        store.register_participant(lookups[name], name, name, committed=True)
    return comms, store, comms.messaging.initialize_private_initial_protocol(), lookups


def test_private_human_channel_and_dm_seal_original_full_audience(tmp_path: Path) -> None:
    comms, store, root_id, lookups = _root(tmp_path)
    channel = comms.messaging.send_user_message("#team", "hello @alice", worktree=str(tmp_path))
    assert channel.sender_role is ThreadRole.USER
    assert [row.body for row in comms.views.channel_history("#team")] == ["hello @alice"]
    initial = comms.bus.log.read_initial_cohort(root_id, channel.seq)
    assert initial.message == channel
    assert {member.recipient_lookup for member in initial.audience.recipients} == set(
        lookups.values()
    )
    receipt = accept_initial_cohort(comms.bus, root_id, channel.seq, store)
    assert isinstance(receipt, Applied)
    assert receipt.value.member_count == 2
    assert receipt.value.claim_count == 1  # Bob is a frozen unmentioned observer.
    dm = comms.messaging.send_user_message("bob", "direct", worktree=str(tmp_path))
    assert dm.seq > channel.seq and dm.sender == channel.sender
    assert comms.views.dm_history(channel.sender, "bob")[-1] == dm
    dm_initial = comms.bus.log.read_initial_cohort(root_id, dm.seq)
    assert [
        (member.canonical_thread, member.recipient_lookup)
        for member in dm_initial.audience.recipients
    ] == [("bob", lookups["bob"])]
    accepted = accept_initial_cohort(comms.bus, root_id, dm.seq, store)
    assert isinstance(accepted, Applied)
    assert accepted.value.member_count == accepted.value.claim_count == 1


def test_legacy_user_append_stays_public_without_private_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_comms import candidate_maintenance

    called = []

    def no_private_schedule(*_args):
        called.append(True)

    monkeypatch.setattr(candidate_maintenance, "schedule_candidate_catchup", no_private_schedule)
    comms = Comms(tmp_path / "legacy")
    comms.registry.register(Thread("alice", frozenset(), str(tmp_path)))
    sent = comms.messaging.send_user_message("alice", "legacy", worktree=str(tmp_path))
    assert sent.sender_role is ThreadRole.USER
    assert comms.bus.log.full_history() == [sent]
    assert "_agent_comms_private_v1" not in (comms.root / "bus.jsonl").read_text()
    assert not called


def test_explicit_root_override_preserves_private_user_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms, _, root_id, _ = _root(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "other-home"))
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(comms.root))
    selected = wire()
    assert selected.root == comms.root
    sent = selected.messaging.send_user_message("alice", "explicit", worktree=str(tmp_path))
    assert selected.bus.log.read_initial_cohort(root_id, sent.seq).message == sent


def test_private_user_requires_typed_origin_and_exact_registered_identity(tmp_path: Path) -> None:
    comms, _, _, _ = _root(tmp_path)
    user = comms.messaging.user_identity(str(tmp_path))
    message = Message(user.name, "alice", "untrusted", MessageType.INFO)
    with pytest.raises(RelationViolationError, match="executable"):
        comms.bus.publisher.publish_initial_cohort(message)
    with pytest.raises(RelationViolationError, match="executable"):
        comms.bus.publisher.publish_ordinary(message)
    with pytest.raises(RelationViolationError, match="USER origin"):
        comms.bus.publisher.publish_ordinary(
            message,
            _human_origin=HumanOrigin(user.name, user.created_at + 1, user.worktree),
        )
    with pytest.raises(RelationViolationError, match="Human messages"):
        comms.messaging.send_message(user.name, "alice", "agent impersonation")
    assert comms.bus.log.latest_sequence() == 0


def test_duplicate_human_id_denied_before_second_append(tmp_path: Path) -> None:
    comms, _, _, _ = _root(tmp_path)
    user = comms.messaging.user_identity(str(tmp_path))
    message = Message(user.name, "alice", "one input", MessageType.INFO, timestamp=123456.0)
    origin = HumanOrigin(user.name, user.created_at, user.worktree)
    first = comms.bus.publisher.publish_ordinary(message, _human_origin=origin)
    with pytest.raises(RelationViolationError, match="already exists"):
        comms.bus.publisher.publish_ordinary(message, _human_origin=origin)
    assert [row.message_id for row in comms.bus.log.full_history()] == [first.message_id]


def test_post_append_error_is_unknown_and_never_automatically_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms, _, root_id, _ = _root(tmp_path)
    original = comms.bus.log._append_private_unlocked
    calls = 0

    def fail_after_append(metadata, row):
        nonlocal calls
        calls += 1
        original(metadata, row)
        raise OSError("simulated lost receipt after durable append")

    monkeypatch.setattr(comms.bus.log, '_append_private_unlocked', fail_after_append)
    with pytest.raises(HumanInitialUnknownError, match="do not retry") as raised:
        comms.messaging.send_user_message("alice", "uncertain", worktree=str(tmp_path))
    assert calls == 1
    error = raised.value
    assert error.wire_root_id == root_id
    initial = comms.bus.log.read_initial_cohort(root_id, error.wire_seq)
    assert initial.message.message_id == error.message_id
    assert len(comms.bus.log.full_history()) == 1
    monkeypatch.setattr(comms.bus.log, '_append_private_unlocked', original)
    user = comms.registry.require(initial.message.sender)
    same_input = Message(
        user.name,
        "alice",
        "uncertain",
        MessageType.INFO,
        timestamp=initial.message.timestamp,
    )
    with pytest.raises(RelationViolationError, match="already exists"):
        comms.bus.publisher.publish_ordinary(
            same_input,
            _human_origin=HumanOrigin(user.name, user.created_at, user.worktree),
        )
    assert len(comms.bus.log.full_history()) == 1


def test_reservation_only_unknown_blocks_same_id_and_later_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms, _, root_id, _ = _root(tmp_path)
    original_open = os.open
    captured = {}
    original_append = comms.bus.log._append_private_unlocked

    def fail_bus_open(path, *args, **kwargs):
        if Path(path) == comms.bus.log.path:
            raise OSError("bus open failed after durable marker reservation")
        return original_open(path, *args, **kwargs)

    def capture_then_append(metadata, row):
        captured.update(row)
        original_append(metadata, row)

    monkeypatch.setattr(os, "open", fail_bus_open)
    monkeypatch.setattr(comms.bus.log, '_append_private_unlocked', capture_then_append)
    with pytest.raises(HumanInitialUnknownError, match="UNKNOWN") as raised:
        comms.messaging.send_user_message("alice", "first input", worktree=str(tmp_path))
    assert raised.value.wire_root_id == root_id
    assert captured["id"] == raised.value.message_id
    assert comms.bus.log.full_history() == []
    assert comms.bus.log._private_marker_unlocked()["last_seq"] == 1
    monkeypatch.setattr(os, "open", original_open)
    second = Comms(comms.root)
    user = second.registry.require(captured["from"])
    same = Message(user.name, "alice", "first input", MessageType.INFO, timestamp=captured["ts"])
    with pytest.raises(RelationViolationError, match="reservation has UNKNOWN"):
        second.bus.publisher.publish_ordinary(
            same,
            _human_origin=HumanOrigin(user.name, user.created_at, user.worktree),
        )
    assert second.bus.log.full_history() == []
    with pytest.raises(RelationViolationError, match="reservation has UNKNOWN"):
        second.messaging.send_user_message("alice", "different new human input", worktree=str(tmp_path))
    assert second.bus.log.full_history() == []
    later_agent = second.messaging.send_initial_cohort("alice", "bob", "unrelated agent input")
    assert later_agent.seq == 2
    with pytest.raises(RelationViolationError, match="sequence gap has UNKNOWN"):
        second.messaging.send_user_message("alice", "new human input", worktree=str(tmp_path))
    assert len(second.bus.log.full_history()) == 1


def test_cancellation_after_append_entry_is_typed_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms, _, root_id, _ = _root(tmp_path)
    original = comms.bus.log._append_private_unlocked

    def cancel_after_append(metadata, row):
        original(metadata, row)
        raise asyncio.CancelledError("cancelled after durable append")

    monkeypatch.setattr(comms.bus.log, '_append_private_unlocked', cancel_after_append)
    with pytest.raises(HumanInitialUnknownError, match="outcome UNKNOWN") as raised:
        comms.messaging.send_user_message("alice", "cancelled after append", worktree=str(tmp_path))
    assert comms.bus.log.read_initial_cohort(root_id, raised.value.wire_seq).message.message_id == (
        raised.value.message_id
    )
    assert len(comms.bus.log.full_history()) == 1


def test_preappend_route_or_target_denial_has_no_bus_row(tmp_path: Path) -> None:
    comms, _, _, _ = _root(tmp_path)
    with pytest.raises(RelationViolationError, match="not routable"):
        comms.messaging.send_user_message("#any", "wrong route", worktree=str(tmp_path))
    with pytest.raises(RelationViolationError, match="Initial direct target"):
        comms.messaging.send_user_message("unregistered", "wrong peer", worktree=str(tmp_path))
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
        comms.bus.log.read_initial_cohort(root_id, message.seq).message.message_id
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
    humans = [thread for thread in members if thread.role is ThreadRole.USER]
    assert len(humans) == 1
    assert [comms.bus.log.read_initial_cohort(root_id, seq).message.seq for seq in seqs] == seqs


async def test_cancelled_ui_wait_does_not_replay_a_blocked_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    comms, _, root_id, _ = _root(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = comms.bus.log._append_private_unlocked
    calls = 0

    def blocked_append(metadata, row):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(5)
        original(metadata, row)

    monkeypatch.setattr(comms.bus.log, '_append_private_unlocked', blocked_append)
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
    assert comms.bus.log.read_initial_cohort(root_id, 1).message.body == "cancelled"
