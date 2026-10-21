"""Cooperative local USER ingress on a marked private root; no provider or live wake."""

from __future__ import annotations

from bus_record_fixture import prepare_native_comms

from agent_comms.channel_targets import CommandTarget
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.bus_publication import HumanOrigin
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms, wire
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordination_results import Applied
from agent_comms.coordinator import Coordination
from agent_comms.coordination_database import CoordinationStore
from agent_comms.launch_root import LaunchRoot
from agent_comms.errors import (
    RelationViolationError,
    UnregisteredThreadError,
)
from agent_comms.messages import Message, InfoKind
from agent_comms.thread_identity import UserThreadRole
from agent_comms.read_basis import NamedViewer

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="private bus requires POSIX owner/directory durability"
)


def _root(tmp_path: Path) -> tuple[Comms, Coordination, str, dict[str, str]]:
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = prepare_native_comms(Comms(root))
    lookups: dict[str, str] = {}
    for name, created_at in (("alice", 17001.0), ("bob", 17002.0)):
        comms.registry.declare(comms.registry.new_thread(name, frozenset({'team'}), str(root), created_at=created_at))
        lookups[name] = RecordedThreadIncarnation("fixture", created_at).lookup
    store = Coordination(str(CoordinationStore.path_in(root)), clock_ms=lambda: 9999)
    install_private_cohort_schema(store)
    for name in lookups:
        store.participants.register(lookups[name], name, name, committed=True)
    return comms, store, comms.registry.prepare_native_root().namespace, lookups


def _public_messages(comms: Comms) -> tuple[Message, ...]:
    return tuple(comms.bus.bus.message_history())


def test_private_human_channel_and_dm_seal_original_full_audience(tmp_path: Path) -> None:
    comms, store, root_id, lookups = _root(tmp_path)
    channel = comms.messaging.send_user_message("#team", "hello @alice", worktree=str(tmp_path))
    assert channel.sender_role == UserThreadRole
    assert [row.body for row in comms.views.history_page(CommandTarget.named("#team")).messages] == ["hello @alice"]
    initial = comms.bus.bus.delivery(channel.seq)
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
    assert list(comms.views.history_page(CommandTarget.named("bob"), viewer=NamedViewer(channel.sender)).messages)[-1] == dm
    dm_initial = comms.bus.bus.delivery(dm.seq)
    assert [
        (member.canonical_thread, member.recipient_lookup)
        for member in dm_initial.audience.recipients
    ] == [("bob", lookups["bob"])]
    accepted = accept_delivery_cohort(comms.bus, root_id, dm.seq, store)
    assert isinstance(accepted, Applied)
    assert accepted.value.member_count == accepted.value.assignment_count == 1


def test_selected_root_preserves_private_user_receipt(tmp_path: Path) -> None:
    comms, _, _, _ = _root(tmp_path)
    with LaunchRoot(comms.root).selected():
        selected = wire()
        assert selected.root == comms.root
        sent = selected.messaging.send_user_message("alice", "explicit", worktree=str(tmp_path))
        assert selected.bus.bus.delivery(sent.seq).message == sent


def test_duplicate_human_id_denied_before_second_append(tmp_path: Path) -> None:
    comms, _, _, _ = _root(tmp_path)
    user = comms.messaging.user_identity(str(tmp_path))
    message = Message(user.name, "alice", "one input", InfoKind, timestamp=123456.0)
    origin = HumanOrigin(user.name, user.created_at, user.worktree)
    first = comms.bus.publisher.publish_ordinary(message, origin=origin)
    with pytest.raises(RelationViolationError):
        comms.bus.publisher.publish_ordinary(message, origin=origin)
    assert [row.message_id for row in _public_messages(comms)] == [first.message_id]


def test_preappend_route_or_target_denial_has_no_bus_row(tmp_path: Path) -> None:
    comms, _, _, _ = _root(tmp_path)
    with pytest.raises(RelationViolationError):
        comms.messaging.send_user_message("#any", "wrong route", worktree=str(tmp_path))
    with pytest.raises(UnregisteredThreadError):
        comms.messaging.send_user_message("unregistered", "wrong peer", worktree=str(tmp_path))
    user = comms.messaging.user_identity(str(tmp_path))
    with pytest.raises(RelationViolationError):
        comms.messaging.send(user.name, "alice", "agent impersonation").message
    assert comms.bus.bus.revision() == 0


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
        comms.bus.bus.delivery(message.seq).message.message_id
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
    assert [comms.bus.bus.delivery(seq).message.seq for seq in seqs] == seqs


