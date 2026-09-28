"""A stopped source preserves its pending and uncertain bytes in an archive."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms import supervised_cutover
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.coordination_store import MutationStore
from agent_comms.errors import RelationViolationError
from agent_comms.goal_presentation import GoalExecutionState, GoalWaitTarget
from agent_comms.goal_waits import GoalWait, GoalWaits
from agent_comms.goals import Goal
from agent_comms.input_disposition import InputDispositions
from agent_comms.operations import Comms
from agent_comms.supervised_cutover import (
    LegacyInventory,
    OwnerWitness,
    archive_stopped_root,
    stage_private_participants,
)
from agent_comms.thread_status import RunningThreadStatus, StoppedThreadStatus
from agent_comms.threads import Thread

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux cutover")


def test_archive_refuses_live_owner_then_preserves_pending_and_unknown(tmp_path):
    comms = Comms(tmp_path / "wire")
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        for name in ("sender", "receiver"):
            comms.register(Thread(name, frozenset(), str(tmp_path), pid=process.pid))
        comms.send("sender", "receiver", "not yet read")
        InputDispositions(comms.root).record(
            "test:unknown", seq=None, owner="receiver", admission=1,
            target="receiver", text="uncertain input",
        )
        with MutationStore(str(comms.root / "coordination.sqlite3")) as store:
            store.register_participant("old-owner", "Old Owner", "sender", committed=True)
        destination = tmp_path / "private-archive" / "snapshot"
        with pytest.raises(RelationViolationError, match="all old owners stopped"):
            archive_stopped_root(comms, destination)
        for name in ("sender", "receiver"):
            comms.registry.unregister(name)
        with pytest.raises(RelationViolationError, match="all old owners stopped"):
            archive_stopped_root(comms, destination)
        assert not destination.exists()
        process.terminate()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    receipt = archive_stopped_root(comms, destination)
    manifest = json.loads((destination / ".archive-manifest").read_text())
    assert receipt.pending_messages == manifest["pending_messages"] == 1
    assert receipt.unknown_inputs == manifest["unknown_inputs"] == 1
    with sqlite3.connect(destination / "coordination.sqlite3") as archived:
        assert archived.execute("PRAGMA user_version").fetchone()[0] == 2
        assert archived.execute("SELECT count(*) FROM participants").fetchone()[0] == 1
    for name, evidence in manifest["files"].items():
        assert (destination / name).read_bytes() == (comms.root / name).read_bytes()
        assert hashlib.sha256((destination / name).read_bytes()).hexdigest() == evidence["sha256"]
    assert (destination / "bus.jsonl").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="already exists"):
        archive_stopped_root(comms, destination)


def test_archive_refuses_path_redirected_back_into_source(tmp_path):
    comms = Comms(tmp_path / "wire")
    alias = tmp_path / "source-alias"
    alias.symlink_to(comms.root, target_is_directory=True)
    with pytest.raises(ValueError, match="outside the old root"):
        archive_stopped_root(comms, alias / "snapshot")


def test_private_archive_keeps_nested_pi_journal_without_legacy_dispositions(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    comms = Comms(root)
    comms.registry.register(
        Thread("owner", frozenset(), str(tmp_path), pid=0), StoppedThreadStatus()
    )
    comms.initialize_private_initial_protocol()
    comms.send_user_message("owner", "pending", worktree=str(tmp_path))
    session = root / "native-sessions" / "recipient" / "session.jsonl"
    session.parent.mkdir(parents=True, mode=0o700)
    session.write_text('{"type":"session"}\n')
    session.chmod(0o600)
    proof = Path(str(session) + ".input-proof")
    proof.write_text('{"type":"context_committed"}\n')
    proof.chmod(0o600)
    assert not (root / "input_dispositions.json").exists()

    archive = archive_stopped_root(comms, tmp_path / "archive" / "private")
    assert archive.unknown_inputs == 0
    assert (archive.path / session.relative_to(root)).read_bytes() == session.read_bytes()
    assert (archive.path / proof.relative_to(root)).read_bytes() == proof.read_bytes()
    session.write_text('{"type":"changed"}\n')
    with pytest.raises(RelationViolationError, match="changed after its cutover archive"):
        supervised_cutover._require_unchanged_archive_source(comms, archive)


def test_archive_refuses_rival_destination_after_staging(tmp_path, monkeypatch):
    comms = Comms(tmp_path / "wire")
    for name in ("sender", "receiver"):
        comms.register(Thread(name, frozenset(), str(tmp_path), pid=0))
    comms.send("sender", "receiver", "pending")
    InputDispositions(comms.root).record(
        "test:unknown", seq=None, owner="receiver", admission=1,
        target="receiver", text="uncertain input",
    )
    for name in ("sender", "receiver"):
        comms.registry.unregister(name)
    destination = tmp_path / "private-archive" / "snapshot"
    original = supervised_cutover._publish_archive_noreplace
    rival_inode: list[int] = []

    def rival_after_copy(stage, target):
        target.mkdir(mode=0o700)
        rival_inode.append(target.stat().st_ino)
        return original(stage, target)

    monkeypatch.setattr(supervised_cutover, "_publish_archive_noreplace", rival_after_copy)
    with pytest.raises(ValueError, match="already exists"):
        archive_stopped_root(comms, destination)
    assert destination.is_dir() and destination.stat().st_ino == rival_inode[0]
    assert list(destination.iterdir()) == []
    assert not list(destination.parent.glob(".cutover-archive-*"))


def test_stage_stopped_owner_into_fresh_private_root_without_old_replay(tmp_path):
    legacy = Comms(tmp_path / "legacy")
    saved = tmp_path / "saved-session.jsonl"
    saved.write_text('{"type":"session"}\n')
    saved.chmod(0o600)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    receiver_process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        legacy.register(
            Thread(
                "sender", frozenset({"team"}), str(tmp_path), pid=process.pid,
                session_file=str(saved), model="openai-codex/gpt-6-sol",
                goal=Goal("wait for receiver", "stage-goal"),
            )
        )
        legacy.register(Thread("receiver", frozenset(), str(tmp_path), pid=receiver_process.pid,
                               session_file=str(saved)))
        legacy.send("sender", "receiver", "old pending message")
        before_wait = legacy.registry.snapshot()
        GoalWaits(legacy.root / "goal_waits.json").record(
            GoalWait(
                "stage-goal", "old-wait", 0, legacy.message_high_water(),
                (GoalWaitTarget("receiver", before_wait.threads["receiver"].created_at),),
                owner_created_at=before_wait.threads["sender"].created_at,
                target_turn_generations=(None,),
            )
        )
        assert legacy.goal_execution("sender").state is GoalExecutionState.STANDBY
        InputDispositions(legacy.root).record(
            "stage:unknown", seq=None, owner="sender", admission=1,
            target="sender", text="uncertain old input",
        )
        snapshot = legacy.registry.snapshot()
        thread = snapshot.threads["sender"]
        info = saved.stat()
        witness = OwnerWitness(
            "sender", process.pid, thread.created_at,
            snapshot.admission_generations["sender"],
            supervised_cutover._process_start_ticks(process.pid),
            saved, info.st_dev, info.st_ino, tmp_path, "pi", "",
        )
        receiver = snapshot.threads["receiver"]
        receiver_witness = OwnerWitness(
            "receiver", receiver_process.pid, receiver.created_at,
            snapshot.admission_generations["receiver"],
            supervised_cutover._process_start_ticks(receiver_process.pid),
            saved, info.st_dev, info.st_ino, tmp_path, "pi", "",
        )
        inventory = LegacyInventory(
            legacy.root, (witness, receiver_witness), (), (), (("receiver", 1),), 1
        )
        process.terminate()
        process.wait(timeout=5)
        receiver_process.terminate()
        receiver_process.wait(timeout=5)
        legacy.registry.unregister("sender")
        legacy.registry.unregister("receiver")
        archive = archive_stopped_root(legacy, tmp_path / "archive" / "snapshot")
        private = Comms(tmp_path / "private")
        incomplete = Comms(tmp_path / "incomplete-private")
        with pytest.raises(RelationViolationError, match="every exact target staged"):
            stage_private_participants(
                legacy, incomplete, archive, inventory, ["sender"]
            )
        assert not (incomplete.root / "bus_meta.json").exists()
        root_id, selected = stage_private_participants(
            legacy, private, archive, inventory, ["sender", "receiver"]
        )
        assert private.bus._private_marker_unlocked()["claim_envelopes_version"] == 1
        assert (private.root / "private_bus_checkpoint.sqlite3").is_file()
        assert json.loads((private.root / "bus_meta.json").read_text())["checkpoint_version"] == 1
        staged = private.registry.require("sender")
        assert selected == (witness, receiver_witness)
        assert staged.pid == 0 and staged.session_file == str(saved)
        assert staged.created_at == thread.created_at
        assert private.registry.status("sender") == StoppedThreadStatus()
        with MutationStore(str(private.root / "coordination.sqlite3")) as store:
            sender = store.participant(stable_thread_lookup(staged.created_at))
            assert sender.display_name == "sender"
            assert store.participant(
                stable_thread_lookup(private.registry.require("receiver").created_at)
            ).display_name == "receiver"
        with sqlite3.connect(private.root / "coordination.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM cohort_schema_meta").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM response_schema_meta").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM native_runtime_schema_meta").fetchone()[0] == 1
        assert private.goal_execution("sender").state is GoalExecutionState.STANDBY
        migrated_wait = GoalWaits(private.root / "goal_waits.json").read()["stage-goal"]
        assert migrated_wait.after_seq == 0
        assert migrated_wait.target_turn_generations == (None,)
        assert (private.root / "bus.jsonl").read_bytes() == b""
        assert archive.pending_messages == archive.unknown_inputs == 1
        private.registry.register(replace(staged, pid=os.getpid()), RunningThreadStatus())
        private.registry.register(
            replace(private.registry.require("receiver"), pid=os.getpid()), RunningThreadStatus()
        )
        message = private.send_initial_cohort("sender", "receiver", "new private input")
        initial = private.bus.read_initial_cohort(root_id, message.seq)
        assert initial.audience.recipients[0].canonical_thread == "receiver"
        assert [item.body for item in legacy.inbox("receiver")] == ["old pending message"]
        with pytest.raises(RelationViolationError, match="fresh private root"):
            stage_private_participants(legacy, private, archive, inventory, ["sender", "receiver"])
        another = Comms(tmp_path / "another-private")
        InputDispositions(legacy.root).record(
            "stage:late", seq=None, owner="sender", admission=1,
            target="sender", text="late old input",
        )
        with pytest.raises(RelationViolationError, match="changed after its cutover archive"):
            stage_private_participants(legacy, another, archive, inventory, ["sender", "receiver"])
        assert not (another.root / "bus_meta.json").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if receiver_process.poll() is None:
            receiver_process.kill()
            receiver_process.wait(timeout=5)
