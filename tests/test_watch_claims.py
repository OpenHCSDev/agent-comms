"""A watch claim observes process death through its kernel-held lifetime."""

import multiprocessing
import signal

from agent_comms_hooks.watch import LapsedWatch, LiveWatch, WatchClaim, WatchRecord


def hold_claim(claim, record, ready, release):
    with claim.held(record):
        ready.set()
        release.wait()


def test_killed_holder_leaves_a_lapsed_claim_with_its_record(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    record = WatchRecord(
        "commander", "/bus", ("agent-comms-claude-bridge", "watch", "--state", "/bus with spaces")
    )
    claim = WatchClaim("session")
    context = multiprocessing.get_context("fork")
    ready, release = context.Event(), context.Event()
    child = context.Process(target=hold_claim, args=(claim, record, ready, release))
    child.start()
    try:
        assert ready.wait(5), "claim holder did not start"
        assert isinstance(claim.state(), LiveWatch)
        child.kill()
        child.join(5)
        assert child.exitcode == -signal.SIGKILL
        state = claim.state()
        assert isinstance(state, LapsedWatch)
        assert state.record == record
    finally:
        if child.is_alive():
            child.kill()
        child.join(5)
        child.close()
