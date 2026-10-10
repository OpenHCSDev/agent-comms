import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms import Registration
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError, UnregisteredThreadError
from agent_comms.goals import Goal
from agent_comms.message_bus import MessageBus
from agent_comms.messages import Message, MessageType
from agent_comms.native_input_owner import RegistryOwner
from agent_comms.runtime_info import AgentRuntimeInfo, RuntimeInfoStore
from agent_comms.shared_ledger import SharedLedger
from agent_comms.thread_status import DeletingThreadStatus, RunningThreadStatus, StoppedThreadStatus
from agent_comms.threads import Thread, current_thread


@pytest.mark.skipif(os.name == "nt", reason="private POSIX ownership unavailable on Windows")
def test_private_marker_checks_relative_and_absolute_ancestor_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unsafe = tmp_path / "nonsticky-writable"
    unsafe.mkdir()
    unsafe.chmod(0o777)
    monkeypatch.chdir(unsafe)
    for root in (Path("relative-wire"), unsafe / "absolute-wire"):
        with pytest.raises(RelationViolationError, match="ancestry is not trusted"):
            Comms(root, private_initial_writes=True).messaging.initialize_private_initial_protocol()
        assert not (root / "bus_meta.json").exists()
        assert not (root / "bus.jsonl").exists()
    monkeypatch.chdir(tmp_path)
    safe = Comms(Path("safe-relative"), private_initial_writes=True)
    assert len(safe.messaging.initialize_private_initial_protocol()) == 32


@pytest.mark.skipif(os.name == "posix", reason="Windows private-bus fail-closed contract")
def test_private_bus_rejects_unattestable_windows_ownership(tmp_path: Path) -> None:
    registry = Registration(tmp_path / "registry.json")
    bus = MessageBus(tmp_path / "bus.jsonl", registry, private_initial_writes=True)
    with pytest.raises(RelationViolationError, match="POSIX ownership"):
        bus.publisher.initialize_private_protocol()


class TestAgentRuntimeInfo:
    def test_context_percent_and_persistence(self, tmp_path: Path):
        info = AgentRuntimeInfo(
            thread="a", model="provider/model", context_used=25, context_size=100, timestamp=1.0
        )
        assert info.context_percent == 25
        store = RuntimeInfoStore(tmp_path / "runtime.json")
        store.set(info)
        assert store.read()["a"] == info

    def test_unknown_context_stays_unknown(self):
        assert AgentRuntimeInfo(thread="a", context_size=100, timestamp=1.0).context_percent is None

    def test_negative_context_is_rejected(self):
        with pytest.raises(ValueError, match="negative"):
            AgentRuntimeInfo(thread="a", context_used=-1, timestamp=1.0)


class TestGoalRevision:
    @pytest.mark.parametrize("revision", [True, 1.0, "1", -1, 1 << 63])
    def test_rejects_noncanonical_or_exhausted_revision(self, revision):
        with pytest.raises(ValueError, match="revision"):
            Goal(text="work", id="goal-id", revision=revision)

    def test_old_registry_goal_without_revision_reopens_at_zero(self, tmp_path: Path):
        path = tmp_path / "registry.json"
        registry = Registration(path)
        registry.register(
            Thread(name="owner", tags=frozenset(), worktree="/wt", goal=Goal("work", "old-id"))
        )
        raw = json.loads(path.read_text())
        del raw["threads"]["owner"]["goal"]["revision"]
        path.write_text(json.dumps(raw))
        restored = Registration(path).require("owner").goal
        assert restored == Goal("work", "old-id", revision=0)


class TestThreadDeclaration:
    def test_constructing_declares(self):
        thread = Thread(name="a", tags=frozenset(), worktree="/wt")
        assert thread.name == "a"
        assert thread.is_fork is False

    def test_rejects_bad_name_characters(self):
        with pytest.raises(ValueError, match="alphanumeric"):
            Thread(name="bad name!", tags=frozenset(), worktree="/wt")

    def test_rejects_empty_name(self):
        with pytest.raises(ValueError):
            Thread(name="", tags=frozenset(), worktree="/wt")

    def test_rejects_empty_worktree(self):
        with pytest.raises(ValueError, match="worktree"):
            Thread(name="a", tags=frozenset(), worktree="")

    def test_rejects_self_parent(self):
        with pytest.raises(RelationViolationError, match="own parent"):
            Thread(name="a", tags=frozenset(), worktree="/wt", parent="a")

    def test_fork_provenance(self):
        fork = Thread(name="b", tags=frozenset(), worktree="/wt", parent="a")
        assert fork.is_fork is True
        assert fork.parent == "a"

    def test_frozen(self):
        thread = Thread(name="a", tags=frozenset(), worktree="/wt")
        with pytest.raises(AttributeError):
            thread.name = "b"  # type: ignore[misc]


