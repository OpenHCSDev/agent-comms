

from agent_comms.repo import Checkout
from bus_record_fixture import prepare_native_comms

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.session_file import RegistrySessionLocation
from agent_comms.agent_events import AgentInfo, ContextUsageObservation
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.context_reading import ContextUsage, UnmeasuredContext
from agent_comms.errors import RelationViolationError, UnregisteredThreadError
from agent_comms.field_codec import FieldCodec, TurnId, GoalId
from agent_comms.goals import Goal
from agent_comms.launch_runtime import CallingThreadUnavailable
from agent_comms.messages import HandoffKind, InfoKind, Message
from agent_comms.native_input_owner import RegistryOwner
from agent_comms.pi_counts import PiCount
from agent_comms.registration import Registration
from agent_comms.private_registry_guard import PrivateRegistryGuard
from agent_comms.registry_document import RegistryDocument
from agent_comms.runtime_info import AgentRuntimeInfo, RuntimeInfoStore
from agent_comms.shared_ledger import SharedLedger
from agent_comms.thread_status import DeletingThreadStatus, HeartbeatThreadStatus, StoppedThreadStatus
from agent_comms.threads import current_thread
from agent_comms.thread_text import ForkedParent, GivenThreadText
from agent_comms.thread_model import DeclaredModel
from agent_comms.threads import Thread


@pytest.mark.skipif(os.name == "nt", reason="private POSIX ownership unavailable on Windows")
def test_native_root_checks_relative_and_absolute_ancestor_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unsafe = tmp_path / "nonsticky-writable"
    unsafe.mkdir()
    unsafe.chmod(0o777)
    monkeypatch.chdir(unsafe)
    for root in (Path("relative-wire"), unsafe / "absolute-wire"):
        candidate = wire(root)
        with pytest.raises(RelationViolationError):
            prepare_native_comms(candidate)
        assert not candidate.registry.store.guard_path.exists()
    monkeypatch.chdir(tmp_path)
    safe = prepare_native_comms(wire(Path("safe-relative")))
    assert safe.registry.prepare_native_root().namespace == PrivateRegistryGuard.load(safe.registry.store.path).namespace


@pytest.mark.skipif(os.name == "posix", reason="Windows private-bus fail-closed contract")
def test_native_root_rejects_unattestable_windows_ownership(tmp_path: Path) -> None:
    with pytest.raises(RelationViolationError):
        prepare_native_comms(wire(tmp_path))


class TestAgentRuntimeInfo:
    def test_context_percent_and_persistence(self, tmp_path: Path):
        info = AgentRuntimeInfo(
            thread="a", model=DeclaredModel('provider/model'), context_used=PiCount.from_wire(25), context_size=PiCount.from_wire(100), timestamp=1.0
        )
        assert info.context_reading() == ContextUsage(25, 100)
        store = RuntimeInfoStore(tmp_path / "runtime.json")
        store.observe("a", 1.0, AgentInfo(model=DeclaredModel('provider/model'), context_size=PiCount.from_wire(100)))
        store.observe("a", 1.0, ContextUsageObservation(context_used=PiCount.from_wire(25)))
        assert store.read().observations["a"] == info

    def test_unknown_context_stays_unknown(self):
        assert AgentRuntimeInfo(thread="a", context_size=PiCount.from_wire(100), timestamp=1.0).context_reading() == UnmeasuredContext()

    def test_nonpositive_context_is_unreported(self):
        info = AgentRuntimeInfo(thread="a", context_used=PiCount.from_wire(-1), timestamp=1.0)
        assert info.context_used == PiCount.from_wire(None)
        assert info.context_reading() == UnmeasuredContext()


