import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_comms import (
    AgentRuntimeInfo,
    Goal,
    Message,
    MessageBus,
    MessageType,
    RelationViolationError,
    RuntimeInfoStore,
    SharedLedger,
    Thread,
    ThreadRegistry,
    ThreadStatus,
    UnregisteredThreadError,
    current_thread,
)
from agent_comms.bus_publication import PRIVATE_WIRE_FIELD, public_envelope_digest
from agent_comms.coordination import (
    MAX_PUBLICATION_PAYLOAD_BYTES,
    PublicationIntent,
    canonical_publication_key,
)
from agent_comms.operations import Comms
from agent_comms.private_registry_guard import PrivateRegistryGuard


def test_windows_snapshot_replace_retries_transient_sharing_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_comms.declarations as declarations

    source = tmp_path / "pending"
    target = tmp_path / "snapshot"
    source.write_text("new")
    target.write_text("old")
    real_replace = os.replace
    calls = 0

    def contested_replace(src: Path, dst: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            error = PermissionError(13, "sharing violation")
            error.winerror = 5
            raise error
        real_replace(src, dst)

    monkeypatch.setattr(declarations.os, "replace", contested_replace)
    declarations._replace_snapshot(source, target, windows=True)
    assert calls == 2
    assert target.read_text() == "new"


def test_windows_snapshot_replace_does_not_retry_real_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_comms.declarations as declarations

    source = tmp_path / "pending"
    target = tmp_path / "snapshot"
    source.write_text("new")
    target.write_text("old")
    calls = 0

    def refused_replace(src: Path, dst: Path) -> None:
        nonlocal calls
        calls += 1
        error = PermissionError(13, "access denied")
        error.winerror = 3
        raise error

    monkeypatch.setattr(declarations.os, "replace", refused_replace)
    with pytest.raises(PermissionError):
        declarations._replace_snapshot(source, target, windows=True)
    assert calls == 1
    assert target.read_text() == "old"


def _test_only_guard_for_handcrafted_marker(registry: ThreadRegistry) -> None:
    """Legacy-log bus tests forge a marker; this is NOT a safe cutover issuer."""
    marker = json.loads((registry._path.parent / "bus_meta.json").read_text())
    guard = PrivateRegistryGuard(registry._path, marker["wire_root_id"])
    guard.create_pending()
    guard.commit_initial()


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
            Comms(root, private_initial_writes=True).initialize_private_initial_protocol()
        assert not (root / "bus_meta.json").exists()
        assert not (root / "bus.jsonl").exists()
    monkeypatch.chdir(tmp_path)
    safe = Comms(Path("safe-relative"), private_initial_writes=True)
    assert len(safe.initialize_private_initial_protocol()) == 32


def response_intent(message: Message, execution_id: str = "execution-1") -> PublicationIntent:
    return PublicationIntent(
        execution_id=execution_id,
        sender=message.sender,
        exact_target=message.target,
        message_type=message.type,
        notice=message.notice,
        timestamp=message.timestamp,
        payload=message.body,
        payload_digest=hashlib.sha256(message.body.encode()).hexdigest(),
        publication_key=canonical_publication_key(execution_id, message.target),
        expected_message_id=message.message_id,
    )


@pytest.mark.skipif(os.name == "posix", reason="Windows private-bus fail-closed contract")
def test_private_bus_rejects_unattestable_windows_ownership(tmp_path: Path) -> None:
    registry = ThreadRegistry(tmp_path / "registry.json")
    bus = MessageBus(tmp_path / "bus.jsonl", registry, private_initial_writes=True)
    with pytest.raises(RelationViolationError, match="POSIX ownership"):
        bus.initialize_private_protocol()


class TestAgentRuntimeInfo:
    def test_context_percent_and_persistence(self, tmp_path: Path):
        info = AgentRuntimeInfo(
            thread="a", model="provider/model", context_used=25, context_size=100
        )
        assert info.context_percent == 25
        store = RuntimeInfoStore(tmp_path / "runtime.json")
        store.set(info)
        assert store.get("a") == info

    def test_unknown_context_stays_unknown(self):
        assert AgentRuntimeInfo(thread="a", context_size=100).context_percent is None

    def test_negative_context_is_rejected(self):
        with pytest.raises(ValueError, match="negative"):
            AgentRuntimeInfo(thread="a", context_used=-1)


class TestGoalRevision:
    @pytest.mark.parametrize("revision", [True, 1.0, "1", -1, 1 << 63])
    def test_rejects_noncanonical_or_exhausted_revision(self, revision):
        with pytest.raises(ValueError, match="revision"):
            Goal(text="work", id="goal-id", revision=revision)

    def test_old_registry_goal_without_revision_reopens_at_zero(self, tmp_path: Path):
        path = tmp_path / "registry.json"
        registry = ThreadRegistry(path)
        registry.register(
            Thread(name="owner", tags=frozenset(), worktree="/wt", goal=Goal("work", "old-id"))
        )
        raw = json.loads(path.read_text())
        del raw["threads"]["owner"]["goal"]["revision"]
        path.write_text(json.dumps(raw))
        restored = ThreadRegistry(path).require("owner").goal
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


class TestThreadRegistry:
    def test_register_and_require(self, tmp_path: Path):
        registry = ThreadRegistry(tmp_path / "registry.json")
        thread = Thread(name="a", tags=frozenset(), worktree="/wt")
        registry.register(thread)
        assert registry.require("a") is not None
        assert "a" in registry

    def test_fail_closed_unknown_reference(self, tmp_path: Path):
        registry = ThreadRegistry(tmp_path / "registry.json")
        with pytest.raises(UnregisteredThreadError):
            registry.require("missing")

    def test_status_transitions(self, tmp_path: Path):
        registry = ThreadRegistry(tmp_path / "registry.json")
        registry.register(Thread(name="a", tags=frozenset(), worktree="/wt"))
        assert registry.status("a") is ThreadStatus.RUNNING
        registry.unregister("a")
        assert registry.status("a") is ThreadStatus.STOPPED
        # Unregistering a stopped (but known) thread is idempotent.
        registry.unregister("a")
        assert registry.status("a") is ThreadStatus.STOPPED
        # Only unknown references fail closed.
        with pytest.raises(UnregisteredThreadError):
            registry.unregister("ghost")

    def test_stop_then_heartbeat_cannot_reclaim_initial_owner_epoch(self, tmp_path: Path):
        registry = ThreadRegistry(tmp_path / "registry.json")
        registry.register(Thread(name="a", tags=frozenset(), worktree="/wt", pid=os.getpid()))
        expected, epoch = registry.live_owner_with_epoch("a")
        registry.unregister("a")
        registry.heartbeat("a")  # legacy lifecycle allows this; a turn CAS must not.
        assert registry.require("a") == expected
        assert registry.status("a") is ThreadStatus.RUNNING
        assert registry.live_owner_with_epoch("a")[1] > epoch
        with pytest.raises(RelationViolationError, match="stopped or changed"):
            registry.claim_live_turn(expected, "new-turn", expected_epoch=epoch)
        assert registry.require("a").active_turn is None

    def test_owner_admission_survives_session_metadata_and_rotates_on_restart(
        self, tmp_path: Path
    ) -> None:
        registry = ThreadRegistry(tmp_path / "registry.json")
        owner = Thread(name="a", tags=frozenset(), worktree="/wt", pid=1234)
        registry.register(owner)
        before = registry.snapshot().admission_generations["a"]
        registry.register(replace(owner, session_file=str(tmp_path / "session.jsonl")))
        assert registry.snapshot().admission_generations["a"] == before
        assert ThreadRegistry(registry._path).snapshot().admission_generations["a"] == before

        registry.register(replace(registry.require("a"), pid=5678))
        assert registry.snapshot().admission_generations["a"] > before

    def test_owner_admission_follows_same_owner_rename(self, tmp_path: Path) -> None:
        registry = ThreadRegistry(tmp_path / "registry.json")
        registry.register(Thread(name="a", tags=frozenset(), worktree="/wt", pid=1234))
        before = registry.snapshot().admission_generations["a"]
        registry.rename("a", "renamed-a")
        snapshot = ThreadRegistry(registry._path).snapshot()
        assert snapshot.admission_generations["renamed-a"] == before
        assert "a" not in snapshot.admission_generations

    def test_other_owner_writes_do_not_invalidate_private_epoch(self, tmp_path: Path):
        registry = ThreadRegistry(tmp_path / "registry.json")
        for name in ("a", "b"):
            registry.register(Thread(name=name, tags=frozenset(), worktree="/wt", pid=os.getpid()))
        owner, epoch = registry.live_owner_with_epoch("a")
        registry.heartbeat("b")
        other, other_epoch = registry.live_owner_with_epoch("b")
        registry.claim_live_turn(other, "other", expected_epoch=other_epoch)
        assert registry.live_owner_with_epoch("a") == (owner, epoch)
        turn = registry.claim_live_turn(owner, "mine", expected_epoch=epoch)
        assert turn.active_turn is not None and turn.active_turn.id == "mine"
        assert registry.finish_claimed_turn("a", "mine")
        assert not registry.finish_claimed_turn("a", "mine")
        assert "owner_epoch" not in owner.to_wire()

    @pytest.mark.parametrize("revocation", ["stop", "finish"])
    def test_registering_saved_turn_never_attests_a_revoked_incarnation(
        self, tmp_path: Path, revocation: str
    ) -> None:
        registry = ThreadRegistry(tmp_path / "registry.json")
        registry.register(Thread(name="a", tags=frozenset(), worktree="/wt", pid=os.getpid()))
        owner, epoch = registry.live_owner_with_epoch("a")
        claimed, claimed_epoch = registry.claim_live_turn_with_epoch(
            owner, "claimed", expected_epoch=epoch
        )
        assert registry.live_owner_with_epoch("a") == (claimed, claimed_epoch)
        if revocation == "stop":
            registry.unregister("a")
        else:
            assert registry.finish_claimed_turn("a", "claimed")
        registry.register(claimed)
        assert registry.require("a") == claimed
        with pytest.raises(RelationViolationError, match="unavailable"):
            registry.live_owner_with_epoch("a")
        assert registry.snapshot().owner_epochs["a"] > claimed_epoch
        assert "turn_epochs" not in claimed.to_wire()

    def test_comms_begin_turn_cannot_revive_stopped_owner(self, tmp_path: Path) -> None:
        comms = Comms(tmp_path / "wire")
        comms.register(Thread(name="a", tags=frozenset(), worktree="/wt", pid=os.getpid()))
        comms.registry.unregister("a")
        with pytest.raises(RelationViolationError, match="stopped or unavailable"):
            comms.begin_turn("a", "revived")
        assert comms.registry.status("a") is ThreadStatus.STOPPED
        assert comms.registry.require("a").active_turn is None

    def test_begin_turn_migrates_unmarked_legacy_registry_without_reviving_stop(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "legacy-wire"
        comms = Comms(root)
        comms.register(Thread(name="a", tags=frozenset(), worktree="/wt", pid=os.getpid()))
        registry_path = root / "registry.json"
        data = json.loads(registry_path.read_text())
        for field in ("owner_epochs", "owner_epoch_counter", "turn_epochs"):
            data.pop(field)
        registry_path.write_text(json.dumps(data))
        comms.begin_turn("a", "migrated-turn")
        assert comms.registry.require("a").active_turn is not None
        assert comms.registry.live_owner_with_epoch("a")[1] > 0
        comms.finish_turn("a", "migrated-turn")

    @pytest.mark.skipif(os.name == "nt", reason="private POSIX ownership unavailable on Windows")
    def test_private_marker_can_precede_first_registry_snapshot(self, tmp_path: Path) -> None:
        root = tmp_path / "private-fresh"
        root.mkdir(mode=0o700)
        comms = Comms(root, private_initial_writes=True)
        comms.initialize_private_initial_protocol()
        reopened = Comms(root)
        reopened.register(Thread(name="a", tags=frozenset(), worktree="/wt", pid=os.getpid()))
        assert reopened.registry.live_owner_with_epoch("a")[1] > 0

    @pytest.mark.skipif(os.name == "nt", reason="private POSIX ownership unavailable on Windows")
    def test_private_marker_does_not_bootstrap_stripped_owner_epoch(self, tmp_path: Path) -> None:
        root = tmp_path / "private-wire"
        root.mkdir(mode=0o700)
        comms = Comms(root, private_initial_writes=True)
        comms.register(Thread(name="a", tags=frozenset(), worktree="/wt", pid=os.getpid()))
        comms.initialize_private_initial_protocol()
        registry_path = root / "registry.json"
        data = json.loads(registry_path.read_text())
        for field in ("owner_epochs", "owner_epoch_counter", "turn_epochs"):
            data.pop(field)
        registry_path.write_text(json.dumps(data))
        with pytest.raises(RelationViolationError, match="guard does not match"):
            comms.begin_turn("a", "must-not-claim")
        with pytest.raises(RelationViolationError, match="guard does not match"):
            comms.registry.register(Thread(name="a", tags=frozenset(), worktree="/wt"))
        assert json.loads(registry_path.read_text())["threads"]["a"].get("active_turn") is None

    @pytest.mark.skipif(sys.platform != "linux", reason="fault injection uses Linux /proc/self/fd")
    @pytest.mark.parametrize("fail_at", ["pending", "replacement", "directory", "commit"])
    def test_private_guard_faults_never_promote_an_unfsynced_owner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_at: str
    ) -> None:
        import agent_comms.declarations as declarations

        root = tmp_path / "isolated-private-root"
        comms = Comms(root, private_initial_writes=True)
        comms.register(Thread(name="a", tags=frozenset(), worktree="/wt", pid=os.getpid()))
        root_id = comms.initialize_private_initial_protocol()
        assert len(root_id) == 32
        cold = ThreadRegistry(root / "registry.json")
        assert cold.require("a").name == "a"  # cache the old revision
        original_fsync = os.fsync
        original_write = declarations._atomic_write_text
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
        monkeypatch.setattr(declarations, "_atomic_write_text", write)
        with pytest.raises((OSError, RelationViolationError), match="injected|guard"):
            comms.registry.register(
                Thread(name="b", tags=frozenset(), worktree="/wt", pid=os.getpid())
            )
        monkeypatch.undo()
        # Failed COMMITTED fsync follows successful directory fsync. Complete
        # visible commit bytes are safe; on restart they may also be absent.
        if fail_at == "commit":
            assert ThreadRegistry(root / "registry.json").require("b").name == "b"
        else:
            if fail_at in {"pending", "replacement"}:
                assert "b" not in json.loads((root / "registry.json").read_text())["threads"]
            with pytest.raises(RelationViolationError, match="Private registry guard"):
                cold.require("a")
            with pytest.raises(RelationViolationError, match="Private registry guard"):
                ThreadRegistry(root / "registry.json")
            child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from agent_comms import ThreadRegistry; "
                    "from pathlib import Path; import sys; "
                    "ThreadRegistry(Path(sys.argv[1])).require('a')",
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
        comms.register(Thread(name="sender", tags=frozenset(), worktree="/wt"))
        original_fsync = os.fsync
        directory_calls = 0

        def fsync(fd: int) -> None:
            nonlocal directory_calls
            if os.readlink(f"/proc/self/fd/{fd}") == str(root):
                directory_calls += 1
                if directory_calls == 3:  # after durable guard pending, at marker replace
                    raise OSError("injected marker directory fsync failure")
            original_fsync(fd)

        monkeypatch.setattr(os, "fsync", fsync)
        with pytest.raises(OSError, match="injected marker"):
            comms.initialize_private_initial_protocol()
        monkeypatch.undo()
        assert (root / ".registry-owner-guard").exists()
        assert (root / "bus_meta.json").exists()
        with pytest.raises(RelationViolationError, match="guard is pending"):
            ThreadRegistry(root / "registry.json")
        with pytest.raises(RelationViolationError, match="fresh bus root"):
            comms.initialize_private_initial_protocol()  # never auto-repair

    @pytest.mark.skipif(sys.platform != "linux", reason="fault injection uses Linux /proc/self/fd")
    @pytest.mark.parametrize("stage", ["pending", "commit"])
    def test_torn_guard_slot_never_falls_back_to_older_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
    ) -> None:
        root = tmp_path / "private"
        comms = Comms(root, private_initial_writes=True)
        comms.register(Thread(name="a", tags=frozenset(), worktree="/wt"))
        comms.initialize_private_initial_protocol()
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
            ThreadRegistry(root / "registry.json")

    @pytest.mark.skipif(os.name == "nt", reason="private POSIX ownership unavailable on Windows")
    @pytest.mark.parametrize("damage", ["missing", "corrupt", "truncated", "strip_epochs"])
    def test_private_guard_rejects_disk_downgrade_even_with_cached_revision(
        self, tmp_path: Path, damage: str
    ) -> None:
        root = tmp_path / "guarded"
        comms = Comms(root, private_initial_writes=True)
        comms.register(Thread(name="a", tags=frozenset(), worktree="/wt", pid=os.getpid()))
        comms.initialize_private_initial_protocol()
        cold = ThreadRegistry(root / "registry.json")
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
            data.pop("owner_epochs")
            data.pop("owner_epoch_counter")
            path.write_text(json.dumps(data))  # simulated old writer ignores marker
        with pytest.raises(RelationViolationError, match="Private registry guard"):
            cold.require("a")
        with pytest.raises(RelationViolationError, match="Private registry guard"):
            comms.registry.register(Thread(name="b", tags=frozenset(), worktree="/wt"))
        assert "registry_guard" not in comms.registry._threads["a"].to_wire()

    def test_finish_claimed_turn_resolves_retained_alias_only_for_exact_turn(self, tmp_path: Path):
        registry = ThreadRegistry(tmp_path / "registry.json")
        registry.register(Thread(name="a", tags=frozenset(), worktree="/wt", pid=os.getpid()))
        registry.rename("a", "b")
        owner, epoch = registry.live_owner_with_epoch("a")
        assert owner.name == "b"
        registry.claim_live_turn(owner, "claimed", expected_epoch=epoch)
        registry.rename("b", "c")
        assert not registry.finish_claimed_turn("a", "other")
        assert registry.require("c").active_turn is not None
        assert registry.finish_claimed_turn("a", "claimed")
        assert registry.require("c").active_turn is None

    def test_missing_or_malformed_private_epoch_metadata_refuses_turn(self, tmp_path: Path):
        path = tmp_path / "registry.json"
        registry = ThreadRegistry(path)
        registry.register(Thread(name="a", tags=frozenset(), worktree="/wt", pid=os.getpid()))
        owner, epoch = registry.live_owner_with_epoch("a")
        data = json.loads(path.read_text())
        data.pop("owner_epochs")
        data.pop("owner_epoch_counter")
        path.write_text(json.dumps(data))
        with pytest.raises(RelationViolationError, match="stopped or changed"):
            registry.claim_live_turn(owner, "claimed", expected_epoch=epoch)
        with pytest.raises(RelationViolationError, match="unavailable"):
            registry.live_owner_with_epoch("a")
        data["owner_epochs"] = {"a": True}
        data["owner_epoch_counter"] = epoch
        path.write_text(json.dumps(data))
        with pytest.raises(RelationViolationError, match="invalid private registry owner epochs"):
            registry.live_owner_with_epoch("a")

    def test_deleting_thread_cannot_be_revived(self, tmp_path: Path):
        registry = ThreadRegistry(tmp_path / "registry.json")
        thread = Thread(name="a", tags=frozenset(), worktree="/wt")
        registry.register(thread)
        registry.unregister("a")
        registry.begin_delete("a")
        assert registry.status("a") is ThreadStatus.DELETING
        with pytest.raises(RelationViolationError, match="permanently deleted"):
            registry.heartbeat("a")
        with pytest.raises(RelationViolationError, match="permanently deleted"):
            registry.register(thread)

    def test_active_threads_excludes_stopped(self, tmp_path: Path):
        registry = ThreadRegistry(tmp_path / "registry.json")
        registry.register(Thread(name="a", tags=frozenset(), worktree="/wt"))
        registry.register(Thread(name="b", tags=frozenset(), worktree="/wt"))
        registry.unregister("a")
        assert set(registry.active_threads()) == {"b"}
        assert set(registry.all_threads()) == {"a", "b"}

    def test_remove_drops_declaration_entirely(self, tmp_path: Path):
        registry = ThreadRegistry(tmp_path / "registry.json")
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
        registry = ThreadRegistry(path)
        registry.register(
            Thread(
                name="a",
                tags=frozenset({"x", "y"}),
                worktree="/wt",
                parent="p",
                task="t",
                pid=9,
            )
        )
        registry.unregister("a")
        reloaded = ThreadRegistry(path)
        thread = reloaded.require("a")
        assert thread.parent == "p" and thread.task == "t" and thread.pid == 9
        assert thread.tags == frozenset({"x", "y"})
        assert reloaded.status("a") is ThreadStatus.STOPPED

    def test_peers_excludes_self(self, tmp_path: Path):
        registry = ThreadRegistry(tmp_path / "registry.json")
        for name in ("a", "b", "c"):
            registry.register(Thread(name=name, tags=frozenset(), worktree="/wt"))
        assert set(registry.peers("b")) == {"a", "c"}