class TestMessageDeclaration:
    def test_constructing_declares(self):
        message = Message(sender="a", target="b", body="hello", type=MessageType.INFO)
        assert message.sender == "a" and message.target == "b"

    def test_rejects_self_message(self):
        with pytest.raises(RelationViolationError, match="itself"):
            Message(sender="a", target="a", body="hello", type=MessageType.INFO)

    def test_rejects_empty_sender(self):
        with pytest.raises(RelationViolationError, match="sender"):
            Message(sender="", target="b", body="hello", type=MessageType.INFO)

    def test_rejects_empty_target(self):
        with pytest.raises(RelationViolationError, match="target"):
            Message(sender="a", target="", body="hello", type=MessageType.INFO)

    def test_rejects_empty_body(self):
        with pytest.raises(ValueError, match="body"):
            Message(sender="a", target="b", body="", type=MessageType.INFO)

    def test_id_is_identity_not_ordering(self):
        # Hash identity: two identical messages share an id, ids are not
        # monotonic in time, so delivery order must come from seq.
        a = Message(sender="a", target="b", body="x", type=MessageType.INFO, timestamp=1.0)
        b = Message(sender="a", target="b", body="x", type=MessageType.INFO, timestamp=1.0)
        assert a.message_id == b.message_id

    def test_wire_roundtrip(self):
        original = Message(sender="a", target="b", body="hello", type=MessageType.HANDOFF, seq=7)
        restored = Message.from_wire(original.to_wire())
        assert restored.sender == original.sender
        assert restored.target == original.target
        assert restored.body == original.body
        assert restored.type is MessageType.HANDOFF
        assert restored.seq == 7