class TestGoalRevision:
    @pytest.mark.parametrize("revision", [True, 1.0, "1", -1, 1 << 63])
    def test_rejects_noncanonical_or_exhausted_revision(self, revision):
        goal_id = GoalId.mint()
        with pytest.raises(ValueError):
            Goal(text="work", id=goal_id, published_seq=0, revision=revision)

    def test_old_registry_goal_without_revision_reopens_at_zero(self, tmp_path: Path):
        old_goal_id = GoalId.mint()
        path = tmp_path / "registry.json"
        registry = Registration(path)
        registry.register(
            Thread(name='owner', tags=frozenset(), worktree=Checkout.from_wire('/wt'), goal=Goal('work',
                old_goal_id, published_seq=0))
        )
        raw = json.loads(path.read_text())
        del raw["threads"]["owner"]["goal"]["revision"]
        path.write_text(json.dumps(raw))
        restored = Registration(path).require("owner").goal
        assert restored == Goal("work", old_goal_id, published_seq=0, revision=0)


class TestThreadDeclaration:
    def test_constructing_declares(self, *, thread_registration):
        thread = Thread(name='a', tags=frozenset(), worktree=Checkout.from_wire('/wt'))
        assert thread.name == "a"
        assert thread.is_fork is False

    def test_rejects_bad_name_characters(self, *, thread_registration):
        with pytest.raises(ValueError):
            Thread(name='bad name!', tags=frozenset(), worktree=Checkout.from_wire('/wt'))

    def test_rejects_empty_name(self, *, thread_registration):
        with pytest.raises(ValueError):
            Thread(name='', tags=frozenset(), worktree=Checkout.from_wire('/wt'))

    def test_rejects_empty_worktree(self, *, thread_registration):
        with pytest.raises(ValueError):
            Thread(name='a', tags=frozenset(), worktree=Checkout.from_wire(''))

    def test_rejects_self_parent(self, *, thread_registration):
        with pytest.raises(RelationViolationError):
            Thread(name='a', tags=frozenset(), worktree=Checkout.from_wire('/wt'), parent=ForkedParent('a'))

    def test_fork_provenance(self, *, thread_registration):
        fork = Thread(name='b', tags=frozenset(), worktree=Checkout.from_wire('/wt'), parent=ForkedParent('a'))
        assert fork.is_fork is True
        assert fork.parent == ForkedParent("a")

    def test_frozen(self, *, thread_registration):
        thread = Thread(name='a', tags=frozenset(), worktree=Checkout.from_wire('/wt'))
        with pytest.raises(AttributeError):
            thread.name = "b"  # type: ignore[misc]


class TestMessageDeclaration:
    def test_constructing_declares(self):
        message = Message(sender="a", target="b", body="hello", type=InfoKind)
        assert message.sender == "a" and message.target == "b"

    def test_rejects_self_message(self):
        with pytest.raises(RelationViolationError):
            Message(sender="a", target="a", body="hello", type=InfoKind)

    def test_rejects_empty_sender(self):
        with pytest.raises(RelationViolationError):
            Message(sender="", target="b", body="hello", type=InfoKind)

    def test_rejects_empty_target(self):
        with pytest.raises(RelationViolationError):
            Message(sender="a", target="", body="hello", type=InfoKind)

    def test_rejects_empty_body(self):
        with pytest.raises(ValueError):
            Message(sender="a", target="b", body="", type=InfoKind)

    def test_id_is_identity_not_ordering(self):
        # Hash identity: two identical messages share an id, ids are not
        # monotonic in time, so delivery order must come from seq.
        a = Message(sender="a", target="b", body="x", type=InfoKind, timestamp=1.0)
        b = Message(sender="a", target="b", body="x", type=InfoKind, timestamp=1.0)
        assert a.message_id == b.message_id

    def test_wire_roundtrip(self):
        original = Message(sender="a", target="b", body="hello", type=HandoffKind, seq=7)
        restored = Message.from_wire(original.to_wire())
        assert restored.sender == original.sender
        assert restored.target == original.target
        assert restored.body == original.body
        assert restored.type is HandoffKind
        assert restored.seq == 7


