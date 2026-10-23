"""A watch claim observes process death through its kernel-held lifetime."""

import multiprocessing
import signal
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from agent_comms.cli_bridges.monitor import InspectSession
from agent_comms_hooks.watch import LapsedWatch, LiveWatch, WatchClaim, WatchRecord

RECORD = WatchRecord(
    "commander", "/bus", ("agent-comms-claude-bridge", "watch", "--state", "/bus with spaces")
)


def hold_claim(claim, record, ready, release):
    with claim.held(record):
        ready.set()
        release.wait()


@contextmanager
def held_elsewhere(claim):
    """Another process holds the claim, as a running bridge does."""
    context = multiprocessing.get_context("fork")
    ready, release = context.Event(), context.Event()
    child = context.Process(target=hold_claim, args=(claim, RECORD, ready, release))
    child.start()
    try:
        assert ready.wait(5), "claim holder did not start"
        yield child
    finally:
        if child.is_alive():
            child.kill()
        child.join(5)
        child.close()


def killed(child):
    child.kill()
    child.join(5)
    assert child.exitcode == -signal.SIGKILL


def test_killed_holder_leaves_a_lapsed_claim_with_its_record(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    claim = WatchClaim("session")
    with held_elsewhere(claim) as child:
        assert isinstance(claim.state(), LiveWatch)
        killed(child)
        state = claim.state()
        assert isinstance(state, LapsedWatch)
        assert state.record == RECORD


def test_reader_waits_for_a_held_claims_socket_until_its_holder_dies(bridge_home):
    claim = WatchClaim("session", bridge_home)
    with held_elsewhere(claim) as child, ThreadPoolExecutor(1) as pool:
        reader = pool.submit(InspectSession().awaited, claim)
        with pytest.raises(TimeoutError):
            reader.result(timeout=1)
        killed(child)
        with pytest.raises(RuntimeError, match='No session bridge is running') as stopped:
            reader.result(timeout=5)
        assert isinstance(stopped.value.__cause__, FileNotFoundError)