class TestMessageBus:
    def _bus(self, tmp_path: Path) -> MessageBus:
        registry = ThreadRegistry(tmp_path / "registry.json")
        for name in ("a", "b"):
            registry.register(Thread(name=name, tags=frozenset(), worktree="/wt"))
        return MessageBus(tmp_path / "bus.jsonl", registry)

    def test_send_returns_id(self, tmp_path: Path):
        bus = self._bus(tmp_path)
        mid = bus.send(Message(sender="a", target="b", body="x", type=MessageType.INFO))
        assert isinstance(mid, str) and len(mid) == 12

    def test_fail_closed_unregistered_sender(self, tmp_path: Path):
        bus = self._bus(tmp_path)
        with pytest.raises(UnregisteredThreadError, match="Sender"):
            bus.send(Message(sender="ghost", target="b", body="x", type=MessageType.INFO))

    def test_fail_closed_unregistered_target(self, tmp_path: Path):
        bus = self._bus(tmp_path)
        with pytest.raises(UnregisteredThreadError, match="Target"):
            bus.send(Message(sender="a", target="ghost", body="x", type=MessageType.INFO))

    def test_broadcast_target_accepted(self, tmp_path: Path):
        bus = self._bus(tmp_path)
        bus.send(Message(sender="a", target="broadcast", body="x", type=MessageType.INFO))
        assert bus.total_messages() == 1

    def test_inbox_excludes_self_sent(self, tmp_path: Path):
        bus = self._bus(tmp_path)
        bus.send(Message(sender="a", target="b", body="x", type=MessageType.INFO))
        assert bus.inbox("a") == []
        assert len(bus.inbox("b")) == 1

    def test_seq_cursor_not_hash_order(self, tmp_path: Path):
        """Regression: read cursor must be monotonic seq, never hash order."""
        bus = self._bus(tmp_path)
        bus.send(Message(sender="a", target="b", body="first", type=MessageType.INFO))
        bus.mark_delivered("b")
        # A later message whose hash happens to sort before the first id
        # must still be delivered.
        bus.send(Message(sender="a", target="b", body="second", type=MessageType.INFO))
        inbox = bus.inbox("b")
        assert len(inbox) == 1 and inbox[0].body == "second"

    def test_seq_monotonic_across_many_sends(self, tmp_path: Path):
        bus = self._bus(tmp_path)
        for i in range(10):
            bus.send(Message(sender="a", target="b", body=f"m{i}", type=MessageType.INFO))
        seqs = [m.seq for m in bus.inbox("b")]
        assert seqs == list(range(1, 11))

    @pytest.mark.skipif(sys.platform != "linux", reason="fsync fault uses Linux /proc/self/fd")
    @pytest.mark.parametrize("private", [False, True])
    def test_sequence_reservation_parent_fsync_precedes_append(
        self, monkeypatch: pytest.MonkeyPatch, private: bool
    ) -> None:
        """A crash cut at the metadata directory sync cannot leave a new row."""
        with TemporaryDirectory(prefix="ac-bus-seq-", dir="/var/tmp") as dirname:
            root = Path(dirname)
            bus = self._bus(root)
            for body in ("first", "second"):
                bus.send(Message(sender="a", target="b", body=body, type=MessageType.INFO))
            if private:
                sequence_path = root / "bus_meta.json"
                marker = json.loads(sequence_path.read_text())
                marker.update(writer_protocol_version=1, wire_root_id="a" * 32)
                sequence_path.write_text(json.dumps(marker))
                sequence_path.chmod(0o600)
                bus._path.chmod(0o600)
                _test_only_guard_for_handcrafted_marker(bus._registry)
                bus = MessageBus(bus._path, bus._registry, private_response_writes=True)

            original_fsync = os.fsync

            def fail_metadata_directory_sync(fd: int) -> None:
                if os.readlink(f"/proc/self/fd/{fd}") == str(root):
                    raise OSError("injected metadata parent fsync failure")
                original_fsync(fd)

            with monkeypatch.context() as patch:
                patch.setattr(os, "fsync", fail_metadata_directory_sync)
                with pytest.raises(OSError, match="metadata parent fsync failure"):
                    if private:
                        intended = Message(
                            sender="a", target="b", body="interrupted", type=MessageType.INFO
                        )
                        bus.publish_keyed_response(response_intent(intended))
                    else:
                        bus.send(
                            Message(
                                sender="a", target="b", body="interrupted", type=MessageType.INFO
                            )
                        )

            reopened = MessageBus(bus._path, bus._registry, private_response_writes=private)
            assert [message.seq for message in reopened.full_history()] == [1, 2]
            if private:
                next_message = Message(
                    sender="a", target="b", body="after restart", type=MessageType.INFO
                )
                reopened.publish_keyed_response(response_intent(next_message))
            else:
                reopened.send(
                    Message(sender="a", target="b", body="after restart", type=MessageType.INFO)
                )
            sequences = [message.seq for message in reopened.full_history()]
            assert sequences == [1, 2, 4]

    @pytest.mark.skipif(os.name != "posix", reason="real /var/tmp durability fixture")
    @pytest.mark.parametrize("metadata_state", ["stale", "missing"])
    @pytest.mark.parametrize("unterminated", [False, True])
    def test_reopen_never_reuses_sequence_after_metadata_rollback(
        self, metadata_state: str, unterminated: bool
    ) -> None:
        """A retained bus row still owns its sequence if metadata rolled back."""
        with TemporaryDirectory(prefix="ac-bus-seq-", dir="/var/tmp") as dirname:
            root = Path(dirname)
            bus = self._bus(root)
            for body in ("first", "second"):
                bus.send(Message(sender="a", target="b", body=body, type=MessageType.INFO))
            sequence_path = root / "bus_meta.json"
            old_metadata = sequence_path.read_bytes()
            bus.send(Message(sender="a", target="b", body="third", type=MessageType.INFO))
            if unterminated:
                bus._path.write_bytes(bus._path.read_bytes()[:-1])

            # Deterministic crash cut: the last fsynced row survives while an
            # older metadata directory entry is recovered on reboot.
            if metadata_state == "stale":
                stale = root / "bus_meta.stale"
                stale.write_bytes(old_metadata)
                os.replace(stale, sequence_path)
            else:
                sequence_path.unlink()

            reopened = MessageBus(bus._path, bus._registry)
            reopened.send(Message(sender="a", target="b", body="new", type=MessageType.INFO))
            assert [message.seq for message in reopened.full_history()] == [1, 2, 3, 4]

    def test_ack_only_clears_up_to_latest(self, tmp_path: Path):
        bus = self._bus(tmp_path)
        bus.send(Message(sender="a", target="b", body="1", type=MessageType.INFO))
        bus.mark_delivered("b")
        bus.send(Message(sender="a", target="b", body="2", type=MessageType.INFO))
        assert [m.body for m in bus.inbox("b")] == ["2"]

    def test_fail_closed_inbox_unknown_thread(self, tmp_path: Path):
        bus = self._bus(tmp_path)
        with pytest.raises(UnregisteredThreadError):
            bus.inbox("ghost")

    def test_persistence_roundtrip(self, tmp_path: Path):
        path = tmp_path / "bus.jsonl"
        registry = ThreadRegistry(tmp_path / "registry.json")
        for name in ("a", "b"):
            registry.register(Thread(name=name, tags=frozenset(), worktree="/wt"))
        bus = MessageBus(path, registry)
        mid = bus.send(Message(sender="a", target="b", body="x", type=MessageType.ALERT))
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        assert lines[0]["type"] == "alert" and lines[0]["id"] == mid
        # The bus assigns the sequence number at send time.
        assert lines[0]["seq"] == 1

    def test_channel_history_pages_use_exclusive_sequence_cursors(self, tmp_path: Path):
        bus = self._bus(tmp_path)
        for index in range(8):
            target = "#all" if index != 4 else "b"
            bus.send(
                Message(
                    sender="a",
                    target=target,
                    body=f"m{index + 1}",
                    type=MessageType.INFO,
                )
            )

        latest = bus.channel_history_page("#all", limit=3)
        assert [message.seq for message in latest.messages] == [6, 7, 8]
        assert latest.has_older
        assert not latest.has_newer
        assert latest.oldest_seq == 6
        assert latest.newest_seq == 8

        older = bus.channel_history_page("#all", before=6, limit=3)
        assert [message.seq for message in older.messages] == [2, 3, 4]
        assert older.has_older
        assert older.has_newer

        newer = bus.channel_history_page("#all", after=4, limit=2)
        assert [message.seq for message in newer.messages] == [6, 7]
        assert newer.has_older
        assert newer.has_newer

    @pytest.mark.parametrize("route", ["channel", "dm", "incoming"])
    @pytest.mark.parametrize("middle_bytes", [180_000, 300_000])
    def test_forward_page_never_skips_large_message_before_smaller_message(
        self, tmp_path: Path, route: str, middle_bytes: int
    ):
        bus = self._bus(tmp_path)
        target = "b" if route == "dm" else "#all"
        for body in ("a" * 120_000, "b" * middle_bytes, "small"):
            bus.send(Message(sender="a", target=target, body=body, type=MessageType.INFO))

        def page(after):
            if route == "channel":
                return bus.channel_history_page("#all", after=after)
            if route == "dm":
                return bus.dm_history_page("a", "b", after=after)
            return bus.incoming_page("b", after=after)

        first = page(0)
        # A later small row must not advance the cursor past the omitted row.
        assert [message.seq for message in first.messages] == [1]
        assert first.has_newer
        delivered = list(first.messages)
        current = first
        while current.has_newer:
            current = page(current.newest_seq)
            assert current.messages
            delivered.extend(current.messages)
        assert [message.seq for message in delivered] == [1, 2, 3]
        assert not page(delivered[-1].seq).messages

    def test_private_sideband_does_not_change_public_history_page_budget(self, tmp_path: Path):
        bus = self._bus(tmp_path)
        for body in ("first", "second", "third"):
            bus.send(Message(sender="a", target="#all", body=body, type=MessageType.INFO))

        lines = bus._path.read_bytes().splitlines(keepends=True)
        public_budget = len(lines[0]) + len(lines[1])
        before = bus.channel_history_page("#all", after=0, max_bytes=public_budget)
        assert [message.body for message in before.messages] == ["first", "second"]
        assert before.has_newer

        private = json.loads(lines[0])
        private[PRIVATE_WIRE_FIELD] = {"receipt": "SECRET" * 1024}
        lines[0] = (json.dumps(private) + "\n").encode()
        bus._path.write_bytes(b"".join(lines))

        after = bus.channel_history_page("#all", after=0, max_bytes=public_budget)
        assert after == before
        with bus._record_snapshot() as (_, records):
            first, charged_bytes = next(records)
        assert first.body == "first"
        assert charged_bytes == len(json.dumps(first.to_wire()).encode()) + 1
        assert charged_bytes < len(lines[0])

    @pytest.mark.skipif(os.name == "nt", reason="private POSIX ownership unavailable on Windows")
    def test_keyed_response_receipt_is_same_row_and_reopens_without_reappend(self, tmp_path: Path):
        legacy = self._bus(tmp_path)
        legacy.send(Message(sender="a", target="#all", body="legacy", type=MessageType.INFO))
        legacy._path.chmod(0o600)
        sequence_path = legacy._path.parent / "bus_meta.json"
        metadata = json.loads(sequence_path.read_text())
        metadata.update(writer_protocol_version=1, wire_root_id="a" * 32)
        sequence_path.write_text(json.dumps(metadata))
        _test_only_guard_for_handcrafted_marker(legacy._registry)
        keyed = MessageBus(legacy._path, legacy._registry, private_response_writes=True)
        intended = Message(
            sender="a", target="#all", body="response", type=MessageType.INFO, timestamp=42.5
        )
        key = canonical_publication_key("execution-1", "#all")

        stored = keyed.publish_keyed_response(response_intent(intended))
        rows = [json.loads(line) for line in legacy._path.read_text().splitlines()]
        public = stored.to_wire()
        assert rows[1] == {
            **public,
            PRIVATE_WIRE_FIELD: {
                "version": 1,
                "response": {
                    "wire_root_id": "a" * 32,
                    "execution_id": "execution-1",
                    "publication_key": key,
                    "envelope_digest": public_envelope_digest(public),
                },
            },
        }
        assert PRIVATE_WIRE_FIELD not in public
        original_bus = legacy._path.read_bytes()
        original_meta = sequence_path.read_bytes()
        reopened = MessageBus(legacy._path, legacy._registry, private_response_writes=True)
        assert reopened.publish_keyed_response(response_intent(intended)) == stored
        assert legacy._path.read_bytes() == original_bus
        assert sequence_path.read_bytes() == original_meta
        with pytest.raises(RelationViolationError, match="intent conflicts"):
            reopened.publish_keyed_response(
                response_intent(
                    Message(
                        sender="a",
                        target="#all",
                        body="different",
                        type=MessageType.INFO,
                        timestamp=42.5,
                    )
                )
            )
        with pytest.raises(RelationViolationError, match="Legacy append"):
            legacy.send(
                Message(sender="a", target="#all", body="old writer", type=MessageType.INFO)
            )
        with pytest.raises(RelationViolationError, match="Private bus protocol"):
            legacy.remove_thread("b")
        assert legacy._path.read_bytes() == original_bus
        legacy._registry.rename("a", "renamed")
        renamed_reopen = MessageBus(legacy._path, legacy._registry, private_response_writes=True)
        assert renamed_reopen.publish_keyed_response(response_intent(intended)) == stored
        assert legacy._path.read_bytes() == original_bus
        assert sequence_path.read_bytes() == original_meta

    @pytest.mark.skipif(os.name == "nt", reason="private POSIX ownership unavailable on Windows")
    @pytest.mark.parametrize("bad_seq", [-1, True, "1", 1 << 63])
    def test_private_marker_rejects_invalid_sequence_type_or_range_without_append(
        self, tmp_path: Path, bad_seq: object
    ):
        legacy = self._bus(tmp_path)
        sequence_path = tmp_path / "bus_meta.json"
        sequence_path.write_text(
            json.dumps(
                {"last_seq": bad_seq, "writer_protocol_version": 1, "wire_root_id": "a" * 32}
            )
        )
        sequence_path.chmod(0o600)
        before = sequence_path.read_bytes()
        keyed = MessageBus(legacy._path, legacy._registry, private_response_writes=True)
        message = Message(sender="a", target="#all", body="reply", type=MessageType.INFO)
        with pytest.raises(RelationViolationError, match="protocol marker"):
            keyed.publish_keyed_response(response_intent(message))
        assert not legacy._path.exists()
        assert sequence_path.read_bytes() == before

    @pytest.mark.skipif(os.name == "nt", reason="private POSIX ownership unavailable on Windows")
    def test_private_marker_duplicate_keys_and_exhausted_sequence_fail_closed(self, tmp_path: Path):
        legacy = self._bus(tmp_path)
        sequence_path = tmp_path / "bus_meta.json"
        sequence_path.write_text(
            '{"last_seq":0,"last_seq":1,"writer_protocol_version":1,'
            '"wire_root_id":"' + "a" * 32 + '"}'
        )
        sequence_path.chmod(0o600)
        keyed = MessageBus(legacy._path, legacy._registry, private_response_writes=True)
        message = Message(sender="a", target="#all", body="reply", type=MessageType.INFO)
        before = sequence_path.read_bytes()
        with pytest.raises(RelationViolationError, match="protocol marker"):
            keyed.publish_keyed_response(response_intent(message))
        assert sequence_path.read_bytes() == before
        sequence_path.write_text(
            json.dumps(
                {"last_seq": (1 << 63) - 1, "writer_protocol_version": 1, "wire_root_id": "a" * 32}
            )
        )
        _test_only_guard_for_handcrafted_marker(legacy._registry)
        before = sequence_path.read_bytes()
        with pytest.raises(RelationViolationError, match="sequence is exhausted"):
            keyed.publish_keyed_response(response_intent(message))
        assert not legacy._path.exists()
        assert sequence_path.read_bytes() == before

    @pytest.mark.skipif(os.name == "nt", reason="private POSIX ownership unavailable on Windows")
    def test_keyed_response_replay_survives_direct_target_rename(self, tmp_path: Path):
        legacy = self._bus(tmp_path)
        sequence_path = tmp_path / "bus_meta.json"
        sequence_path.write_text(
            json.dumps({"last_seq": 0, "writer_protocol_version": 1, "wire_root_id": "a" * 32})
        )
        sequence_path.chmod(0o600)
        _test_only_guard_for_handcrafted_marker(legacy._registry)
        intended = Message(sender="a", target="b", body="response", type=MessageType.INFO)
        keyed = MessageBus(legacy._path, legacy._registry, private_response_writes=True)
        stored = keyed.publish_keyed_response(response_intent(intended))
        before = legacy._path.read_bytes(), sequence_path.read_bytes()
        legacy._registry.rename("b", "renamed")
        reopened = MessageBus(legacy._path, legacy._registry, private_response_writes=True)
        assert reopened.publish_keyed_response(response_intent(intended)) == stored
        assert (legacy._path.read_bytes(), sequence_path.read_bytes()) == before

    @pytest.mark.skipif(os.name == "nt", reason="private POSIX ownership unavailable on Windows")
    def test_keyed_writer_is_disabled_and_rejects_unsafe_or_corrupt_roots(self, tmp_path: Path):
        legacy = self._bus(tmp_path)
        intended = Message(sender="a", target="#all", body="response", type=MessageType.INFO)
        intent = response_intent(intended)
        with pytest.raises(RelationViolationError, match="disabled"):
            legacy.publish_keyed_response(intent)
        keyed = MessageBus(legacy._path, legacy._registry, private_response_writes=True)
        with pytest.raises(TypeError, match="validated PublicationIntent"):
            keyed.publish_keyed_response(intended)  # type: ignore[arg-type]
        with pytest.raises(RelationViolationError, match="protocol marker"):
            keyed.publish_keyed_response(intent)
        metadata = {"last_seq": 0, "writer_protocol_version": 1, "wire_root_id": "a" * 32}
        (tmp_path / "bus_meta.json").write_text(json.dumps(metadata))
        (tmp_path / "bus_meta.json").chmod(0o600)
        _test_only_guard_for_handcrafted_marker(legacy._registry)
        keyed.publish_keyed_response(intent)
        original = legacy._path.read_bytes()
        legacy._path.chmod(0o644)
        with pytest.raises(RelationViolationError, match="owner-only"):
            keyed.publish_keyed_response(intent)
        assert legacy._path.read_bytes() == original
        legacy._path.chmod(0o600)
        legacy._path.write_bytes(original[:-1])
        with pytest.raises(RelationViolationError, match="Incomplete bus row"):
            keyed.publish_keyed_response(intent)
        duplicated = original.replace(
            b'"text": "response"', b'"text": "response", "text": "response"'
        )
        assert duplicated != original
        legacy._path.write_bytes(duplicated)
        with pytest.raises(ValueError, match="Duplicate bus object key"):
            keyed.publish_keyed_response(intent)
        legacy._path.write_bytes(original)
        record = json.loads(legacy._path.read_text())
        record[PRIVATE_WIRE_FIELD]["response"]["envelope_digest"] = "0" * 64
        legacy._path.write_text(json.dumps(record) + "\n")
        with pytest.raises(RelationViolationError, match="malformed private bus receipt"):
            keyed.publish_keyed_response(intent)

    @pytest.mark.skipif(os.name == "nt", reason="private POSIX ownership unavailable on Windows")
    @pytest.mark.parametrize(
        "broken", ["missing", "duplicate", "out_of_order", "notice_bool", "mention_bool"]
    )
    def test_keyed_append_rejects_invalid_complete_legacy_rows_without_mutation(
        self, tmp_path: Path, broken: str
    ):
        legacy = self._bus(tmp_path)
        legacy.send(
            Message(sender="a", target="#all", body="@b first", type=MessageType.INFO, notice=True)
        )
        legacy.send(Message(sender="a", target="#all", body="second", type=MessageType.INFO))
        legacy._path.chmod(0o600)
        rows = [json.loads(line) for line in legacy._path.read_text().splitlines()]
        if broken == "missing":
            rows[0] = {"seq": 7}
        elif broken == "notice_bool":
            rows[0]["notice"] = 1  # Python equality treats 1 == True, JSON does not.
        elif broken == "mention_bool":
            rows[0]["mentions"][0]["start"] = False  # False == 0, raw JSON differs.
        else:
            rows[1]["seq"] = 1 if broken == "duplicate" else 0
        legacy._path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        sequence_path = tmp_path / "bus_meta.json"
        metadata = json.loads(sequence_path.read_text())
        metadata.update(writer_protocol_version=1, wire_root_id="a" * 32)
        sequence_path.write_text(json.dumps(metadata))
        before_log, before_meta = legacy._path.read_bytes(), sequence_path.read_bytes()
        keyed = MessageBus(legacy._path, legacy._registry, private_response_writes=True)
        intended = Message(sender="a", target="#all", body="response", type=MessageType.INFO)
        with pytest.raises(RelationViolationError, match="Malformed public bus row"):
            keyed.publish_keyed_response(response_intent(intended))
        assert (legacy._path.read_bytes(), sequence_path.read_bytes()) == (before_log, before_meta)

    @pytest.mark.skipif(os.name == "nt", reason="private POSIX ownership unavailable on Windows")
    def test_keyed_append_checks_actual_custom_basename_repair_file(self, tmp_path: Path):
        legacy = self._bus(tmp_path)
        custom = tmp_path / "custom"
        custom.mkdir()
        path = custom / "events.jsonl"
        meta = custom / "bus_meta.json"
        meta.write_text(
            json.dumps({"last_seq": 0, "writer_protocol_version": 1, "wire_root_id": "a" * 32})
        )
        meta.chmod(0o600)
        repair = custom / "events.jsonl.corrupt"
        repair.write_text("old contents")
        repair.chmod(0o644)
        keyed = MessageBus(path, legacy._registry, private_response_writes=True)
        intended = Message(sender="a", target="#all", body="response", type=MessageType.INFO)
        with pytest.raises(RelationViolationError, match="owner-only"):
            keyed.publish_keyed_response(response_intent(intended))
        assert not path.exists()
        assert repair.read_text() == "old contents"

    def test_response_intent_rejects_negative_time_and_oversized_payload(self):
        intended = Message(sender="a", target="#all", body="response", type=MessageType.INFO)
        with pytest.raises(ValueError, match="non-negative"):
            response_intent(replace(intended, timestamp=-1))
        with pytest.raises(ValueError, match="byte limit"):
            response_intent(replace(intended, body="x" * (MAX_PUBLICATION_PAYLOAD_BYTES + 1)))

    def test_direct_legacy_remove_cannot_erase_unrelated_private_sideband(self, tmp_path: Path):
        bus = self._bus(tmp_path)
        bus.send(Message(sender="a", target="b", body="purge", type=MessageType.INFO))
        bus.send(Message(sender="a", target="#all", body="retain", type=MessageType.INFO))
        lines = bus._path.read_bytes().splitlines(keepends=True)
        retained = json.loads(lines[1])
        retained[PRIVATE_WIRE_FIELD] = {"publication_key": "SECRET"}
        lines[1] = (json.dumps(retained) + "\n").encode()
        bus._path.write_bytes(b"".join(lines))
        before_bus = bus._path.read_bytes()
        sequence_path = bus._path.parent / "bus_meta.json"
        before_meta = sequence_path.read_bytes()

        with pytest.raises(RelationViolationError, match="Private bus authority"):
            bus.remove_thread("b")

        assert bus._path.read_bytes() == before_bus
        assert sequence_path.read_bytes() == before_meta
        assert [message.body for message in bus.full_history()] == ["purge", "retain"]

    def test_dm_history_page_handles_aliases_and_byte_budget(self, tmp_path: Path):
        bus = self._bus(tmp_path)
        bus.send(Message(sender="a", target="b", body="x" * 1000, type=MessageType.INFO))
        bus.send(Message(sender="b", target="a", body="small", type=MessageType.INFO))
        bus._registry.rename("a", "renamed")

        first = bus.dm_history_page("renamed", "b", after=0, max_bytes=10)
        assert [message.seq for message in first.messages] == [1]
        assert first.has_newer

        second = bus.dm_history_page("renamed", "b", after=1, max_bytes=10)
        assert [message.seq for message in second.messages] == [2]
        assert second.has_older

    def test_paged_history_and_send_do_not_materialize_full_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        bus = self._bus(tmp_path)
        for index in range(5):
            bus.send(Message(sender="a", target="#all", body=str(index), type=MessageType.INFO))

        monkeypatch.setattr(
            bus,
            "_load_log_unlocked",
            lambda: pytest.fail("paged paths must stream the log"),
        )
        assert bus.channel_history_page("#all", limit=2).newest_seq == 5
        bus.send(Message(sender="a", target="#all", body="next", type=MessageType.INFO))
        assert bus.latest_sequence() == 6

    def test_unread_count_and_acknowledge_stream_the_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        bus = self._bus(tmp_path)
        for index in range(5):
            bus.send(Message(sender="a", target="b", body=str(index), type=MessageType.INFO))
        monkeypatch.setattr(
            bus,
            "_load_log_unlocked",
            lambda: pytest.fail("unread operations must stream the log"),
        )

        assert bus.pending_count("b") == 5
        assert bus.mark_delivered("b") == 5
        assert bus.pending_count("b") == 0

    def test_history_page_validates_bounds(self, tmp_path: Path):
        bus = self._bus(tmp_path)
        with pytest.raises(ValueError, match="either before or after"):
            bus.channel_history_page("#all", before=2, after=1)
        with pytest.raises(ValueError, match="positive"):
            bus.channel_history_page("#all", limit=0)


class TestSharedLedger:
    def test_merge_and_read(self, tmp_path: Path):
        ledger = SharedLedger(tmp_path / "ledger.json")
        ledger.merge({"a": 1}, author="x")
        assert ledger.read()["a"] == 1

    def test_rejects_non_string_keys(self, tmp_path: Path):
        ledger = SharedLedger(tmp_path / "ledger.json")
        with pytest.raises(ValueError, match="string"):
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