class TestRegistration:
    def test_register_and_require(self, tmp_path: Path):
        registry = Registration(tmp_path / "registry.json")
        thread = Thread(name="a", tags=frozenset(), worktree="/wt")
        registry.register(thread)
        assert registry.require("a") is not None
        assert "a" in registry

    def test_fail_closed_unknown_reference(self, tmp_path: Path):
        registry = Registration(tmp_path / "registry.json")
        with pytest.raises(UnregisteredThreadError):
            registry.require("missing")

    def test_status_transitions(self, tmp_path: Path):
        registry = Registration(tmp_path / "registry.json")
        registry.register(Thread(name="a", tags=frozenset(), worktree="/wt"))
        assert registry.status("a") == RunningThreadStatus()
        registry.unregister("a")
        assert registry.status("a") == StoppedThreadStatus()
        # Unregistering a stopped (but known) thread is idempotent.
        registry.unregister("a")
        assert registry.status("a") == StoppedThreadStatus()
        # Only unknown references fail closed.
        with pytest.raises(UnregisteredThreadError):
            registry.unregister("ghost")

    def test_stop_then_heartbeat_cannot_reclaim_initial_owner_generation(self, tmp_path: Path):
        registry = Registration(tmp_path / "registry.json")
        registry.register(Thread(name="a", tags=frozenset(), worktree="/wt", process_identity=ProcessIdentity.capture(os.getpid())))
        snapshot = registry.snapshot()
        expected = RegistryOwner.capture_local(snapshot, "a").thread
        admission_generation = snapshot.owner_identity("a").generation
        registry.unregister("a")
        registry.heartbeat("a")  # Presence can resume; a stale turn CAS cannot reclaim it.
        assert registry.require("a") == expected
        assert registry.status("a") == RunningThreadStatus()
        assert registry.snapshot().owner_identity("a").generation > admission_generation
        with pytest.raises(RelationViolationError, match="live owner generation changed"):
            registry.lease_live_turn_with_generation(
                expected, "new-turn", expected_owner_generation=admission_generation
            )
        assert registry.require("a").active_turn is None

    def test_owner_admission_survives_session_metadata_and_rotates_on_restart(
        self, tmp_path: Path
    ) -> None:
        registry = Registration(tmp_path / "registry.json")
        owner = Thread(name="a", tags=frozenset(), worktree="/wt", process_identity=ProcessIdentity.capture(os.getpid()))
        registry.register(owner)
        before = registry.snapshot().admission_generations["a"]
        registry.register(replace(owner, session_file=str(tmp_path / "session.jsonl")))
        assert registry.snapshot().admission_generations["a"] == before
        assert Registration(registry.store.path).snapshot().admission_generations["a"] == before

        registry.register(replace(registry.require("a"), process_identity=ProcessIdentity.capture(os.getppid())))
        assert registry.snapshot().admission_generations["a"] > before

    def test_owner_admission_follows_same_owner_rename(self, tmp_path: Path) -> None:
        registry = Registration(tmp_path / "registry.json")
        registry.register(Thread(name="a", tags=frozenset(), worktree="/wt", process_identity=ProcessIdentity.capture(os.getpid())))
        before = registry.snapshot().admission_generations["a"]
        registry.rename("a", "renamed-a")
        snapshot = Registration(registry.store.path).snapshot()
        assert snapshot.admission_generations["renamed-a"] == before
        assert "a" not in snapshot.admission_generations

    def test_rename_reclaims_own_alias_without_losing_owner_or_turn(self, tmp_path: Path) -> None:
        registry = Registration(tmp_path / "registry.json")
        registry.register(
            Thread(
                name="agent-comms-ux",
                tags=frozenset(),
                worktree="/wt",
                process_identity=ProcessIdentity.capture(os.getpid()),
                goal=Goal("Finish the goal", "goal-1"),
            )
        )
        registry.register(
            Thread(name="child", tags=frozenset(), worktree="/wt", parent="agent-comms-ux")
        )
        original = registry.require("agent-comms-ux")
        registry.rename("agent-comms-ux", "pr17")
        admission = registry.snapshot().admission_generations["pr17"]
        registry.lease_local_turn("pr17", "goal-turn")

        assert registry.rename("pr17", "agent-comms-ux") == ("pr17", "agent-comms-ux")

        reopened = Registration(registry.store.path)
        snapshot = reopened.snapshot()
        assert snapshot.aliases == {"pr17": "agent-comms-ux"}
        assert reopened.require("pr17").name == "agent-comms-ux"
        assert reopened.require("agent-comms-ux").created_at == original.created_at
        assert reopened.require("agent-comms-ux").goal == original.goal
        assert reopened.require("agent-comms-ux").active_turn is not None
        assert reopened.require("agent-comms-ux").active_turn.id == "goal-turn"
        assert reopened.require("child").parent == "agent-comms-ux"
        assert snapshot.admission_generations["agent-comms-ux"] == admission
        assert reopened.release_turn(reopened.require("pr17").turn_lease)[0]

    def test_other_owner_writes_do_not_invalidate_private_generation(self, tmp_path: Path):
        registry = Registration(tmp_path / "registry.json")
        for name in ("a", "b"):
            registry.register(Thread(name=name, tags=frozenset(), worktree="/wt", process_identity=ProcessIdentity.capture(os.getpid())))
        snapshot = registry.snapshot()
        owner = RegistryOwner.capture_local(snapshot, "a").thread
        admission_generation = snapshot.owner_identity("a").generation
        registry.heartbeat("b")
        snapshot = registry.snapshot()
        other = RegistryOwner.capture_local(snapshot, "b").thread
        other_generation = snapshot.owner_identity("b").generation
        registry.lease_live_turn_with_generation(
            other, "other", expected_owner_generation=other_generation
        )
        snapshot = registry.snapshot()
        assert RegistryOwner.capture_local(snapshot, "a").thread == owner
        assert snapshot.owner_identity("a").generation == admission_generation
        turn, _ = registry.lease_live_turn_with_generation(
            owner, "mine", expected_owner_generation=admission_generation
        )
        assert turn.active_turn is not None and turn.active_turn.id == "mine"
        assert registry.release_turn(turn.turn_lease)[0]
        assert not registry.release_turn(turn.turn_lease)[0]
        assert "owner_epoch" not in owner.to_wire()

    @pytest.mark.parametrize("revocation", ["stop", "finish"])
    def test_registering_saved_turn_never_attests_a_revoked_incarnation(
        self, tmp_path: Path, revocation: str
    ) -> None:
        registry = Registration(tmp_path / "registry.json")
        registry.register(Thread(name="a", tags=frozenset(), worktree="/wt", process_identity=ProcessIdentity.capture(os.getpid())))
        snapshot = registry.snapshot()
        owner = RegistryOwner.capture_local(snapshot, "a").thread
        admission_generation = snapshot.owner_identity("a").generation
        leased, owner_generation = registry.lease_live_turn_with_generation(
            owner, "claimed", expected_owner_generation=admission_generation
        )
        snapshot = registry.snapshot()
        assert RegistryOwner.capture_local(snapshot, "a").thread == leased
        assert snapshot.owner_identity("a").generation == owner_generation
        if revocation == "stop":
            registry.unregister("a")
        else:
            assert registry.release_turn(registry.require("a").turn_lease)[0]
        registry.register(leased)
        assert registry.require("a").active_turn.admission_generation is None
        with pytest.raises(
            RelationViolationError, match="live owner turn admission is no longer current"
        ):
            registry.live_owner_with_admission("a")
        if revocation == "stop":
            assert registry.snapshot().owner_generations["a"] > owner_generation
        else:
            assert registry.snapshot().owner_generations["a"] == owner_generation
        assert "turn_epochs" not in leased.to_wire()

    def test_comms_begin_turn_cannot_revive_stopped_owner(self, tmp_path: Path) -> None:
        comms = Comms(tmp_path / "wire")
        comms.registry.declare(Thread(name="a", tags=frozenset(), worktree="/wt", process_identity=ProcessIdentity.capture(os.getpid())))
        comms.registry.unregister("a")
        with pytest.raises(RelationViolationError, match="stopped or unavailable"):
            comms.agents.begin_turn("a", "revived")
        assert comms.registry.status("a") == StoppedThreadStatus()
        assert comms.registry.require("a").active_turn is None


    @pytest.mark.skipif(os.name == "nt", reason="private POSIX ownership unavailable on Windows")
    def test_private_marker_can_precede_first_registry_snapshot(self, tmp_path: Path) -> None:
        root = tmp_path / "private-fresh"
        root.mkdir(mode=0o700)
        comms = Comms(root, private_initial_writes=True)
        comms.messaging.initialize_private_initial_protocol()
        reopened = Comms(root)
        reopened.registry.declare(Thread(name="a", tags=frozenset(), worktree="/wt", process_identity=ProcessIdentity.capture(os.getpid())))
        assert reopened.registry.snapshot().owner_identity("a").generation > 0

    @pytest.mark.skipif(os.name == "nt", reason="private POSIX ownership unavailable on Windows")
    def test_private_marker_does_not_bootstrap_stripped_owner_generation(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "private-wire"
        root.mkdir(mode=0o700)
        comms = Comms(root, private_initial_writes=True)
        comms.registry.declare(Thread(name="a", tags=frozenset(), worktree="/wt", process_identity=ProcessIdentity.capture(os.getpid())))
        comms.messaging.initialize_private_initial_protocol()
        registry_path = root / "registry.json"
        data = json.loads(registry_path.read_text())
        for field in ("owners", "admissions"):
            data.pop(field)
        registry_path.write_text(json.dumps(data))
        with pytest.raises(RelationViolationError, match="guard does not match"):
            comms.agents.begin_turn("a", "must-not-claim")
        with pytest.raises(RelationViolationError, match="guard does not match"):
            comms.registry.register(Thread(name="a", tags=frozenset(), worktree="/wt"))
        assert json.loads(registry_path.read_text())["threads"]["a"].get("active_turn") is None

    @pytest.mark.skipif(sys.platform != "linux", reason="fault injection uses Linux /proc/self/fd")
    @pytest.mark.parametrize("fail_at", ["pending", "replacement", "directory", "commit"])
    def test_private_guard_faults_never_promote_an_unfsynced_owner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_at: str
    ) -> None:
        from agent_comms import store_files

        root = tmp_path / "isolated-private-root"
        comms = Comms(root, private_initial_writes=True)
        comms.registry.declare(Thread(name="a", tags=frozenset(), worktree="/wt", process_identity=ProcessIdentity.capture(os.getpid())))
        root_id = comms.messaging.initialize_private_initial_protocol()
        assert len(root_id) == 32
        cold = Registration(root / "registry.json")
        assert cold.require("a").name == "a"  # cache the old revision
        original_fsync = os.fsync
        original_write = store_files._atomic_write_text
        guard_calls = 0

        def fsync(fd: int) -> None:
            nonlocal guard_calls
            name = os.readlink(f"/proc/self/fd/{fd}")
            if name.endswith(".registry-owner-guard"):
                guard_calls += 1
                if (fail_at == "pending" and guard_calls == 1) or (
                    fail_at == "commit" and guard_calls == 2
                ):
                    raise OSError("injected guard fsync failure")
            if fail_at == "directory" and name == str(root):
                raise OSError("injected directory fsync failure")
            original_fsync(fd)

        def write(path: Path, text: str, **kwargs) -> None:
            if fail_at == "replacement" and path == root / "registry.json":
                raise OSError("injected replacement failure")
            original_write(path, text, **kwargs)

        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(store_files, "_atomic_write_text", write)
        with pytest.raises((OSError, RelationViolationError), match="injected|guard"):
            comms.registry.register(
                Thread(name="b", tags=frozenset(), worktree="/wt", process_identity=ProcessIdentity.capture(os.getpid()))
            )
        monkeypatch.undo()
        # Failed COMMITTED fsync follows successful directory fsync. Complete
        # visible commit bytes are safe; on restart they may also be absent.
        if fail_at == "commit":
            assert Registration(root / "registry.json").require("b").name == "b"
        elif fail_at in {"pending", "replacement"}:
            # An abandoned write: the replacement never reached the registry,
            # so the previous commit stands and the next write proceeds.
            assert "b" not in json.loads((root / "registry.json").read_text())["threads"]
            assert cold.require("a").name == "a"
            assert Registration(root / "registry.json").require("a").name == "a"
            comms.registry.register(
                Thread(name="b", tags=frozenset(), worktree="/wt", process_identity=ProcessIdentity.capture(os.getpid()))
            )
            assert Registration(root / "registry.json").require("b").name == "b"
        else:
            with pytest.raises(RelationViolationError, match="Private registry guard"):
                cold.require("a")
            with pytest.raises(RelationViolationError, match="Private registry guard"):
                Registration(root / "registry.json")
            child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from agent_comms import Registration; "
                    "from pathlib import Path; import sys; "
                    "Registration(Path(sys.argv[1])).require('a')",
                    str(root / "registry.json"),
                ],
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
                env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
            )
            assert child.returncode != 0 and "Private registry guard" in child.stderr

    @pytest.mark.skipif(sys.platform != "linux", reason="fault injection uses Linux /proc/self/fd")
    def test_private_marker_directory_fsync_failure_leaves_guard_pending(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "new-private"
        comms = Comms(root, private_initial_writes=True)
        comms.registry.declare(Thread(name="sender", tags=frozenset(), worktree="/wt"))
        original_fsync = os.fsync
        def fsync(fd: int) -> None:
            # Target the actual marker publication rather than an ordinal:
            # current private sidecars fsync this directory before the guard.
            if (os.readlink(f"/proc/self/fd/{fd}") == str(root)
                    and (root / "bus_meta.json").exists()):
                raise OSError("injected marker directory fsync failure")
            original_fsync(fd)

        monkeypatch.setattr(os, "fsync", fsync)
        with pytest.raises(OSError, match="injected marker"):
            comms.messaging.initialize_private_initial_protocol()
        monkeypatch.undo()
        assert (root / ".registry-owner-guard").exists()
        assert (root / "bus_meta.json").exists()
        with pytest.raises(RelationViolationError, match="guard is pending"):
            Registration(root / "registry.json")
        with pytest.raises(RelationViolationError, match="read-only"):
            comms.messaging.initialize_private_initial_protocol()  # never auto-repair

    @pytest.mark.skipif(sys.platform != "linux", reason="fault injection uses Linux /proc/self/fd")
    @pytest.mark.parametrize("stage", ["pending", "commit"])
    def test_torn_guard_slot_never_falls_back_to_older_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
    ) -> None:
        root = tmp_path / "private"
        comms = Comms(root, private_initial_writes=True)
        comms.registry.declare(Thread(name="a", tags=frozenset(), worktree="/wt"))
        comms.messaging.initialize_private_initial_protocol()
        original_pwrite = os.pwrite
        guard_calls = 0

        def pwrite(fd: int, data: bytes, offset: int) -> int:
            nonlocal guard_calls
            if os.readlink(f"/proc/self/fd/{fd}").endswith(".registry-owner-guard"):
                guard_calls += 1
                if (stage == "pending" and guard_calls == 1) or (
                    stage == "commit" and guard_calls == 2
                ):
                    return original_pwrite(fd, data[: len(data) // 2], offset)
            return original_pwrite(fd, data, offset)

        monkeypatch.setattr(os, "pwrite", pwrite)
        with pytest.raises(RelationViolationError, match="slot write was incomplete"):
            comms.registry.register(Thread(name="b", tags=frozenset(), worktree="/wt"))
        monkeypatch.undo()
        with pytest.raises(RelationViolationError, match="slot checksum"):
            Registration(root / "registry.json")

    @pytest.mark.skipif(os.name == "nt", reason="private POSIX ownership unavailable on Windows")
    @pytest.mark.parametrize("damage", ["missing", "corrupt", "truncated", "strip_epochs"])
    def test_private_guard_rejects_disk_downgrade_even_with_cached_revision(
        self, tmp_path: Path, damage: str
    ) -> None:
        root = tmp_path / "guarded"
        comms = Comms(root, private_initial_writes=True)
        comms.registry.declare(Thread(name="a", tags=frozenset(), worktree="/wt", process_identity=ProcessIdentity.capture(os.getpid())))
        comms.messaging.initialize_private_initial_protocol()
        cold = Registration(root / "registry.json")
        assert cold.require("a").name == "a"
        guard = root / ".registry-owner-guard"
        if damage == "missing":
            guard.unlink()
        elif damage == "corrupt":
            raw = bytearray(guard.read_bytes())
            raw[0] ^= 1
            guard.write_bytes(raw)
        elif damage == "truncated":
            guard.write_bytes(guard.read_bytes()[:-1])
        else:
            path = root / "registry.json"
            data = json.loads(path.read_text())
            data.pop("owners")
            data.pop("admissions")
            path.write_text(json.dumps(data))  # simulated old writer ignores marker
        with pytest.raises(RelationViolationError, match="Private registry guard"):
            cold.require("a")
        with pytest.raises(RelationViolationError, match="Private registry guard"):
            comms.registry.register(Thread(name="b", tags=frozenset(), worktree="/wt"))
        # A refused read leaves no cached document; none may carry guard state.
        entry = comms.registry.store.cache.entry
        assert entry is None or "registry_guard" not in entry.document.threads["a"].to_wire()

    def test_release_turn_resolves_retained_alias_only_for_exact_lease(self, tmp_path: Path):
        registry = Registration(tmp_path / "registry.json")
        registry.register(Thread(name="a", tags=frozenset(), worktree="/wt", process_identity=ProcessIdentity.capture(os.getpid())))
        registry.rename("a", "b")
        snapshot = registry.snapshot()
        owner = RegistryOwner.capture_local(snapshot, "a").thread
        admission_generation = snapshot.owner_identity("a").generation
        assert owner.name == "b"
        leased, _ = registry.lease_live_turn_with_generation(
            owner, "claimed", expected_owner_generation=admission_generation
        )
        registry.rename("b", "c")
        assert not registry.release_turn(replace(leased.turn_lease, turn_id="other"))[0]
        assert registry.require("c").active_turn is not None
        assert registry.release_turn(leased.turn_lease)[0]
        assert registry.require("c").active_turn is None


    def test_deleting_thread_cannot_be_revived(self, tmp_path: Path):
        registry = Registration(tmp_path / "registry.json")
        thread = Thread(name="a", tags=frozenset(), worktree="/wt")
        registry.register(thread)
        registry.unregister("a")
        registry.begin_delete("a")
        assert registry.status("a") == DeletingThreadStatus()
        with pytest.raises(RelationViolationError, match="permanently deleted"):
            registry.heartbeat("a")
        with pytest.raises(RelationViolationError, match="permanently deleted"):
            registry.register(thread)

    def test_active_threads_excludes_stopped(self, tmp_path: Path):
        registry = Registration(tmp_path / "registry.json")
        registry.register(Thread(name="a", tags=frozenset(), worktree="/wt"))
        registry.register(Thread(name="b", tags=frozenset(), worktree="/wt"))
        registry.unregister("a")
        assert set(registry.active_threads()) == {"b"}
        assert set(registry.all_threads()) == {"a", "b"}

    def test_remove_drops_declaration_entirely(self, tmp_path: Path):
        registry = Registration(tmp_path / "registry.json")
        registry.register(Thread(name="a", tags=frozenset(), worktree="/wt"))
        registry.remove("a")
        assert "a" not in registry
        with pytest.raises(UnregisteredThreadError):
            registry.require("a")
        # remove is not idempotent: unknown references fail closed.
        with pytest.raises(UnregisteredThreadError):
            registry.remove("a")

    def test_persistence_roundtrip(self, tmp_path: Path):
        path = tmp_path / "registry.json"
        registry = Registration(path)
        registry.register(
            Thread(
                name="a",
                tags=frozenset({"x", "y"}),
                worktree="/wt",
                parent="p",
                task="t",
                process_identity=ProcessIdentity.capture(os.getpid()),
            )
        )
        registry.unregister("a")
        reloaded = Registration(path)
        thread = reloaded.require("a")
        assert thread.parent == "p" and thread.task == "t" and thread.process_identity == ProcessIdentity.capture(os.getpid())
        assert thread.tags == frozenset({"x", "y"})
        assert reloaded.status("a") == StoppedThreadStatus()

    def test_peers_excludes_self(self, tmp_path: Path):
        registry = Registration(tmp_path / "registry.json")
        for name in ("a", "b", "c"):
            registry.register(Thread(name=name, tags=frozenset(), worktree="/wt"))
        assert set(registry.peers("b")) == {"a", "c"}


def test_canonical_bus_reopens_and_pages_routes_without_materializing_history(tmp_path, monkeypatch):
    comms = Comms(tmp_path)
    for name in ("a", "b"):
        comms.registry.declare(Thread(name, frozenset(), str(tmp_path)))
    for sender, target in (("missing", "b"), ("a", "missing")):
        with pytest.raises(ValueError):
            comms.messaging.send_message(sender, target, "invalid")
    messages = [
        comms.messaging.send_message("a", "b", "first"),
        comms.messaging.send_message("b", "a", "second"),
        comms.messaging.send_message("a", "#all", "channel"),
    ]
    assert [row.seq for row in messages] == [1, 2, 3]
    assert len({row.message_id for row in messages}) == 3
    reopened = Comms(tmp_path)
    assert reopened.views.full_history() == messages
    reopened.registry.rename("a", "renamed")
    monkeypatch.setattr(reopened.bus.log, "full_history", lambda: pytest.fail("full scan"))
    newest = reopened.bus.dm_history_page("renamed", "b", limit=1)
    assert [row.seq for row in newest.messages] == [2]
    older = reopened.bus.dm_history_page("renamed", "b", before=2, max_bytes=1)
    assert [row.seq for row in older.messages] == [1]
    assert [row.seq for row in reopened.bus.inbox("b")] == [1, 3]
    assert reopened.bus.mark_delivered("b") == 2
    assert reopened.bus.pending_count("b") == 0
    later = reopened.messaging.send_message("renamed", "b", "later")
    assert later.seq == 4
    assert [row.seq for row in reopened.bus.inbox("b")] == [4]


class TestSharedLedger:
    def test_merge_and_read(self, tmp_path: Path):
        ledger = SharedLedger(tmp_path / "ledger.json")
        ledger.merge({"a": 1}, author="x")
        assert ledger.read()["a"] == 1

    def test_rejects_non_string_keys(self, tmp_path: Path):
        ledger = SharedLedger(tmp_path / "ledger.json")
        with pytest.raises(ValueError, match="str"):
            ledger.merge({1: "x"}, author="x")

    def test_persistence_roundtrip(self, tmp_path: Path):
        path = tmp_path / "ledger.json"
        SharedLedger(path).merge({"k": "v"}, author="x")
        assert SharedLedger(path).read()["k"] == "v"

    def test_records_author(self, tmp_path: Path):
        ledger = SharedLedger(tmp_path / "ledger.json")
        ledger.merge({}, author="me")
        assert ledger.read()["last_updated_by"] == "me"


class TestCurrentThread:
    def test_fail_closed_without_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("PI_AGENT_ID", raising=False)
        with pytest.raises(UnregisteredThreadError, match="PI_AGENT_ID"):
            current_thread()

    def test_declares_from_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.setenv("PI_AGENT_ID", "worker")
        monkeypatch.setenv("PI_AGENT_TAGS", "a, b")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PI_PARENT_ID", raising=False)
        thread = current_thread()
        assert thread.name == "worker"
        assert thread.tags == frozenset({"a", "b"})
