"""An install's socket gap is waited out, never reported as a failed tool call.

The read gate runs on every tool call. While the installer swaps a bridge's socket, connect()
can time out or come back reset; that is the gap the swap wait exists for, so the hook must end
in the program's own absent answer instead of raising HookBridgeUnavailable.
"""

import multiprocessing
import socket
from pathlib import Path

import pytest

from agent_comms_hooks.watch import SessionEndpoint, SwappingSession, WatchRecord

RECORD = WatchRecord("colleague", "/bus", ("agent-comms-claude-bridge", "watch"), Path("/bus"))


class RecordingProgram:
    """The hook program's three answers, recorded instead of printed."""

    absent_calls = []

    @classmethod
    def wire_name(cls) -> str:
        return "read_gate"

    @classmethod
    def absent(cls, record, error) -> None:
        cls.absent_calls.append(error)


def hold_claim(claim, ready, release) -> None:
    with claim.held(RECORD):
        ready.set()
        release.wait()


def test_a_swapping_socket_that_never_answers_lets_the_program_answer_alone(
    bridge_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = SessionEndpoint("session", bridge_home)
    endpoint.path.parent.mkdir(parents=True, exist_ok=True)
    endpoint.record_path.write_text(RECORD.to_json())

    # The claim is held by another process, as an installer holds it across its restart.
    from agent_comms.cli_bridges.session_watch import WatchClaim

    claim = WatchClaim("session", bridge_home)
    context = multiprocessing.get_context("fork")
    ready, release = context.Event(), context.Event()
    holder = context.Process(target=hold_claim, args=(claim, ready, release))
    holder.start()
    try:
        assert ready.wait(5), "claim holder did not start"
        # A listening socket that accepts and never answers: exactly a bridge mid-swap.
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(endpoint.socket_path))
        listener.listen(1)
        try:
            monkeypatch.setattr(SwappingSession, "wait", 0.6)
            RecordingProgram.absent_calls.clear()
            SwappingSession(endpoint).invoke(RecordingProgram, {})
        finally:
            listener.close()
    finally:
        release.set()
        holder.join(5)
        holder.close()

    assert len(RecordingProgram.absent_calls) == 1, "the hook must end in the program's own answer"
