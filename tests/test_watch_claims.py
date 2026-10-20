"""Every claim observes process death through its shared kernel-held lifetime."""

import json
import multiprocessing
import shlex
import signal

import pytest

from agent_comms_hooks import ClaudeHook
from agent_comms_hooks.watch import LapsedWatch, LiveWatch, LoopClaim, WatchClaim, WatchRecord


def hold_claim(claim, record, ready, release):
    with claim.held(record):
        ready.set()
        release.wait()


@pytest.mark.parametrize("claim_kind", (WatchClaim, LoopClaim))
def test_killed_holder_warns_with_original_command(claim_kind, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    record = WatchRecord(
        "commander", "/bus", ("agent-comms-commander", "tool-watch", "--root", "/bus with spaces")
    )
    claim = (WatchClaim("session") if claim_kind is WatchClaim
             else LoopClaim.for_command(record.command))
    context = multiprocessing.get_context("fork")
    ready, release = context.Event(), context.Event()
    child = context.Process(target=hold_claim, args=(claim, record, ready, release))
    child.start()
    try:
        assert ready.wait(5), "claim holder did not start"
        live = claim.state()
        assert isinstance(live, LiveWatch)
        if claim_kind is LoopClaim:
            live.report(ClaudeHook("session", "UserPromptSubmit"))
            assert capsys.readouterr().out == ""
        child.kill()
        child.join(5)
        assert child.exitcode == -signal.SIGKILL
        state = claim.state()
        assert isinstance(state, LapsedWatch)
        assert state.record == record
        state.report(ClaudeHook("session", "UserPromptSubmit"))
        warning = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        assert shlex.join(record.command) in warning
        assert str(claim.path) in warning
    finally:
        if child.is_alive():
            child.kill()
        child.join(5)
        child.close()
