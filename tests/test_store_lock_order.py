"""Actual conversation/native lock paths follow their store declarations."""
import os
import threading

import pytest

from agent_comms.comms import wire
from agent_comms.coordination_session import NativeBoundary
from agent_comms.historical_views import DMDisplayHistory
from agent_comms.store_files import StoreLock
from agent_comms.threads import Thread


def test_undeclared_store_rank_is_rejected_before_acquisition():
    with pytest.raises(AttributeError, match="lock_rank"):
        with StoreLock.ordered({object(): True}):
            pytest.fail("Unranked owner was admitted")


def test_conversation_read_and_native_write_share_declared_order(tmp_path):
    comms = wire(tmp_path / "wire")
    comms.registry.declare(Thread("peer", frozenset({"comms"}), str(tmp_path)))
    comms.messaging.user_identity(str(tmp_path))
    reader_has_bus, writer_at_bus = threading.Event(), threading.Event()
    failures, events = [], []

    def profile(frame, event, arg):
        if frame.f_code.co_name != "acquire_store_lock":
            return
        path = os.readlink(f'/proc/self/fd/{frame.f_locals["descriptor"]}')
        actor = threading.current_thread().name
        if actor == "native" and event == "call" and path.endswith(".bus.jsonl.lock"):
            writer_at_bus.set()
        if event != "return":
            return
        events.append((actor, path, frame.f_locals["shared"]))
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
            revision = DMDisplayHistory.revision(comms.views, "peer", worktree=str(tmp_path))
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
        reader.start(); writer.start()
        reader.join(5); writer.join(5)
    finally:
        threading.setprofile(prior)
    assert not reader.is_alive() and not writer.is_alive()
    assert not failures, failures
    assert reader_has_bus.is_set() and writer_at_bus.is_set()
    native = [path for actor, path, shared in events if actor == "native"]
    assert native.index(str(comms.root / ".wire.lock")) < native.index(str(comms.root / ".bus.jsonl.lock"))
    assert native.index(str(comms.root / ".bus.jsonl.lock")) < native.index(str(comms.root / ".registry.json.lock"))