class TestRegistration:
    def test_register_and_require(self, tmp_path: Path):
        registry = Registration(tmp_path / "registry.json")
        thread = Thread(name='a', tags=frozenset(), worktree=Checkout.from_wire('/wt'))
        registry.register(thread)
        assert registry.require("a") is not None
        assert "a" in registry

    def test_fail_closed_unknown_reference(self, tmp_path: Path):
        registry = Registration(tmp_path / "registry.json")
        with pytest.raises(UnregisteredThreadError):
            registry.require("missing")

    def test_status_transitions(self, tmp_path: Path):
        registry = Registration(tmp_path / "registry.json")
        registry.register(Thread(name='a', tags=frozenset(), worktree=Checkout.from_wire('/wt')))
        assert registry.status("a") == HeartbeatThreadStatus()
        registry.unregister("a")
        assert registry.status("a") == StoppedThreadStatus()
        # Unregistering a stopped (but known) thread is idempotent.
        registry.unregister("a")
        assert registry.status("a") == StoppedThreadStatus()
        # Only unknown references fail closed.
        with pytest.raises(UnregisteredThreadError):
            registry.unregister("ghost")

    def test_owner_claim_survives_session_metadata_and_rotates_on_restart(
        self, tmp_path: Path
    ) -> None:
        registry = Registration(tmp_path / "registry.json")
        owner = Thread(name='a', tags=frozenset(), worktree=Checkout.from_wire('/wt'), process_identity=ProcessIdentity.capture(os.getpid()))
        registry.register(owner)
        before = registry.snapshot().claim_generation("a")
        registry.register(replace(owner, session_file=RegistrySessionLocation.from_path(str(tmp_path / "session.jsonl"), tmp_path)))
        assert registry.snapshot().claim_generation("a") == before
        assert Registration(registry.store.path).snapshot().claim_generation("a") == before

        registry.register(replace(registry.require("a"), process_identity=ProcessIdentity.capture(os.getppid())))
        assert registry.snapshot().claim_generation("a") > before

    def test_owner_claim_follows_same_owner_rename(self, tmp_path: Path) -> None:
        registry = Registration(tmp_path / "registry.json")
        registry.register(Thread(name='a', tags=frozenset(), worktree=Checkout.from_wire('/wt'), process_identity=ProcessIdentity.capture(os.getpid())))
        before = registry.snapshot().claim_generation("a")
        registry.rename("a", "renamed-a")
        snapshot = Registration(registry.store.path).snapshot()
        assert snapshot.claim_generation("renamed-a") == before
        assert snapshot.claim_generation("a") == before
        assert not hasattr(snapshot, "claimed_generations")

    def test_rename_reclaims_own_alias_without_losing_owner_or_turn(self, tmp_path: Path) -> None:
        goal_1_goal_id = GoalId.mint()
        registry = Registration(tmp_path / "registry.json")
        registry.register(
            Thread(
                name='agent-comms-ux', tags=frozenset(), worktree=Checkout.from_wire('/wt'),
                process_identity=ProcessIdentity.capture(os.getpid()),
                goal=Goal('Finish the goal', goal_1_goal_id, published_seq=0),
            )
        )
        registry.register(
            Thread(name='child', tags=frozenset(), worktree=Checkout.from_wire('/wt'), parent=ForkedParent('agent-comms-ux'))
        )
        original = registry.require("agent-comms-ux")
        registry.rename("agent-comms-ux", "pr17")
        claim_scope = registry.snapshot().claim_generation("pr17")
        registry.lease_local_turn("pr17", TurnId('3de341077ab881901aed7df98057d67f'))

        assert registry.rename("pr17", "agent-comms-ux") == ("pr17", "agent-comms-ux")

        reopened = Registration(registry.store.path)
        snapshot = reopened.snapshot()
        assert snapshot.aliases == {"pr17": "agent-comms-ux"}
        assert reopened.require("pr17").name == "agent-comms-ux"
        assert reopened.require("agent-comms-ux").created_at == original.created_at
        assert reopened.require("agent-comms-ux").goal == original.goal
        assert reopened.require("child").parent == ForkedParent("agent-comms-ux")
        assert snapshot.claim_generation("agent-comms-ux") == claim_scope
        assert reopened.release_turn(reopened.require("pr17").turn_lease)[0]

    def test_other_owner_writes_do_not_invalidate_private_generation(self, tmp_path: Path):
        registry = Registration(tmp_path / "registry.json")
        for name in ("a", "b"):
            registry.register(Thread(name=name, tags=frozenset(), worktree=Checkout.from_wire('/wt'), process_identity=ProcessIdentity.capture(os.getpid())))
        snapshot = registry.snapshot()
        owner = RegistryOwner.capture_local(snapshot, "a").thread
        claimed_generation = snapshot.owner_identity("a").generation
        registry.heartbeat("b")
        snapshot = registry.snapshot()
        other = RegistryOwner.capture_local(snapshot, "b").thread
        registry.lease_local_turn(other.name, TurnId('d9298a10d1b0735837dc4bd85dac641b'))
        snapshot = registry.snapshot()
        assert RegistryOwner.capture_local(snapshot, "a").thread == owner
        assert snapshot.owner_identity("a").generation == claimed_generation
        turn, _ = registry.lease_local_turn(owner.name, TurnId('3fd30542fe3f61b14bd4a4b2dc0b6fb3'))
        lease = turn.require_turn_lease()
        assert registry.release_turn(lease)[0]
        assert not registry.release_turn(lease)[0]
        assert "owner_epoch" not in owner.to_wire()

    @pytest.mark.parametrize("revocation", ["stop", "finish"])
    def test_registering_saved_turn_never_attests_a_revoked_incarnation(
        self, tmp_path: Path, revocation: str
    ) -> None:
        registry = Registration(tmp_path / "registry.json")
        registry.register(Thread(name='a', tags=frozenset(), worktree=Checkout.from_wire('/wt'), process_identity=ProcessIdentity.capture(os.getpid())))
        snapshot = registry.snapshot()
        owner = RegistryOwner.capture_local(snapshot, "a").thread
        leased, owner_generation = registry.lease_local_turn(owner.name, TurnId('d79d3458a58877cd8e48c18ebaf4c27b'))
        snapshot = registry.snapshot()
        assert RegistryOwner.capture_local(snapshot, "a").thread == leased
        assert snapshot.owner_identity("a").generation == owner_generation
        if revocation == "stop":
            registry.unregister("a")
        else:
            assert registry.release_turn(registry.require("a").turn_lease)[0]
        registry.register(leased)
        with pytest.raises(
            RelationViolationError
        ):
            registry.live_owner_with_claim("a")
        if revocation == "stop":
            assert registry.snapshot().owner_generations["a"] > owner_generation
        else:
            assert registry.snapshot().owner_generations["a"] == owner_generation
        assert "turn_epochs" not in leased.to_wire()

    def test_comms_begin_turn_cannot_revive_stopped_owner(self, tmp_path: Path) -> None:
        comms = wire(tmp_path / "wire")
        comms.registry.declare(Thread(name='a', tags=frozenset(), worktree=Checkout.from_wire('/wt'), process_identity=ProcessIdentity.capture(os.getpid())))
        comms.registry.unregister("a")
        with pytest.raises(RelationViolationError):
            comms.agents.begin_turn("a", TurnId('eb1c7f738bbe6d0fd106bf4a2fe97600'))
        assert comms.registry.status("a") == StoppedThreadStatus()

    @pytest.mark.skipif(os.name == "nt", reason="private POSIX ownership unavailable on Windows")
    def test_native_guard_can_precede_first_registry_snapshot(self, tmp_path: Path) -> None:
        root = tmp_path / "private-fresh"
        root.mkdir(mode=0o700)
        comms = wire(root)
        prepare_native_comms(comms)
        reopened = wire(root)
        reopened.registry.declare(Thread(name='a', tags=frozenset(), worktree=Checkout.from_wire('/wt'), process_identity=ProcessIdentity.capture(os.getpid())))
        assert reopened.registry.snapshot().owner_identity("a").generation > 0

    @pytest.mark.skipif(os.name == "nt", reason="private POSIX ownership unavailable on Windows")
    def test_native_guard_does_not_bootstrap_stripped_owner_generation(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "private-wire"
        root.mkdir(mode=0o700)
        comms = wire(root)
        comms.registry.declare(Thread(name='a', tags=frozenset(), worktree=Checkout.from_wire('/wt'), process_identity=ProcessIdentity.capture(os.getpid())))
        prepare_native_comms(comms)
        registry_path = root / "registry.json"
        data = json.loads(registry_path.read_text())
        for field in ("owners", "claims"):
            data.pop(FieldCodec.record_key(RegistryDocument, field))
        registry_path.write_text(json.dumps(data))
        with pytest.raises(RelationViolationError):
            comms.agents.begin_turn("a", TurnId('18e37571ddd8a9cd1f2b7ffa63493bb6'))
        with pytest.raises(RelationViolationError):
            comms.registry.register(Thread(name='a', tags=frozenset(), worktree=Checkout.from_wire('/wt')))

    @pytest.mark.skipif(sys.platform != "linux", reason="fault injection uses Linux /proc/self/fd")
    @pytest.mark.parametrize("stage", ["pending", "commit"])
    def test_torn_guard_slot_never_falls_back_to_older_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
    ) -> None:
        root = tmp_path / "private"
        comms = wire(root)
        comms.registry.declare(Thread(name='a', tags=frozenset(), worktree=Checkout.from_wire('/wt')))
        prepare_native_comms(comms)
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
        with pytest.raises(RelationViolationError):
            comms.registry.register(Thread(name='b', tags=frozenset(), worktree=Checkout.from_wire('/wt')))
        monkeypatch.undo()
        with pytest.raises(RelationViolationError):
            Registration(root / "registry.json").require("a")

    @pytest.mark.skipif(os.name == "nt", reason="private POSIX ownership unavailable on Windows")
    @pytest.mark.parametrize("damage", ["missing", "corrupt", "truncated", "strip_epochs"])
    def test_private_guard_rejects_disk_downgrade_even_with_cached_revision(
        self, tmp_path: Path, damage: str
    ) -> None:

        root = tmp_path / "guarded"
        comms = wire(root)
        comms.registry.declare(Thread(name='a', tags=frozenset(), worktree=Checkout.from_wire('/wt'), process_identity=ProcessIdentity.capture(os.getpid())))
        prepare_native_comms(comms)
        # A native coordinator is retained evidence of private mode even if its guard vanishes.
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
            for field in ("owners", "claims"):
                data.pop(FieldCodec.record_key(RegistryDocument, field))
            path.write_text(json.dumps(data))  # simulated old writer ignores marker
        with pytest.raises(RelationViolationError):
            cold.require("a")
        with pytest.raises(RelationViolationError):
            comms.registry.register(Thread(name='b', tags=frozenset(), worktree=Checkout.from_wire('/wt')))
        # A refused read leaves no cached document; none may carry guard state.
        entry = comms.registry.store.cache.entry
        assert entry is None or "registry_guard" not in entry.document.threads["a"].to_wire()

    def test_release_turn_resolves_retained_alias_only_for_exact_lease(self, tmp_path: Path):
        registry = Registration(tmp_path / "registry.json")
        registry.register(Thread(name='a', tags=frozenset(), worktree=Checkout.from_wire('/wt'), process_identity=ProcessIdentity.capture(os.getpid())))
        registry.rename("a", "b")
        snapshot = registry.snapshot()
        owner = RegistryOwner.capture_local(snapshot, "a").thread
        assert owner.name == "b"
        leased, _ = registry.lease_local_turn(owner.name, TurnId('d79d3458a58877cd8e48c18ebaf4c27b'))
        registry.rename("b", "c")
        assert not registry.release_turn(
            replace(leased.turn_lease, turn_id=TurnId('d9298a10d1b0735837dc4bd85dac641b'))
        )[0]
        assert registry.release_turn(leased.turn_lease)[0]

    def test_deleting_thread_cannot_be_revived(self, tmp_path: Path):
        registry = Registration(tmp_path / "registry.json")
        thread = Thread(name='a', tags=frozenset(), worktree=Checkout.from_wire('/wt'))
        registry.register(thread)
        registry.unregister("a")
        registry.begin_delete("a")
        assert registry.status("a") == DeletingThreadStatus()
        with pytest.raises(RelationViolationError):
            registry.heartbeat("a")
        with pytest.raises(RelationViolationError):
            registry.register(thread)

    def test_active_threads_excludes_stopped(self, tmp_path: Path):
        registry = Registration(tmp_path / "registry.json")
        registry.register(Thread(name='a', tags=frozenset(), worktree=Checkout.from_wire('/wt')))
        registry.register(Thread(name='b', tags=frozenset(), worktree=Checkout.from_wire('/wt')))
        registry.unregister("a")
        assert set(registry.active_threads()) == {"b"}
        assert set(registry.all_threads()) == {"a", "b"}

    def test_remove_drops_declaration_entirely(self, tmp_path: Path):
        registry = Registration(tmp_path / "registry.json")
        registry.register(Thread(name='a', tags=frozenset(), worktree=Checkout.from_wire('/wt')))
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
            Thread(name='a', tags=frozenset({'x', 'y'}), worktree=Checkout.from_wire('/wt'), parent=ForkedParent('p'), task=GivenThreadText('t'), process_identity=ProcessIdentity.capture(os.getpid()))
        )
        registry.unregister("a")
        reloaded = Registration(path)
        thread = reloaded.require("a")
        assert thread.parent == ForkedParent("p") and thread.task == GivenThreadText("t") and thread.process_identity == ProcessIdentity.capture(os.getpid())
        assert thread.tags == frozenset({"x", "y"})
        assert reloaded.status("a") == StoppedThreadStatus()

    def test_peers_excludes_self(self, tmp_path: Path):
        registry = Registration(tmp_path / "registry.json")
        for name in ("a", "b", "c"):
            registry.register(Thread(name=name, tags=frozenset(), worktree=Checkout.from_wire('/wt')))
        assert set(registry.peers("b")) == {"a", "c"}


