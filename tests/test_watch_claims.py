"""A watch claim observes process death through its kernel-held lifetime."""

import multiprocessing
import signal
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.cli_bridges.monitor import InspectSession
from agent_comms_hooks.watch import LapsedWatch, LiveWatch, NoSessionBridge, WatchClaim, WatchRecord

RECORD = WatchRecord(
    "commander", "/bus", ("agent-comms-claude-bridge", "watch", "--state", "/bus with spaces"),
    Path("/bus with spaces"),
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
    swaps = []
    with held_elsewhere(claim) as child, ThreadPoolExecutor(1) as pool:
        reader = pool.submit(InspectSession().awaited, claim, swaps.append)
        with pytest.raises(TimeoutError):
            reader.result(timeout=1)
        assert swaps == [claim]
        killed(child)
        with pytest.raises(NoSessionBridge) as stopped:
            reader.result(timeout=5)
        assert isinstance(stopped.value.__cause__, FileNotFoundError)


ADOPTED = replace(RECORD, command=(*RECORD.command, "--adopted"))


def adopt_repeatedly(claim, ready, rounds):
    """An install's exec successor adopts the held description and publishes its record;
    here one holder does that many times on dups of its own open description."""
    with claim.held(RECORD) as held:
        ready.set()
        for round_ in range(rounds):
            with claim.adopted(held.inheritable(), (ADOPTED, RECORD)[round_ % 2]):
                pass


def test_readers_racing_claim_adoption_always_decode_a_whole_record(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    claim = WatchClaim("session")
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    child = context.Process(target=adopt_repeatedly, args=(claim, ready, 2000))
    child.start()
    reads = 0
    try:
        assert ready.wait(5), "claim holder did not start"
        while child.is_alive():
            state = claim.state()
            assert isinstance(state, LiveWatch | LapsedWatch)
            assert state.record in (RECORD, ADOPTED)
            reads += 1
    finally:
        child.join(30)
    assert child.exitcode == 0
    assert reads > 0
