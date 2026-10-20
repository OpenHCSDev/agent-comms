"""Actual conversation/native lock paths follow their store declarations."""
import importlib
import os
import threading
import traceback

import pytest

from agent_comms.comms import wire
from agent_comms.coordination_session import NativeBoundary
from agent_comms.historical_views import ConversationObservation, DMDisplayHistory
from agent_comms.message_bus import MessageBus
from agent_comms.store_files import (
    ExclusiveLock, SharedLock, StoreLock, StoreLockContention, StoreLockOwner,
    store_lock_path,
)
from agent_comms import store_order, wire_log


def test_undeclared_store_rank_is_rejected_before_acquisition():
    with pytest.raises(AttributeError):
        with StoreLock.ordered({object(): SharedLock()}):
            pytest.fail("Unranked owner was authorized")


def test_complete_tuple_derives_every_rank():
    assert set(store_order.STORE_LOCK_ORDER) == set(StoreLockOwner.members_with(StoreLockOwner))
    for rank, owner in enumerate(store_order.STORE_LOCK_ORDER):
        assert vars(owner)["lock_rank"] == rank


def test_duplicate_owner_in_order_is_rejected_at_module_load(monkeypatch):
    try:
        with monkeypatch.context() as patch:
            patch.setattr(wire_log, "WireLog", MessageBus)
            with pytest.raises(TypeError):
                importlib.reload(store_order)
    finally:
        importlib.reload(store_order)


def test_conversation_read_and_native_write_share_declared_order(tmp_path):
    comms = wire(tmp_path / "wire")
    comms.registry.declare(comms.registry.new_thread('peer', frozenset({'comms'}), str(tmp_path)))
    comms.messaging.user_identity(str(tmp_path))
    reader_has_bus, writer_at_bus = threading.Event(), threading.Event()
    failures, events = [], []
    held, files, native_bus_states = {}, {}, []
    bus_path = str(store_lock_path(comms.bus.log.path))
    ranks = {bus_path: comms.bus.log.lock_rank}

    def profile(frame, event, arg):
        if frame.f_code is StoreLockOwner.locked.__code__ and event == "call":
            owner = frame.f_locals["self"]
            ranks[str(store_lock_path(owner.path))] = owner.lock_rank
            return
        operation = frame.f_code.co_name
        if operation != "_store_lock_file" and frame.f_code is not StoreLockContention.acquire.__code__:
            return
        actor = threading.current_thread().name
        active = held.setdefault(actor, {})
        if operation == "_store_lock_file":
            if event == "return":
                if arg is not None:
                    files[frame] = arg.fileno()
                elif frame in files:
                    # POSIX releases at file close, not release_store_lock's no-op.
                    active.pop(files.pop(frame), None)
            return
        descriptor = frame.f_locals["descriptor"]
        path = os.readlink(f'/proc/self/fd/{descriptor}')
        if event == "call":
            if path in ranks:
                assert all(rank <= ranks[path] for rank, _path in active.values()), (
                    "out-of-order store acquisition", actor, path,
                    tuple(active.values()), "".join(traceback.format_stack(frame)))
            if actor == "native" and path == bus_path:
                native_bus_states.append((tuple(active.values()), frame.f_locals["mode"]))
                writer_at_bus.set()
        if event != "return":
            return
        if path in ranks:
            active[descriptor] = (ranks[path], path)
        events.append((actor, path, frame.f_locals["mode"]))
        # Select the retained conversation cut, not user_identity's earlier read.
        parent = frame.f_back
        while parent is not None:
            if parent.f_code.co_name == "reading" and parent.f_locals.get("sources"):
                if actor == "observer" and path.endswith(".bus.jsonl.lock"):
                    reader_has_bus.set()
                    assert writer_at_bus.wait(3)
                break
            parent = parent.f_back

    def observe():
        try:
            revision = DMDisplayHistory.revision(comms.views, "peer", ConversationObservation(), worktree=str(tmp_path))
            assert revision.selection.peer.name == "peer"
        except BaseException as error:
            failures.append(error)

    def publish():
        try:
            assert reader_has_bus.wait(3)
            with NativeBoundary(comms.bus) as boundary:
                assert boundary.registry.require("peer").name == "peer"
        except BaseException as error:
            failures.append(error)

    prior = threading.getprofile()
    threading.setprofile(profile)
    reader = threading.Thread(target=observe, name="observer", daemon=True)
    writer = threading.Thread(target=publish, name="native", daemon=True)
    try:
        reader.start()
        writer.start()
        reader.join(5)
        writer.join(5)
    finally:
        threading.setprofile(prior)
    assert not reader.is_alive() and not writer.is_alive()
    assert not failures, failures
    assert reader_has_bus.is_set() and writer_at_bus.is_set()
    assert not files
    assert all(not active for active in held.values())
    native = [path for actor, path, _mode in events if actor == "native"]
    assert len(native_bus_states) == native.count(bus_path) == 2
    # The activity read holds no rank; publication holds the namespace rank.
    assert native_bus_states[0][0] == ()
    assert isinstance(native_bus_states[0][1], SharedLock)
    assert native_bus_states[1][0] == ((comms.bus.lock_rank, str(store_lock_path(comms.bus.path))),)
    assert isinstance(native_bus_states[1][1], ExclusiveLock)
    assert native.count(str(store_lock_path(comms.registry.store.path))) == 1


def test_native_bus_read_under_registry_lock_is_rejected(tmp_path, monkeypatch):
    class InvertedBoundary(NativeBoundary):
        def __enter__(self):
            self.enter_context(self.bus._registry.store.locked(mode=SharedLock()))
            return super().__enter__()

    monkeypatch.setitem(globals(), "NativeBoundary", InvertedBoundary)
    with pytest.raises(AssertionError):
        test_conversation_read_and_native_write_share_declared_order(tmp_path)
