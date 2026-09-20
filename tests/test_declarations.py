import json
from pathlib import Path

import pytest

from agent_comms import (
    AgentRuntimeInfo,
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