class TestSharedLedger:
    def test_merge_and_read(self, tmp_path: Path):
        ledger = SharedLedger(tmp_path / "ledger.json")
        ledger.merge({"a": 1}, author="x")
        assert FieldCodec.encode(ledger.read())["a"] == 1

    def test_rejects_non_string_keys(self, tmp_path: Path):
        ledger = SharedLedger(tmp_path / "ledger.json")
        with pytest.raises(ValueError):
            ledger.merge({1: "x"}, author="x")

    def test_persistence_roundtrip(self, tmp_path: Path):
        path = tmp_path / "ledger.json"
        SharedLedger(path).merge({"k": "v"}, author="x")
        assert FieldCodec.encode(SharedLedger(path).read())["k"] == "v"

    def test_records_author(self, tmp_path: Path):
        ledger = SharedLedger(tmp_path / "ledger.json")
        ledger.merge({}, author="me")
        assert FieldCodec.encode(ledger.read())["last_updated_by"] == "me"


class TestCurrentThread:
    def test_fail_closed_without_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("AGENT_COMMS_THREAD", raising=False)
        with pytest.raises(CallingThreadUnavailable):
            current_thread(None)

    def test_owner_launch_environment_declares_the_same_thread(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        registry = wire(tmp_path).registry
        launched = Thread(
            "worker", frozenset({"a", "b"}), Checkout.from_wire(str(tmp_path / "project")), parent=ForkedParent("lead"), task=GivenThreadText("Ship it")
        )
        for name, value in launched.launch().apply(dict(os.environ)).items():
            monkeypatch.setenv(name, value)
        thread = current_thread(registry)
        assert (thread.name, thread.tags, thread.worktree, thread.parent, thread.task) == (
            "worker", frozenset({"a", "b"}), str(tmp_path / "project"), ForkedParent("lead"), GivenThreadText("Ship it")
        )
